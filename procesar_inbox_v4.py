"""
Módulo de Procesamiento Automatizado de Inbox (WhatsApp a PostGIS).

Este script monitorea la carpeta 'whatsapp_inbox', transcribe mensajes de audio
mediante OpenAI Whisper, extrae datos de texto e imágenes, aplica Fuzzy Matching
con pg_trgm sobre calles_avellaneda, anonimiza al ciudadano mediante hashing SHA-256,
clasifica el reclamo consultando la base de datos, construye el mensaje de respuesta al vecino
y guarda el registro junto con su trazabilidad en el historial de forma transaccional aislada.

Cumple con especificaciones PEP 8 y buenas prácticas de auditoría.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Directorios de Inbox
INBOX_DIR = BASE_DIR / "whatsapp_inbox"
PROCESADOS_DIR = INBOX_DIR / "procesados"

INBOX_DIR.mkdir(parents=True, exist_ok=True)
PROCESADOS_DIR.mkdir(parents=True, exist_ok=True)

# Configuración de Nominatim para Avellaneda
AVELLANEDA_VIEWBOX = "-58.38,-34.72,-58.30,-34.64"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "MuniIA_MVP_Avellaneda/2.0 (contacto_muniia@avellaneda.gob.ar)"

# --- CONFIGURACIÓN DE CONEXIÓN A BASE DE DATOS ---
DB_PASS = os.getenv("DB_PASS")
if not DB_PASS:
    print("⚠️ Advertencia: DB_PASS no está definida en .env, usando valor predeterminado 'postgres'")
    DB_PASS = "postgres"

DB_CONFIG: Dict[str, str] = {
    "dbname": os.getenv("DB_NAME", "muniia"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": DB_PASS,
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


def corregir_calle_fuzzy(conn: psycopg2.extensions.connection, calle_cruda: str) -> str:
    """Corrige errores fonéticos de transcripción usando pg_trgm sobre la tabla calles_avellaneda."""
    if not calle_cruda or len(calle_cruda.strip()) < 3 or calle_cruda.upper() == "DESCONOCIDO":
        return calle_cruda

    query = """
        SELECT nombre, similarity(LOWER(nombre), LOWER(%s)) as sim
        FROM public.calles_avellaneda
        WHERE similarity(LOWER(nombre), LOWER(%s)) > 0.35
        ORDER BY sim DESC
        LIMIT 1;
    """
    try:
        with conn.cursor() as cur:
            cur.execute(query, (calle_cruda, calle_cruda))
            row = cur.fetchone()
            if row:
                print(f"🔍 [Fuzzy Match]: '{calle_cruda}' -> Corregido a: '{row[0]}' (Similitud: {row[1]:.2f})")
                return row[0]
    except Exception as err:
        print(f"⚠️ Error en fuzzy matching: {err}")
        conn.rollback()

    return calle_cruda


def leer_metadata(meta_path: Path) -> Tuple[Optional[str], str]:
    """Lee el remoto_jid y el texto adicional del archivo .txt adjunto sin colapsar por codificación de bytes."""
    remoto_jid, texto_extra = None, ""
    if meta_path.exists() and meta_path.suffix.lower() == ".txt":
        try:
            contenido = meta_path.read_text(encoding="utf-8", errors="ignore").strip()
            lines = contenido.split("\n")
            for line in lines:
                if line.startswith("JID:"):
                    remoto_jid = line.replace("JID:", "").strip()
                else:
                    texto_extra += line + "\n"
        except Exception as err:
            print(f"⚠️ Error leyendo metadatos de {meta_path.name}: {err}")
            
    return remoto_jid, texto_extra.strip()


def get_barrio_postgis(conn: psycopg2.extensions.connection, lat: Optional[float], lon: Optional[float]) -> str:
    """Determina el barrio realizando una consulta espacial ST_Contains sobre barrios_avellaneda."""
    if lat is None or lon is None:
        return "Avellaneda Centro"

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
    """Geocodifica una dirección en Nominatim (OSM) delimitada a Avellaneda."""
    if not direccion_texto or "a confirmar" in direccion_texto.lower():
        return None, None, "Sin Geolocalizar"

    query = f"{direccion_texto}, Avellaneda, Buenos Aires, Argentina"
    params = {
        "q": query,
        "format": "jsonv2",
        "bounded": 1,
        "viewbox": AVELLANEDA_VIEWBOX,
        "limit": 1,
        "countrycodes": "ar",
        "addressdetails": 1
    }
    
    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "es-AR,es;q=0.9"
    }
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                if data and len(data) > 0:
                    item = data[0]
                    lat = float(item["lat"])
                    lon = float(item["lon"])
                    importance = float(item.get("importance", 0))
                    
                    precision = "Dirección con Altura" if importance >= 0.3 else "Aproximada"
                    return lat, lon, precision
    except Exception as err:
        print(f"⚠️ Error al consultar Nominatim OSM: {err}")

    return None, None, "Sin Geolocalizar"


def obtener_o_crear_ciudadano_hash(
    conn: psycopg2.extensions.connection,
    telefono_o_id: Optional[str],
    barrio: str = "Sarandí"
) -> str:
    """Genera hash SHA-256 del JID/Teléfono para anonimizar la identidad del vecino."""
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
    conn.commit()
    return hash_ciudadano


def resolver_calle_exacta_db(conn: psycopg2.extensions.connection, palabra: str) -> Optional[str]:
    """Valida un token o nombre contra la base de datos completa de calles_avellaneda."""
    if not palabra or len(palabra.strip()) < 3:
        return None

    ignorar = {
        "calle", "avenida", "av", "esquina", "esq", "entre", "cerca", "es", "para", 
        "hacer", "reclamo", "denuncia", "choque", "accidente", "sobre", "gracias", 
        "buenas", "tardes", "dias", "noches", "funciona", "unciona", "semaforo", "semáforo"
    }
    
    if palabra.lower().strip() in ignorar:
        return None

    query = """
        SELECT nombre, similarity(LOWER(nombre), LOWER(%s)) as sim
        FROM public.calles_avellaneda
        WHERE similarity(LOWER(nombre), LOWER(%s)) > 0.40
        ORDER BY sim DESC
        LIMIT 1;
    """
    try:
        with conn.cursor() as cur:
            cur.execute(query, (palabra.strip(), palabra.strip()))
            row = cur.fetchone()
            if row:
                return row[0]
    except Exception as err:
        conn.rollback()

    return None


def extraer_geometria_y_barrio_gratuito(
    conn: psycopg2.extensions.connection, 
    texto: str
) -> Tuple[str, str, float, float, str]:
    """
    Extrae esquinas o altura validando directamente contra la red vial completa de Avellaneda.
    """
    direccion_limpia = "Avellaneda (Dirección a confirmar)"
    lat, lon = -34.6865, -58.3486
    precision = "Sin Geolocalizar"

    if not texto or len(texto.strip()) < 5:
        barrio_detectado = get_barrio_postgis(conn, lat, lon)
        return direccion_limpia, barrio_detectado, lat, lon, precision

    tokens = re.findall(r"\b\w+\b", texto)

    # --- CASO A: DETECCIÓN DE ESQUINAS E INTERSECCIONES ---
    for i, token in enumerate(tokens):
        if token.lower() in ["y", "esq", "esquina"] and 0 < i < len(tokens) - 1:
            c1_cand = tokens[i - 1]
            calle_1 = resolver_calle_exacta_db(conn, c1_cand)
            if not calle_1 and i - 2 >= 0:
                calle_1 = resolver_calle_exacta_db(conn, f"{tokens[i-2]} {tokens[i-1]}")

            c2_cand = tokens[i + 1]
            calle_2 = resolver_calle_exacta_db(conn, c2_cand)
            if not calle_2 and i + 2 < len(tokens):
                calle_2 = resolver_calle_exacta_db(conn, f"{tokens[i+1]} {tokens[i+2]}")

            if calle_1 and calle_2:
                direccion_limpia = f"{calle_1} y {calle_2}"
                lat_osm, lon_osm, prec_osm = geocodificar_nominatim(f"{calle_1} y {calle_2}")
                if lat_osm and lon_osm:
                    lat, lon, precision = lat_osm, lon_osm, "Esquina / Intersección"
                else:
                    lat_c, lon_c, _ = geocodificar_nominatim(calle_1)
                    if lat_c and lon_c:
                        lat, lon, precision = lat_c, lon_c, "Aproximada"

                barrio_detectado = get_barrio_postgis(conn, lat, lon)
                return direccion_limpia, barrio_detectado, lat, lon, precision

    # --- CASO B: DETECCIÓN DE CALLE + ALTURA ---
    for i, token in enumerate(tokens):
        if token.isdigit() and 2 <= len(token) <= 5:
            altura = token
            for offset in [1, 2, 3]:
                if i - offset >= 0:
                    candidato_texto = " ".join(tokens[i - offset : i])
                    calle_encontrada = resolver_calle_exacta_db(conn, candidato_texto)
                    if calle_encontrada:
                        direccion_limpia = f"{calle_encontrada} {altura}"
                        lat_osm, lon_osm, prec_osm = geocodificar_nominatim(direccion_limpia)
                        if lat_osm and lon_osm:
                            lat, lon, precision = lat_osm, lon_osm, prec_osm
                        else:
                            lat_calle, lon_calle, _ = geocodificar_nominatim(calle_encontrada)
                            if lat_calle and lon_calle:
                                lat, lon, precision = lat_calle, lon_calle, "Aproximada"

                        barrio_detectado = get_barrio_postgis(conn, lat, lon)
                        return direccion_limpia, barrio_detectado, lat, lon, precision

    barrio_detectado = get_barrio_postgis(conn, lat, lon)
    return direccion_limpia, barrio_detectado, lat, lon, precision

def clasificar_y_obtener_metadatos(
    conn: psycopg2.extensions.connection,
    texto: Optional[str]
) -> Tuple[Optional[int], Optional[int], str, Optional[int], Optional[str], Optional[str]]:
    """
    Clasifica el texto según coincidencia ponderada de tokens entre el mensaje
    y el título/descripción de las subcategorías (sin palabras clave hardcodeadas).
    """
    if not texto or len(texto.strip()) < 3:
        return None, None, "MEDIA", None, None, None

    stopwords = {
        "para", "como", "esta", "este", "sobre", "hacer", "reclamo", "informar", 
        "consulta", "buenas", "tardes", "dias", "noches", "favor", "necesito", 
        "queria", "saber", "tener", "local", "lugar", "zona", "denuncia", "que",
        "hubo", "hacer", "calle", "avellaneda", "gracias", "unas", "unos"
    }

    tokens_texto = set(
        p.lower() for p in re.split(r"\W+", texto) 
        if len(p) > 2 and p.lower() not in stopwords
    )

    if not tokens_texto:
        return None, None, "MEDIA", None, None, None

    subcategoria_ganadora = None
    max_score = 0

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT 
                    s.id_subcategoria, 
                    s.id_cat_general, 
                    s.nombre_subcategoria, 
                    s.urgencia_default,
                    s.descripcion,
                    c.id_area_default,
                    c.nombre_general AS nombre_cat_general
                FROM subcategorias s
                JOIN categorias_generales c ON s.id_cat_general = c.id_cat_general
            """
            cur.execute(query)
            subcategorias = cur.fetchall()

            for sub in subcategorias:
                nombre_sub = (sub["nombre_subcategoria"] or "").lower()
                desc_sub = (sub["descripcion"] or "").lower()

                tokens_nombre = set(p for p in re.split(r"\W+", nombre_sub) if len(p) > 2 and p not in stopwords)
                tokens_desc = set(p for p in re.split(r"\W+", desc_sub) if len(p) > 2 and p not in stopwords)

                # Ponderación estructural pura
                match_nombre = tokens_texto.intersection(tokens_nombre)
                match_desc = tokens_texto.intersection(tokens_desc) - match_nombre

                score = (len(match_nombre) * 3) + len(match_desc)

                if score > max_score:
                    max_score = score
                    subcategoria_ganadora = sub

            if subcategoria_ganadora and max_score > 0:
                urgencia = subcategoria_ganadora["urgencia_default"] or "MEDIA"
                return (
                    subcategoria_ganadora["id_cat_general"],
                    subcategoria_ganadora["id_subcategoria"],
                    urgencia.upper(),
                    subcategoria_ganadora["id_area_default"],
                    subcategoria_ganadora["nombre_cat_general"],
                    subcategoria_ganadora["nombre_subcategoria"]
                )

    except Exception as err:
        print(f"⚠️ Error durante la clasificación por scoring en BD: {err}")
        try:
            conn.rollback()
        except Exception:
            pass

    return None, None, "MEDIA", None, None, None

def guardar_reclamo_completo(
    conn: psycopg2.extensions.connection,
    datos: Dict[str, Any]
) -> None:
    """Registra atómicamente el reclamo en las tablas reclamos e historial_estados junto con la respuesta al vecino."""
    tz_arg = ZoneInfo("America/Argentina/Buenos_Aires")
    fecha_ahora = datetime.datetime.now(tz_arg).replace(tzinfo=None)

    import uuid
    id_reclamo = str(uuid.uuid4())
    codigo_seguimiento = f"WAP-{fecha_ahora.strftime('%Y%m%d')}-{id_reclamo[:4].upper()}"

    barrio_real = datos.get("barrio_extraido") or get_barrio_postgis(conn, datos.get("lat"), datos.get("lon"))

    hash_ciudadano = obtener_o_crear_ciudadano_hash(
        conn,
        telefono_o_id=datos.get("remoto_jid") or datos.get("remitente_telefono"),
        barrio=barrio_real
    )

    texto_evaluar = datos.get("texto_transcripto") or datos.get("texto_original") or ""
    id_cat_general, id_subcategoria, urgencia_bd, id_area_default, nombre_cat, nombre_sub = clasificar_y_obtener_metadatos(conn, texto_evaluar)

    direccion = datos.get("direccion_extraida")
    es_direccion_valida = direccion and "a confirmar" not in direccion.lower()
    
    if id_cat_general and id_subcategoria and es_direccion_valida:
        estado_reclamo = "INGRESADO"
        id_area_asignada = id_area_default
        comentario_historial = f"Reclamo asignado automáticamente a área ID {id_area_asignada}."
        
        # Plantilla Estado INGRESADO
        mensaje_respuesta = (
            f"¡Hola! Gracias por comunicarse con MUNI-IA Avellaneda.\n\n"
            f"Su reclamo fue registrado con éxito.\n"
            f"📌 Código: {codigo_seguimiento}\n"
            f"🏢 Sector: {nombre_cat or 'General'} ({nombre_sub or 'General'})\n"
            f"📍 Ubicación: {direccion} ({barrio_real})\n\n"
            f"Estamos coordinando la solución con el área correspondiente."
        )
    else:
        estado_reclamo = "PENDIENTE_REVISION"
        id_area_asignada = None
        comentario_historial = "Reclamo derivado a revisión manual por datos incompletos o baja confianza."
        
        # Plantilla Estado PENDIENTE_REVISION
        mensaje_respuesta = (
            f"¡Hola! Gracias por comunicarse con MUNI-IA Avellaneda.\n\n"
            f"Hemos recibido su mensaje (Código provisorio: {codigo_seguimiento}).\n"
            f"Un operador municipal revisará la información o se contactará a la brevedad "
            f"para precisar la ubicación o solicitar datos adicionales.\n\n"
            f"¡Muchas gracias por su colaboración!"
        )

    geom_sql = (
        f"ST_SetSRID(ST_MakePoint({datos['lon']}, {datos['lat']}), 4326)"
        if datos.get("lat") and datos.get("lon")
        else "NULL"
    )

    sql_reclamo = f"""
        INSERT INTO reclamos (
            id_reclamo, codigo_seguimiento, id_ciudadano_hash, remoto_jid, id_canal,
            id_cat_general, id_subcategoria, id_area_asignada, texto_original, texto_transcripto,
            url_audio, url_foto, direccion_extraida, barrio_extraido,
            urgencia, confianza_clasificacion, confianza_subcategoria,
            precision_geocoding, geom, lat, lon, fecha_ingreso,
            fecha_ultima_actualizacion, estado, mensaje_respuesta, respuesta_enviada
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            {geom_sql}, %s, %s, %s, %s, %s, %s, %s
        )
    """

    urgencia_normalizada = (urgencia_bd or "MEDIA").upper()

    params_reclamo = (
        id_reclamo,
        codigo_seguimiento,
        hash_ciudadano,
        datos.get("remoto_jid"),
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
        estado_reclamo,
        mensaje_respuesta,
        False  # respuesta_enviada por defecto
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
    print(f"  ✅ Reclamo {codigo_seguimiento} [{estado_reclamo}] guardado con mensaje preparado para el vecino.")

def procesar_inbox_daemon():
    """Ejecuta el demonio de monitoreo continuo sobre la carpeta whatsapp_inbox."""
    conn = None
    print("🚀 [MuniIA Worker] Iniciando monitoreo continuo de inbox...")

    print("🎙️ Cargando modelo OpenAI Whisper ('small')...")
    model_whisper = whisper.load_model("small")

    while True:
        try:
            if not conn or conn.closed != 0:
                conn = obtener_conexion()

            todos_los_archivos = [
                f for f in INBOX_DIR.iterdir() 
                if f.is_file() and f.parent == INBOX_DIR and not f.name.endswith('.tmp')
            ]

            for archivo in todos_los_archivos:
                ext = archivo.suffix.lower()

                if ext == ".txt":
                    nombre_base = archivo.stem
                    if any(f.name == nombre_base for f in todos_los_archivos):
                        continue

                print(f"\n📂 Procesando: {archivo.name}")

                try:
                    meta_txt = archivo if ext == ".txt" else INBOX_DIR / f"{archivo.name}.txt"
                    remoto_jid, texto_meta = leer_metadata(meta_txt)

                    datos: Dict[str, Any] = {
                        "remoto_jid": remoto_jid,
                        "remitente_telefono": remoto_jid.split("@")[0] if remoto_jid else "whatsapp_user",
                        "url_audio": None,
                        "url_foto": None,
                        "texto_original": None,
                        "texto_transcripto": None
                    }

                    if ext in [".ogg", ".mp3", ".wav", ".m4a", ".oga"]:
                        print(f"  🎙️ Transcribiendo audio {archivo.name} con Whisper...")
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

                    elif ext == ".txt":
                        contenido = texto_meta or archivo.read_text(encoding="utf-8", errors="ignore").strip()
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

                    elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        if texto_meta:
                            dir_ext, barrio_ext, lat, lon, prec = extraer_geometria_y_barrio_gratuito(conn, texto_meta)
                        else:
                            dir_ext, barrio_ext, lat, lon, prec = "Avellaneda (Foto adjunta)", "Sarandí", -34.6865, -58.3486, "Pendiente Imagen"

                        datos.update({
                            "url_foto": archivo.name,
                            "texto_original": texto_meta if texto_meta else "[Imagen adjunta enviada vía WhatsApp]",
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

                    archivo.rename(PROCESADOS_DIR / archivo.name)
                    if meta_txt.exists() and meta_txt != archivo:
                        meta_txt.rename(PROCESADOS_DIR / meta_txt.name)

                    print(f"✅ Procesado e insertado en BD: {archivo.name}")

                except Exception as err_archivo:
                    print(f"❌ Error procesando {archivo.name}: {err_archivo}")
                    if conn:
                        conn.rollback()

        except Exception as err_loop:
            print(f"❌ Error en bucle daemon: {err_loop}")
            if conn:
                conn.rollback()

        time.sleep(5)

if __name__ == "__main__":
    procesar_inbox_daemon()