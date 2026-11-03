"""
Módulo de Procesamiento Automatizado de Inbox (WhatsApp a PostGIS).

Este script monitorea la carpeta 'whatsapp_inbox', transcribe mensajes de audio
mediante OpenAI Whisper, extrae datos de texto e imágenes, anonimiza al
ciudadano mediante hashing SHA-256, clasifica el reclamo consultando la base
de datos y guarda el registro junto con su trazabilidad en el historial.

Cumple con especificaciones de codificación PEP 8 y buenas prácticas de auditoría.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()

# Configuración de Nominatim para Avellaneda
AVELLANEDA_VIEWBOX = "-58.38,-34.72,-58.30,-34.64"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "MuniIA_MVP_Avellaneda/1.0 (14056411@ifts.edu.ar)"

# --- CONFIGURACIÓN DE CONEXIÓN A BASE DE DATOS ---
DB_CONFIG: Dict[str, str] = {
    "dbname": os.getenv("DB_NAME", "muniia"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASS", "postgres"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
}

# --- PARCHE DE COMPATIBILIDAD AUDIOOP / PYDUB ---
try:
    import audioop
except ImportError:
    try:
        import audioop_lts as audioop
        sys.modules["audioop"] = audioop
    except ImportError:
        pass

from pydub import AudioSegment
import whisper


def obtener_conexion() -> psycopg2.extensions.connection:
    """Establece y retorna una conexión activa a PostgreSQL/PostGIS."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except psycopg2.OperationalError as err:
        print("\n❌ [ERROR GRAVE DE CONEXIÓN A BASE DE DATOS]")
        print(f"   Detalle: {err}")
        raise


def get_barrio_postgis(conn: psycopg2.extensions.connection, lat: Optional[float], lon: Optional[float]) -> str:
    """
    Determina el barrio realizando una consulta espacial ST_Contains 
    sobre la tabla 'barrios_avellaneda' en PostGIS.
    """
    if lat is None or lon is None:
        return "Sarandí"  # Fallback predeterminado

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nombre 
                FROM barrios_avellaneda
                WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                LIMIT 1
            """, (lon, lat))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
            else:
                return "Avellaneda Centro"
    except Exception as e:
        print(f"⚠️ [ERROR] Barrio PostGIS: {e}")
        return "Sarandí"

def geocodificar_nominatim(direccion_texto: str) -> Tuple[Optional[float], Optional[float], str]:
    """
    Geocodifica una dirección limpia en Nominatim (OSM) delimitado a Avellaneda.
    Devuelve (lat, lon, precision).
    """
    if not direccion_texto or "a confirmar" in direccion_texto.lower():
        return None, None, "Sin Geolocalizar"

    query = f"{direccion_texto}, Avellaneda, Buenos Aires, Argentina"
    params = {
        "q": query,
        "format": "json",
        "bounded": 1,
        "viewbox": AVELLANEDA_VIEWBOX,
        "limit": 1,
        "countrycodes": "ar",
        "addressdetails": 1
    }
    
    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                if data:
                    item = data[0]
                    lat = float(item["lat"])
                    lon = float(item["lon"])
                    importance = float(item.get("importance", 0))
                    
                    precision = "Dirección con Altura" if importance >= 0.3 else "Aproximada"
                    return lat, lon, precision
    except Exception as err:
        print(f"⚠️ Error al consultar Nominatim OSM: {err}")

    return None, None, "Sin Geolocalizar"


def extraer_geometria_y_barrio_gratuito(
    conn: psycopg2.extensions.connection, 
    texto: str
) -> Tuple[str, str, float, float, str]:
    """
    Extrae estrictamente [Nombre de Calle] + [Altura], geocodifica vía OSM 
    y resuelve el barrio espacialmente vía PostGIS ST_Contains.
    """
    direccion_limpia = "Avellaneda (Dirección a confirmar)"
    lat, lon = -34.6865, -58.3486  # Centroide por defecto
    precision = "Sin Geolocalizar"

    if not texto or len(texto.strip()) < 5:
        barrio_detectado = get_barrio_postgis(conn, lat, lon)
        return direccion_limpia, barrio_detectado, lat, lon, precision

    # 1. Palabras de relleno comunes en reclamos de WhatsApp a remover del nombre de la calle
    stopwords_reclamo = [
        "buenas", "noches", "tardes", "dias", "hola", "hay", "un", "una", "el", "la", 
        "los", "las", "contenedor", "fuego", "arbol", "árbol", "vereda", "caido", "caído", 
        "prendido", "peligro", "por", "tormenta", "rota", "roto", "de", "en", "calle", 
        "av", "avenida", "al", "que", "hizo", "para", "hacer", "reclamo", "luz", "mercurio",
        "reparacion", "reparación", "reportar", "es", "una"
    ]

    # 2. Capturar patrón de calle + número (ejemplo: "... en la calle Mitre al 2000 ...")
    match = re.search(r"([a-zA-ZáéíóúñÁÉÍÓÚÑ\s]{3,35})\s+(?:al\s+)?(\d{2,5})", texto, re.IGNORECASE)

    if match:
        calle_candidata = match.group(1).strip()
        altura = match.group(2).strip()

        # Filtrar palabras de relleno para conservar sólo el nombre propio de la calle
        palabras_calle = [
            p.title() for p in calle_candidata.split() 
            if p.lower() not in stopwords_reclamo
        ]

        if palabras_calle:
            calle_nombre = " ".join(palabras_calle)
            direccion_limpia = f"{calle_nombre} {altura}"

            # 3. Intentar geocodificar Dirección + Altura en Nominatim
            lat_osm, lon_osm, prec_osm = geocodificar_nominatim(direccion_limpia)

            if lat_osm and lon_osm:
                lat, lon = lat_osm, lon_osm
                precision = prec_osm
            else:
                # Fallback: intentar solo con la calle si la altura exacta falla en OSM
                lat_calle, lon_calle, _ = geocodificar_nominatim(calle_nombre)
                if lat_calle and lon_calle:
                    lat, lon = lat_calle, lon_calle
                    precision = "Aproximada"

    # 4. Asignación espacial del barrio exacto usando PostGIS ST_Contains
    barrio_detectado = get_barrio_postgis(conn, lat, lon)

    return direccion_limpia, barrio_detectado, lat, lon, precision

def obtener_o_crear_ciudadano_hash(
    conn: psycopg2.extensions.connection,
    telefono_o_id: Optional[str],
    barrio: str = "Sarandí"
) -> str:
    """Genera hash SHA-256 del teléfono para anonimizar la identidad del vecino."""
    tz_arg = ZoneInfo("America/Argentina/Buenos_Aires")
    fecha_ahora = datetime.datetime.now(tz_arg)
    if not telefono_o_id:
        telefono_o_id = "anonimo_whatsapp"

    hash_ciudadano = hashlib.sha256(telefono_o_id.encode("utf-8")).hexdigest()[:16]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT cantidad_reclamos FROM ciudadanos_anonimizados WHERE id_ciudadano_hash = %s",
            (hash_ciudadano,)
        )
        row = cur.fetchone()

        if row:
            cur.execute(
                """
                UPDATE ciudadanos_anonimizados 
                SET cantidad_reclamos = cantidad_reclamos + 1,
                    barrio_inferido = COALESCE(%s, barrio_inferido)
                WHERE id_ciudadano_hash = %s
                """,
                (barrio, hash_ciudadano)
            )
        else:
            cur.execute(
                """
                INSERT INTO ciudadanos_anonimizados (
                    id_ciudadano_hash, barrio_inferido, primera_interaccion, cantidad_reclamos
                ) VALUES (%s, %s, %s, %s)
                """,
                (hash_ciudadano, barrio, fecha_ahora, 1)
            )

    return hash_ciudadano


def clasificar_y_obtener_metadatos(
    conn: psycopg2.extensions.connection,
    texto: Optional[str]
) -> Tuple[Optional[int], Optional[int], str, Optional[int]]:
    """
    Clasifica el texto consultando 'subcategorias' y 'categorias_generales'.
    Devuelve: (id_cat_general, id_subcategoria, urgencia_default, id_area_default)
    """
    if not texto or len(texto.strip()) < 3:
        return None, None, "MEDIA", None

    texto_lower = texto.lower()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT 
                    s.id_subcategoria, 
                    s.id_cat_general, 
                    s.nombre_subcategoria, 
                    s.urgencia_default,
                    c.id_area_default
                FROM subcategorias s
                JOIN categorias_generales c ON s.id_cat_general = c.id_cat_general
                """
            )
            subcategorias = cur.fetchall()

            for sub in subcategorias:
                nombre_sub = sub["nombre_subcategoria"].lower()
                palabras = [p for p in re.split(r"\W+", nombre_sub) if len(p) > 3]
                if any(p in texto_lower for p in palabras if p not in ["para", "como", "esta"]):
                    urgencia = sub["urgencia_default"] if sub["urgencia_default"] else "MEDIA"
                    return (
                        sub["id_cat_general"],
                        sub["id_subcategoria"],
                        urgencia.upper(),
                        sub["id_area_default"]
                    )

            # Heurísticas de respaldo si la coincidencia exacta de palabras clave falla
            for sub in subcategorias:
                nom = sub["nombre_subcategoria"].lower()
                if any(k in texto_lower for k in ["basura", "contenedor", "fuego", "desbordada"]):
                    if any(k in nom for k in ["basura", "contenedor", "residuos"]):
                        return sub["id_cat_general"], sub["id_subcategoria"], (sub["urgencia_default"] or "MEDIA").upper(), sub["id_area_default"]
                elif any(k in texto_lower for k in ["luz", "mercurio", "lampara", "luminaria", "apagada"]):
                    if any(k in nom for k in ["luminaria", "alumbrado", "luz"]):
                        return sub["id_cat_general"], sub["id_subcategoria"], (sub["urgencia_default"] or "MEDIA").upper(), sub["id_area_default"]
                elif any(k in texto_lower for k in ["árbol", "arbol", "rama", "poda", "caída", "caido"]):
                    if any(k in nom for k in ["arbol", "poda", "caída", "caida"]):
                        return sub["id_cat_general"], sub["id_subcategoria"], (sub["urgencia_default"] or "MEDIA").upper(), sub["id_area_default"]

    except Exception as err:
        print(f"⚠️ Error durante la clasificación desde BD: {err}")

    return None, None, "MEDIA", None

def guardar_reclamo_completo(
    conn: psycopg2.extensions.connection,
    datos: Dict[str, Any]
) -> None:
    """Registra atómicamente el reclamo en 'reclamos' e 'historial_estados'."""
    
    # Obtener la hora actual en zona horaria Argentina y quitar el offset implícito
    tz_arg = ZoneInfo("America/Argentina/Buenos_Aires")
    fecha_ahora = datetime.datetime.now(tz_arg).replace(tzinfo=None)

    import uuid
    id_reclamo = str(uuid.uuid4())
    codigo_seguimiento = f"WAP-{fecha_ahora.strftime('%Y%m%d')}-{id_reclamo[:4].upper()}"

    # Barrio resuelto dinámicamente con PostGIS
    barrio_real = datos.get("barrio_extraido") or get_barrio_postgis(conn, datos.get("lat"), datos.get("lon"))

    hash_ciudadano = obtener_o_crear_ciudadano_hash(
        conn,
        telefono_o_id=datos.get("remitente_telefono"),
        barrio=barrio_real
    )

    texto_evaluar = datos.get("texto_transcripto") or datos.get("texto_original") or ""
    id_cat_general, id_subcategoria, urgencia_bd, id_area_default = clasificar_y_obtener_metadatos(conn, texto_evaluar)

    direccion = datos.get("direccion_extraida")
    es_direccion_valida = direccion and "a confirmar" not in direccion.lower()
    
    if id_cat_general and id_subcategoria and es_direccion_valida:
        estado_reclamo = "INGRESADO"
        id_area_asignada = id_area_default
        comentario_historial = f"Reclamo asignado automáticamente a área ID {id_area_asignada}."
    else:
        estado_reclamo = "PENDIENTE_REVISION"
        id_area_asignada = None
        comentario_historial = "Reclamo derivado a revisión manual por datos incompletos o baja confianza."

    geom_sql = (
        f"ST_SetSRID(ST_MakePoint({datos['lon']}, {datos['lat']}), 4326)"
        if datos.get("lat") and datos.get("lon")
        else "NULL"
    )

    sql_reclamo = f"""
        INSERT INTO reclamos (
            id_reclamo, codigo_seguimiento, id_ciudadano_hash, id_canal,
            id_cat_general, id_subcategoria, id_area_asignada, texto_original, texto_transcripto,
            url_audio, url_foto, direccion_extraida, barrio_extraido,
            urgencia, confianza_clasificacion, confianza_subcategoria,
            precision_geocoding, geom, lat, lon, fecha_ingreso,
            fecha_ultima_actualizacion, estado
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            {geom_sql}, %s, %s, %s, %s, %s
        )
    """

    urgencia_normalizada = (urgencia_bd or "MEDIA").upper()

    params_reclamo = (
        id_reclamo,
        codigo_seguimiento,
        hash_ciudadano,
        1,  # Canal WhatsApp
        id_cat_general,
        id_subcategoria,
        id_area_asignada,
        datos.get("texto_original"),
        datos.get("texto_transcripto"),
        datos.get("url_audio"),
        datos.get("url_foto"),
        direccion,
        barrio_real,
        urgencia_normalizada,
        datos.get("confianza_clasificacion", 0.85 if id_cat_general else 0.40),
        datos.get("confianza_subcategoria", 0.85 if id_subcategoria else 0.40),
        datos.get("precision_geocoding", "Sin Geolocalizar"),
        datos.get("lat"),
        datos.get("lon"),
        fecha_ahora,
        fecha_ahora,
        estado_reclamo
    )

    sql_historial = """
        INSERT INTO historial_estados (
            id_reclamo, estado_anterior, estado_nuevo, fecha_cambio, responsable, comentario
        ) VALUES (%s, %s, %s, %s, %s, %s)
    """

    params_historial = (
        id_reclamo,
        None,
        estado_reclamo,
        fecha_ahora,
        "Sistema Automatizado WhatsApp",
        f"Código {codigo_seguimiento}: {comentario_historial}"
    )

    with conn.cursor() as cur:
        cur.execute(sql_reclamo, params_reclamo)
        cur.execute(sql_historial, params_historial)

    conn.commit()
    print(f"  ✅ Reclamo {codigo_seguimiento} [{estado_reclamo}] en barrio '{barrio_real}' guardado a las {fecha_ahora.strftime('%H:%M:%S')}.")

def procesar_inbox(directorio_inbox: str) -> None:
    """Orquesta el procesamiento iterativo de archivos recibidos."""
    inbox_path = Path(directorio_inbox)
    procesados_path = inbox_path / "procesados"
    procesados_path.mkdir(exist_ok=True)

    if not inbox_path.exists():
        print(f"❌ La carpeta '{directorio_inbox}' no existe.")
        return

    print("🔌 Verificando conexión a PostgreSQL/PostGIS...")
    conn = obtener_conexion()
    print("  -> Conexión a BD establecida correctamente.")

    print("🎙️ Cargando modelo Whisper ('small')...")
    model_whisper = whisper.load_model("small")

    archivos = [f for f in inbox_path.iterdir() if f.is_file()]
    print(f"📂 Archivos pendientes a procesar: {len(archivos)}")

    for archivo in archivos:
        ext = archivo.suffix.lower()
        nombre_base = archivo.stem

        if ext == ".txt" and (inbox_path / f"{nombre_base}.jpg").exists():
            continue

        remitente_tel = (
            nombre_base.split("_")[0]
            if "_" in nombre_base and nombre_base.split("_")[0].isdigit()
            else "whatsapp_user"
        )

        datos: Dict[str, Any] = {
            "remitente_telefono": remitente_tel,
            "url_audio": None,
            "url_foto": None,
            "texto_original": None,
            "texto_transcripto": None
        }

        try:
            # 1. MENSAJES DE AUDIO
            if ext in [".ogg", ".mp3", ".wav", ".m4a", ".oga"]:
                print(f"\n=== Procesando Audio: {archivo.name} ===")
                res = model_whisper.transcribe(str(archivo))
                transcripcion = res.get("text", "").strip()
                print(f"  -> Transcripción: {transcripcion}")

                dir_ext, barrio_ext, lat, lon, prec = extraer_geometria_y_barrio_gratuito(conn, transcripcion)
                datos.update({
                    "url_audio": archivo.name,
                    "texto_original": "[Mensaje de voz de WhatsApp]",
                    "texto_transcripto": transcripcion,
                    "direccion_extraida": dir_ext,
                    "barrio_extraido": barrio_ext,
                    "lat": lat,
                    "lon": lon,
                    "precision_geocoding": prec
                })

            # 2. MENSAJES DE TEXTO PURO (.txt)
            elif ext == ".txt":
                print(f"\n=== Procesando Texto: {archivo.name} ===")
                with open(archivo, "r", encoding="utf-8") as f:
                    contenido = f.read().strip()

                dir_ext, barrio_ext, lat, lon, prec = extraer_geometria_y_barrio_gratuito(conn, contenido)
                datos.update({
                    "texto_original": contenido,
                    "texto_transcripto": None,
                    "direccion_extraida": dir_ext,
                    "barrio_extraido": barrio_ext,
                    "lat": lat,
                    "lon": lon,
                    "precision_geocoding": prec
                })

            # 3. IMÁGENES (.jpg, .jpeg, .png, .webp)
            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                print(f"\n=== Procesando Imagen: {archivo.name} ===")
                txt_asociado = archivo.with_suffix(".txt")
                txt_contenido = ""
                
                if txt_asociado.exists():
                    with open(txt_asociado, "r", encoding="utf-8") as f:
                        txt_contenido = f.read().strip()
                    txt_asociado.rename(procesados_path / txt_asociado.name)

                if txt_contenido:
                    dir_ext, barrio_ext, lat, lon, prec = extraer_geometria_y_barrio_gratuito(conn, txt_contenido)
                else:
                    dir_ext, barrio_ext, lat, lon, prec = "Avellaneda (Foto adjunta)", "Sarandí", -34.6865, -58.3486, "Pendiente Imagen"

                datos.update({
                    "url_foto": archivo.name,
                    "texto_original": txt_contenido if txt_contenido else "[Imagen adjunta enviada vía WhatsApp]",
                    "texto_transcripto": None,
                    "direccion_extraida": dir_ext,
                    "barrio_extraido": barrio_ext,
                    "lat": lat,
                    "lon": lon,
                    "precision_geocoding": prec
                })

            else:
                continue

            guardar_reclamo_completo(conn, datos)
            archivo.rename(procesados_path / archivo.name)

        except Exception as err:
            print(f"❌ Error procesando {archivo.name}: {err}")
            conn.rollback()

    conn.close()
    print("\n[OK] Inbox procesado e integrado exitosamente con PostGIS.")



def procesar_inbox_daemon(directorio_inbox: str = "whatsapp_inbox"):
    inbox_path = Path(directorio_inbox)
    print("🚀 [MuniIA Worker] Iniciando monitoreo continuo...")
    conn = obtener_conexion()
    model_whisper = whisper.load_model("small")
    while True:
        try:
            # Reusa la lógica de procesar_inbox pero sin cerrar conexión
            archivos = [f for f in inbox_path.iterdir() if f.is_file() and not f.name.endswith('.tmp')]
            for archivo in archivos:
                # ... toda tu lógica de audio/texto/imagen + guardar_reclamo_completo ...
                parser = argparse.ArgumentParser(description="Procesador de Inbox WhatsApp a PostGIS")
                parser.add_argument("--input", default="whatsapp_inbox", help="Carpeta de entrada")
                args = parser.parse_args()

                procesar_inbox(args.input)
                pass
        except Exception as err:
            print(f"❌ Loop Error: {err}")
#        datetime.time.sleep(5)

if __name__ == "__main__":
#    parser = argparse.ArgumentParser(description="Procesador de Inbox WhatsApp a PostGIS")
#    parser.add_argument("--input", default="whatsapp_inbox", help="Carpeta de entrada")
#    args = parser.parse_args()

    procesar_inbox_daemon()
#    print("\n[OK] Listo. Refresca dashboard en http://localhost:8501")
