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
import difflib
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
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

# Nominatim espera el viewbox como izquierda, arriba, derecha, abajo.
AVELLANEDA_VIEWBOX = "-58.45,-34.60,-58.23,-34.73"
AVELLANEDA_BOUNDS = (-58.45, -34.73, -58.23, -34.60)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "MuniIA_MVP_Avellaneda/2.0 (contacto_muniia@avellaneda.gob.ar)"
_ULTIMA_CONSULTA_NOMINATIM = 0.0

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
    """Determina el barrio por ubicación; no inventa uno si faltan coordenadas."""
    if lat is None or lon is None:
        return "A confirmar"

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nombre 
                FROM public.barrios_avellaneda
                WHERE ST_Covers(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                ORDER BY ST_Area(geom) ASC
                LIMIT 1
            """, (lon, lat))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
            else:
                return "A confirmar"
    except Exception as e:
        print(f"⚠️ [ERROR] Barrio PostGIS: {e}")
        conn.rollback()
        return "A confirmar"


def _esperar_nominatim() -> None:
    """Respeta el límite de una solicitud por segundo del servicio público."""
    global _ULTIMA_CONSULTA_NOMINATIM
    espera = 1.0 - (time.monotonic() - _ULTIMA_CONSULTA_NOMINATIM)
    if espera > 0:
        time.sleep(espera)
    _ULTIMA_CONSULTA_NOMINATIM = time.monotonic()


def _es_localidad_de_avellaneda(address: Dict[str, Any]) -> bool:
    localidades = (
        address.get(key, "")
        for key in ("municipality", "county", "city", "town", "city_district", "suburb")
    )
    return any(
        isinstance(localidad, str) and "avellaneda" in localidad.lower()
        for localidad in localidades
    )


def _extraer_barrio_de_address(address: Dict[str, Any]) -> Optional[str]:
    for key in ("neighbourhood", "suburb", "borough", "city_district"):
        localidad = address.get(key)
        if (
            isinstance(localidad, str)
            and localidad.strip()
            and localidad.strip().lower() not in {
                "avellaneda", "partido de avellaneda", "municipio de avellaneda"
            }
        ):
            return localidad.strip()
    return None


def geocodificar_barrio_nominatim(lat: float, lon: float) -> Optional[str]:
    """Busca el barrio de un punto con geocodificación inversa de Nominatim."""
    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "zoom": 17,
        "addressdetails": 1,
    }
    url = f"{NOMINATIM_REVERSE_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-AR,es;q=0.9",
        },
    )

    _esperar_nominatim()
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status != 200:
                return None
            data = json.loads(response.read().decode("utf-8"))
            address = data.get("address") or {}
            if not _es_localidad_de_avellaneda(address):
                return None
            return _extraer_barrio_de_address(address)
    except Exception as err:
        print(f"⚠️ Error al consultar barrio en Nominatim: {err}")
        return None


def geocodificar_nominatim(
    direccion_texto: str
) -> Tuple[Optional[float], Optional[float], str, Optional[str]]:
    """Geocodifica una dirección y acepta solo resultados identificados como Avellaneda."""
    global _ULTIMA_CONSULTA_NOMINATIM

    if not direccion_texto or "a confirmar" in direccion_texto.lower():
        return None, None, "Sin Geolocalizar", None

    query = f"{direccion_texto}, Avellaneda, Buenos Aires, Argentina"
    params = {
        "q": query,
        "format": "jsonv2",
        "bounded": 1,
        "viewbox": AVELLANEDA_VIEWBOX,
        "limit": 5,
        "countrycodes": "ar",
        "addressdetails": 1
    }

    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "es-AR,es;q=0.9"
    }
    req = urllib.request.Request(url, headers=headers)
    resultado = None

    _esperar_nominatim()
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                for item in data:
                    lat = float(item["lat"])
                    lon = float(item["lon"])
                    min_lon, min_lat, max_lon, max_lat = AVELLANEDA_BOUNDS
                    if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
                        continue

                    address = item.get("address") or {}
                    if not _es_localidad_de_avellaneda(address):
                        continue

                    precision = (
                        "Dirección con Altura"
                        if address.get("house_number")
                        else "Aproximada"
                    )
                    resultado = (
                        lat,
                        lon,
                        precision,
                        _extraer_barrio_de_address(address),
                    )
                    break
    except Exception as err:
        print(f"⚠️ Error al consultar Nominatim OSM: {err}")

    if resultado:
        lat, lon, precision, barrio = resultado
        if not barrio:
            barrio = geocodificar_barrio_nominatim(lat, lon)
        return lat, lon, precision, barrio

    return None, None, "Sin Geolocalizar", None


def obtener_o_crear_ciudadano_hash(
    conn: psycopg2.extensions.connection,
    telefono_o_id: Optional[str],
    barrio: str = "A confirmar"
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
                    barrio_inferido = COALESCE(NULLIF(%s, 'A confirmar'), barrio_inferido)
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


def _candidatos_calle(tokens: list, desde_final: bool) -> list:
    """Genera frases cortas contiguas para buscar nombres de calles cerca de un marcador."""
    ignorar = {
        "al", "altura", "avenida", "av", "calle", "con", "esquina", "nro", "numero",
        "número", "por", "sobre",
    }
    palabras = list(tokens)
    while palabras and palabras[0].lower() in ignorar:
        palabras.pop(0)
    while palabras and palabras[-1].lower() in ignorar:
        palabras.pop()
    if desde_final:
        palabras = palabras[-5:]
    else:
        palabras = palabras[:5]

    candidatos = []
    for cantidad in range(1, len(palabras) + 1):
        frase = (
            palabras[-cantidad:] if desde_final else palabras[:cantidad]
        )
        candidato = " ".join(frase).strip()
        if len(candidato) >= 3:
            candidatos.append(candidato)
    return candidatos


def _resolver_calle_db(
    conn: psycopg2.extensions.connection,
    candidatos: list
) -> Optional[str]:
    """Busca la mejor coincidencia de las frases candidatas en el callejero municipal."""
    candidatos = list(dict.fromkeys(candidatos))
    if not candidatos:
        return None

    query = """
        SELECT candidatos.orden, coincidencia.nombre, coincidencia.similitud
        FROM unnest(%s::text[]) WITH ORDINALITY AS candidatos(frase, orden)
        CROSS JOIN LATERAL (
            SELECT nombre, similarity(LOWER(nombre), LOWER(candidatos.frase)) AS similitud
            FROM public.calles_avellaneda
            WHERE similarity(LOWER(nombre), LOWER(candidatos.frase)) >= 0.45
            ORDER BY similitud DESC
            LIMIT 1
        ) AS coincidencia
        ORDER BY coincidencia.similitud DESC, LENGTH(candidatos.frase) DESC
        LIMIT 1
    """
    try:
        with conn.cursor() as cur:
            cur.execute(query, (candidatos,))
            row = cur.fetchone()
            if row and row[2] >= 0.50:
                return row[1]
    except Exception as err:
        print(f"⚠️ Error buscando la calle en el callejero: {err}")
        conn.rollback()
    return None


def extraer_geometria_y_barrio_gratuito(
    conn: psycopg2.extensions.connection, 
    texto: str
) -> Tuple[str, str, Optional[float], Optional[float], str]:
    """
    Extrae esquinas o altura contrastando frases completas con el callejero municipal.
    """
    direccion_limpia = "Avellaneda (Dirección a confirmar)"
    lat, lon = None, None
    precision = "Sin Geolocalizar"

    if not texto or len(texto.strip()) < 5:
        barrio_detectado = get_barrio_postgis(conn, lat, lon)
        return direccion_limpia, barrio_detectado, lat, lon, precision

    tokens = re.findall(r"\b[\w]+\b", texto, flags=re.UNICODE)

    # --- CASO A: DETECCIÓN DE ESQUINAS E INTERSECCIONES ---
    for i, token in enumerate(tokens):
        if token.lower() in ["y", "esq", "esquina"] and 0 < i < len(tokens) - 1:
            candidatos_1 = _candidatos_calle(tokens[:i], desde_final=True)
            candidatos_2 = _candidatos_calle(tokens[i + 1:], desde_final=False)
            calle_1 = _resolver_calle_db(conn, candidatos_1)
            calle_2 = _resolver_calle_db(conn, candidatos_2)

            if calle_1 and calle_2:
                direccion_limpia = f"{calle_1} y {calle_2}"
                lat_osm, lon_osm, _, barrio_osm = geocodificar_nominatim(direccion_limpia)
                if lat_osm is not None and lon_osm is not None:
                    lat, lon, precision = lat_osm, lon_osm, "Esquina / Intersección"

                barrio_detectado = barrio_osm or get_barrio_postgis(conn, lat, lon)
                return direccion_limpia, barrio_detectado, lat, lon, precision

    # --- CASO B: DETECCIÓN DE CALLE + ALTURA ---
    for i, token in enumerate(tokens):
        if token.isdigit() and 2 <= len(token) <= 5:
            altura = token
            candidatos = _candidatos_calle(tokens[:i], desde_final=True)
            calle_encontrada = _resolver_calle_db(conn, candidatos)
            if not calle_encontrada:
                candidatos = _candidatos_calle(tokens[i + 1:], desde_final=False)
                calle_encontrada = _resolver_calle_db(conn, candidatos)
            if calle_encontrada:
                direccion_limpia = f"{calle_encontrada} {altura}"
                lat, lon, precision, barrio_osm = geocodificar_nominatim(direccion_limpia)
                barrio_detectado = barrio_osm or get_barrio_postgis(conn, lat, lon)
                return direccion_limpia, barrio_detectado, lat, lon, precision

    barrio_detectado = get_barrio_postgis(conn, lat, lon)
    return direccion_limpia, barrio_detectado, lat, lon, precision

def _variantes_token_clasificacion(token: str) -> set:
    """Normaliza acentos y variantes comunes de singular/plural en español."""
    token = "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", token.lower())
        if unicodedata.category(caracter) != "Mn"
    )
    variantes = {token}
    if len(token) > 4 and token.endswith("ces"):
        variantes.add(token[:-3] + "z")
    if len(token) > 4 and token.endswith("s"):
        variantes.add(token[:-1])
    if len(token) > 5 and token.endswith("es"):
        variantes.add(token[:-2])
    if len(token) > 5 and token.endswith(("an", "en")):
        variantes.add(token[:-1])
    if len(token) > 5 and token.endswith(("ado", "ada", "ido", "ida")):
        variantes.add(token[:-1])
    return variantes


def _tokenizar_clasificacion(texto: str, stopwords: set) -> list:
    tokens = []
    for token in re.findall(r"\w+", texto.lower(), flags=re.UNICODE):
        variantes = _variantes_token_clasificacion(token)
        if (len(token) > 2 or token in {"no", "ni"}) and not variantes.intersection(stopwords):
            tokens.append(variantes)
    return tokens


def _frases_clave(valor: Any) -> list:
    if isinstance(valor, str):
        return [frase.strip() for frase in re.split(r"[,;\n|]+", valor) if frase.strip()]
    if isinstance(valor, (list, tuple, set)):
        return [str(frase).strip() for frase in valor if str(frase).strip()]
    return []


def _cantidad_tokens_coincidentes(tokens_texto: list, texto_catalogo: str, stopwords: set) -> int:
    tokens_catalogo = _tokenizar_clasificacion(texto_catalogo, stopwords)
    unicos = {frozenset(token) for token in tokens_catalogo}
    return sum(
        any(_tokens_clasificacion_compatibles(token, token_texto) for token_texto in tokens_texto)
        for token in unicos
    )


def _tokens_clasificacion_compatibles(token_1: set, token_2: set) -> bool:
    if token_1.intersection(token_2):
        return True
    return any(
        len(palabra_1) >= 5
        and len(palabra_2) >= 5
        and difflib.SequenceMatcher(None, palabra_1, palabra_2).ratio() >= 0.84
        for palabra_1 in token_1
        for palabra_2 in token_2
    )


def _frase_coincide(frase: str, tokens_texto: list, stopwords: set) -> bool:
    tokens_frase = _tokenizar_clasificacion(frase, stopwords)
    if not tokens_frase or len(tokens_frase) > len(tokens_texto):
        return False
    for inicio in range(len(tokens_texto) - len(tokens_frase) + 1):
        if all(
            _tokens_clasificacion_compatibles(actual, esperado)
            for actual, esperado in zip(
                tokens_texto[inicio:inicio + len(tokens_frase)],
                tokens_frase
            )
        ):
            return True
    return False


def clasificar_y_obtener_metadatos(
    conn: psycopg2.extensions.connection,
    texto: Optional[str]
) -> Tuple[Optional[int], Optional[int], str, Optional[int], Optional[str], Optional[str]]:
    """Clasifica con palabras clave y evita asignar categorías de baja confianza o ambiguas."""
    if not texto or len(texto.strip()) < 3:
        return None, None, "MEDIA", None, None, None
    if "imagen adjunta" in texto.lower() or "foto adjunta" in texto.lower():
        return None, None, "MEDIA", None, None, None

    stopwords = {
        "para", "como", "esta", "este", "sobre", "hacer", "reclamo", "informar",
        "consulta", "buenas", "tardes", "dias", "noches", "favor", "necesito",
        "queria", "saber", "tener", "local", "lugar", "zona", "denuncia", "que",
        "hubo", "calle", "avellaneda", "gracias", "unas", "unos", "una", "uno",
        "los", "las", "del", "por", "con", "hay", "ahi", "aca", "esta", "estan",
        "de", "la", "el", "en", "al", "a", "y", "se", "me", "lo",
    }
    stopwords_normalizadas = set()
    for palabra in stopwords:
        stopwords_normalizadas.update(_variantes_token_clasificacion(palabra))

    tokens_texto = _tokenizar_clasificacion(texto, stopwords_normalizadas)
    if not tokens_texto:
        return None, None, "MEDIA", None, None, None

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    s.id_subcategoria,
                    s.id_cat_general,
                    s.nombre_subcategoria,
                    s.urgencia_default,
                    s.descripcion,
                    to_jsonb(s) ->> 'palabras_clave' AS palabras_clave,
                    c.id_area_default,
                    c.nombre_general AS nombre_cat_general
                FROM subcategorias s
                LEFT JOIN categorias_generales c ON s.id_cat_general = c.id_cat_general
            """)
            subcategorias = cur.fetchall()

        if not subcategorias:
            print("⚠️ No hay subcategorías disponibles para clasificar el reclamo.")
            return None, None, "MEDIA", None, None, None

        puntajes = []
        for subcategoria in subcategorias:
            frases = _frases_clave(subcategoria.get("palabras_clave"))
            tokens_clave = " ".join(frases)
            coincidencias_clave = _cantidad_tokens_coincidentes(
                tokens_texto,
                tokens_clave,
                stopwords_normalizadas
            )
            coincidencias_frase = sum(
                _frase_coincide(frase, tokens_texto, stopwords_normalizadas)
                for frase in frases
            )

            nombre = subcategoria.get("nombre_subcategoria") or ""
            descripcion = subcategoria.get("descripcion") or ""
            frases_descripcion = _frases_clave(descripcion)
            coincidencias_descripcion_frase = sum(
                _frase_coincide(frase, tokens_texto, stopwords_normalizadas)
                for frase in frases_descripcion
            )

            score = (
                min(coincidencias_clave * 2, 8)
                + min(coincidencias_frase, 2) * 3
                + min(_cantidad_tokens_coincidentes(
                    tokens_texto,
                    nombre,
                    stopwords_normalizadas
                ) * 3, 6)
                + min(_cantidad_tokens_coincidentes(
                    tokens_texto,
                    descripcion,
                    stopwords_normalizadas
                ) * 2, 8)
                + min(coincidencias_descripcion_frase, 2) * 4
            )
            if score:
                puntajes.append((score, subcategoria))

        puntajes.sort(key=lambda resultado: resultado[0], reverse=True)
        if not puntajes or puntajes[0][0] < 2:
            print("⚠️ Ninguna subcategoría del catálogo coincide con suficiente claridad.")
            return None, None, "MEDIA", None, None, None
        if len(puntajes) > 1 and puntajes[0][0] == puntajes[1][0]:
            print(
                "⚠️ Clasificación ambigua entre subcategorías: "
                f"{puntajes[0][1].get('nombre_subcategoria')} / "
                f"{puntajes[1][1].get('nombre_subcategoria')}."
            )
            return None, None, "MEDIA", None, None, None

        ganadora = puntajes[0][1]
        urgencia = ganadora.get("urgencia_default") or "MEDIA"
        return (
            ganadora["id_cat_general"],
            ganadora["id_subcategoria"],
            urgencia.upper(),
            ganadora.get("id_area_default"),
            ganadora.get("nombre_cat_general"),
            ganadora.get("nombre_subcategoria")
        )
    except Exception as err:
        print(f"⚠️ Error durante la clasificación por scoring en BD: {err}")
        conn.rollback()

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
    es_direccion_valida = (
        direccion
        and "a confirmar" not in direccion.lower()
        and datos.get("lat") is not None
        and datos.get("lon") is not None
    )
    
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
        if datos.get("lat") is not None and datos.get("lon") is not None
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