"""
MuniIA - Dashboard Operativo v4.8
Incorporate: Edición de Reclamos Ingresados, Feed Multimedia Integrado y CRUD Administrativo.
"""
import os
import json
import urllib.parse
import urllib.request
import warnings
import streamlit as st
import pandas as pd
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

# Autorefresh para actualización en tiempo real
try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

st.set_page_config(page_title="MuniIA - Centro de Control Operativo", layout="wide", page_icon="🏙️")

# Evitar autorefresh durante edición activa
if HAS_AUTOREFRESH and not any(k.startswith(("confirm_delete_", "edit_ingresado_")) and v for k, v in st.session_state.items()):
    st_autorefresh(interval=15000, limit=None, key="muniia_dashboard_autorefresh")

try:
    import folium
    from streamlit_folium import st_folium
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

INBOX_DIR = BASE_DIR / "whatsapp_inbox"
PROCESADOS_DIR = INBOX_DIR / "procesados"

DB_PASS = os.getenv("DB_PASS")
if not DB_PASS:
    st.error("❌ Error: La variable DB_PASS no está definida en el archivo .env")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME", "muniia"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": DB_PASS,
}

BARRIOS_AVELLANEDA = [
    "Avellaneda Centro", "Sarandí", "Villa Domínico", "Wilde",
    "Gerli", "Piñeyro", "Dock Sud", "Crucecita"
]

def get_conn():
    try:
        return psycopg2.connect(**DB_CONFIG, connect_timeout=4)
    except Exception as e:
        print(f"Error conexión DB: {e}")
        return None

def re_geocodificar(direccion: str):
    """Geocodifica la dirección ajustada usando Nominatim."""
    if not direccion or len(direccion.strip()) < 3 or "a confirmar" in direccion.lower():
        return None, None
    
    query_str = f"{direccion.strip()}, Avellaneda, Buenos Aires, Argentina"
    url = f"https://nominatim.openstreetmap.org/search?{urllib.parse.urlencode({'q': query_str, 'format': 'jsonv2', 'limit': 1})}"
    
    headers = {
        "User-Agent": "MuniIA_Municipal_App/2.0 (contacto_muniia@avellaneda.gob.ar)",
        "Accept-Language": "es-AR,es;q=0.9"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and len(data) > 0:
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                print(f"📍 Geocodificado OK: '{direccion}' -> Lat: {lat}, Lon: {lon}")
                return lat, lon
    except Exception as err:
        print(f"⚠️ Error en geocodificación Nominatim para '{direccion}': {err}")
        
    return None, None

def load_catalogos():
    """Carga categorías y subcategorías completas convirtiendo explícitamente los IDs a int e independizando alias de columna."""
    conn = get_conn()
    cats_dict = {}     # Nombre Categoría -> id_cat_general (int)
    subcats_dict = {}  # Nombre Subcategoría -> {"id_sub": int, "id_cat": int, "keywords": str}
    
    if not conn: 
        return cats_dict, subcats_dict
    
    try:
        cur = conn.cursor()
        
        # 1. Cargar Categorías Generales probando esquemas con COALESCE
        cur.execute("""
            SELECT id_cat_general, nombre_general as nombre 
            FROM categorias_generales 
            ORDER BY nombre;
        """)
        for row in cur.fetchall():
            if row[0] is not None and row[1]:
                id_cat = int(row[0])
                nombre_cat = str(row[1]).strip()
                cats_dict[nombre_cat] = id_cat

        # 2. Cargar Subcategorías
        cur.execute("""
            SELECT 
                id_subcategoria, 
                id_cat_general, 
                nombre_subcategoria, 
                COALESCE(descripcion, '') 
            FROM subcategorias 
            ORDER BY nombre_subcategoria;
        """)
        for row in cur.fetchall():
            if row[0] is not None and row[1] is not None and row[2]:
                id_sub = int(row[0])
                id_cat = int(row[1])
                nombre_sub = str(row[2]).strip()
                keywords = str(row[3]).strip()
                
                subcats_dict[nombre_sub] = {
                    "id_sub": id_sub, 
                    "id_cat": id_cat, 
                    "keywords": keywords
                }
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error cargando catálogos en Dashboard: {e}")
        if conn: 
            conn.close()
            
    return cats_dict, subcats_dict

def load_reclamos():
    """Carga los reclamos procesando coordenadas limpias y trayendo los nombres oficiales de categorías."""
    conn = get_conn()
    if not conn: return pd.DataFrame()
    query = """
        SELECT 
            r.id_reclamo,
            COALESCE(r.codigo_seguimiento, 'SIN-CODIGO') as codigo_seguimiento,
            r.fecha_ingreso as fecha,
            r.texto_original,
            r.texto_transcripto,
            COALESCE(r.direccion_extraida, 'A confirmar') as direccion,
            COALESCE(r.barrio_extraido, 'Avellaneda Centro') as barrio,
            r.id_cat_general,
            r.id_subcategoria,
            COALESCE(TRIM(cg.nombre_general), 'Sin Categorizar') as categoria,
            COALESCE(TRIM(sc.nombre_subcategoria), 'Sin Subcategoría') as subcategoria,
            UPPER(COALESCE(r.urgencia, 'MEDIA')) as urgencia,
            COALESCE(r.precision_geocoding, 'Sin Geolocalizar') as precision_geocoding,
            CASE 
                WHEN r.geom IS NOT NULL THEN ST_Y(r.geom)
                WHEN r.lat IS NOT NULL AND r.lat != 0 THEN r.lat
                ELSE NULL 
            END as lat,
            CASE 
                WHEN r.geom IS NOT NULL THEN ST_X(r.geom)
                WHEN r.lon IS NOT NULL AND r.lon != 0 THEN r.lon
                ELSE NULL 
            END as lon,
            COALESCE(r.estado, 'PENDIENTE_REVISION') as estado,
            r.url_audio, r.url_foto
        FROM reclamos r
        LEFT JOIN categorias_generales cg ON r.id_cat_general = cg.id_cat_general
        LEFT JOIN subcategorias sc ON r.id_subcategoria = sc.id_subcategoria
        ORDER BY r.fecha_ingreso DESC
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            df = pd.read_sql(query, conn)
        conn.close()
        
        df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
        df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
        return df
    except Exception as e:
        print(f"Error cargando reclamos: {e}")
        if conn: conn.close()
        return pd.DataFrame()

def actualizar_reclamo_operador(id_reclamo, estado_nuevo, direccion_nueva, barrio_nuevo, motivo_nuevo, id_cat, id_subcat, estado_anterior="PENDIENTE_REVISION"):
    """Permite actualizar reclamos tanto en revisión como previamente ingresados."""
    conn = get_conn()
    if not conn: return False
    
    lat, lon = re_geocodificar(direccion_nueva)
    
    try:
        cur = conn.cursor()
        if lat and lon:
            cur.execute("""
                UPDATE reclamos 
                SET estado = %s, 
                    direccion_extraida = %s, 
                    barrio_extraido = %s,
                    texto_transcripto = %s,
                    id_cat_general = %s,
                    id_subcategoria = %s,
                    lat = %s, lon = %s, 
                    geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                    precision_geocoding = 'Validado Operador', 
                    fecha_ultima_actualizacion = NOW()
                WHERE id_reclamo = %s
            """, (estado_nuevo, direccion_nueva, barrio_nuevo, motivo_nuevo, id_cat, id_subcat, lat, lon, lon, lat, id_reclamo))
        else:
            cur.execute("""
                UPDATE reclamos 
                SET estado = %s, 
                    direccion_extraida = %s, 
                    barrio_extraido = %s,
                    texto_transcripto = %s,
                    id_cat_general = %s,
                    id_subcategoria = %s,
                    fecha_ultima_actualizacion = NOW()
                WHERE id_reclamo = %s
            """, (estado_nuevo, direccion_nueva, barrio_nuevo, motivo_nuevo, id_cat, id_subcat, id_reclamo))

        cur.execute("""
            INSERT INTO historial_estados (id_reclamo, estado_anterior, estado_nuevo, fecha_cambio, responsable, comentario)
            VALUES (%s, %s, %s, NOW(), 'Operador Dashboard', %s)
        """, (id_reclamo, estado_anterior, estado_nuevo, f"Edición manual operador: {direccion_nueva}"))

        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ Error actualizando reclamo: {e}")
        if conn: conn.rollback(); conn.close()
        return False

def guardar_subcategoria(id_sub, id_cat, nombre_sub, palabras_clave):
    """Inserta o actualiza una subcategoría y sus palabras clave en PostgreSQL."""
    conn = get_conn()
    if not conn: return False
    try:
        cur = conn.cursor()
        if id_sub:
            cur.execute("""
                UPDATE subcategorias 
                SET id_cat_general = %s, nombre_subcategoria = %s, palabras_clave = %s
                WHERE id_subcategoria = %s
            """, (id_cat, nombre_sub, palabras_clave, id_sub))
        else:
            cur.execute("""
                INSERT INTO subcategorias (id_cat_general, nombre_subcategoria, palabras_clave)
                VALUES (%s, %s, %s)
            """, (id_cat, nombre_sub, palabras_clave))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error guardando subcategoría: {e}")
        if conn: conn.rollback(); conn.close()
        return False

def eliminar_reclamo_transaccional(id_reclamo, operador, motivo_baja):
    conn = get_conn()
    if not conn: return False

    sql_crear_tabla = """
        CREATE TABLE IF NOT EXISTS public.reclamos_eliminados (
            id_eliminado SERIAL PRIMARY KEY,
            id_reclamo UUID,  
            codigo_seguimiento VARCHAR(50),
            remoto_jid VARCHAR(100),
            texto_original TEXT,
            motivo_eliminacion TEXT,
            operador VARCHAR(100),
            fecha_eliminacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            datos_completos_json JSONB
        );
    """

    sql_copiar = """
        INSERT INTO public.reclamos_eliminados (id_reclamo, codigo_seguimiento, remoto_jid, texto_original, motivo_eliminacion, operador, datos_completos_json)
        SELECT id_reclamo, codigo_seguimiento, remoto_jid, texto_original, %s, %s, row_to_json(r)
        FROM public.reclamos r
        WHERE id_reclamo = %s;
    """
    sql_borrar_historial = "DELETE FROM public.historial_estados WHERE id_reclamo = %s;"
    sql_borrar_reclamo = "DELETE FROM public.reclamos WHERE id_reclamo = %s;"

    try:
        cur = conn.cursor()
        cur.execute(sql_crear_tabla)
        cur.execute(sql_copiar, (motivo_baja, operador, id_reclamo))
        cur.execute(sql_borrar_historial, (id_reclamo,))
        cur.execute(sql_borrar_reclamo, (id_reclamo,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ Error borrando reclamo {id_reclamo}: {e}")
        if conn: conn.rollback(); conn.close()
        return False

# --- SIDEBAR DE FILTROS ---
st.sidebar.title("🏙️ MuniIA Avellaneda")
st.sidebar.markdown("---")

df_raw = load_reclamos()
cats_dict, subcats_dict = load_catalogos()

if not df_raw.empty:
    barrio_sel = st.sidebar.selectbox("Filtrar por Barrio", ["Todos"] + list(df_raw["barrio"].dropna().unique()))
    estado_sel = st.sidebar.selectbox("Filtrar por Estado", ["Todos"] + list(df_raw["estado"].dropna().unique()))

    df = df_raw.copy()
    if barrio_sel != "Todos": df = df[df["barrio"] == barrio_sel]
    if estado_sel != "Todos": df = df[df["estado"] == estado_sel]
else:
    df = df_raw

st.title("📌 Centro de Control Operativo · Reclamos Urbanos")
st.caption("Municipio de Avellaneda | Conectado a PostGIS")

# METRICAS KPI
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Reclamos", len(df_raw))
pend = len(df_raw[df_raw['estado'] == 'PENDIENTE_REVISION']) if not df_raw.empty else 0
c2.metric("Pendientes Revisión", pend)
ing = len(df_raw[df_raw['estado'] == 'INGRESADO']) if not df_raw.empty else 0
c3.metric("Ingresados / En Curso", ing)
res = len(df_raw[df_raw['estado'] == 'RESUELTO']) if not df_raw.empty else 0
c4.metric("Resueltos", res)

st.markdown("---")

# Si no hay pestaña guardada, arranca en Revisión
if "pestana_activa" not in st.session_state:
    st.session_state.pestana_activa = "🗺 Mapa Interactivo"

pestana = st.segmented_control(
    "Seleccionar Pestaña",
    ["🗺 Mapa Interactivo", "🚨 Bandeja de Revisión", "📋 Tabla General & Edición", "⚙️ CRUD Subcategorías", "📊 Telemetría Inbox"],
    default=st.session_state.pestana_activa,
    key="nav_pestanas"
)
# Guardamos la elección para que sobreviva al rerun
st.session_state.pestana_activa = pestana


if st.session_state.pestana_activa == "🗺 Mapa Interactivo":
    if df.empty:
        st.info("Sin datos para mostrar.")
    elif HAS_FOLIUM:
        m = folium.Map(location=[-34.6865, -58.3486], zoom_start=13, tiles="OpenStreetMap")
        st.caption("🔴 **Alta/Crítica** | 🟠 **Media** | 🟢 **Baja** | 🟡 **Pendiente Revisión**")
        
        marcadores_agregados = 0
        for _, row in df.iterrows():
            if pd.isna(row['lat']) or pd.isna(row['lon']): continue
            
            urg = str(row['urgencia']).upper()
            if row['estado'] == 'PENDIENTE_REVISION':
                color_pin = "orange"
            elif urg in ["ALTA", "CRITICA", "URGENTE"]:
                color_pin = "red"
            elif urg == "BAJA":
                color_pin = "green"
            else:
                color_pin = "blue"
            
            popup_html = f"""
            <b>Código:</b> {row['codigo_seguimiento']}<br>
            <b>Urgencia:</b> {row['urgencia']}<br>
            <b>Dirección:</b> {row['direccion']}<br>
            <b>Barrio:</b> {row['barrio']}<br>
            <b>Estado:</b> {row['estado']}
            """
            
            folium.Marker(
                location=[float(row['lat']), float(row['lon'])],
                popup=popup_html,
                tooltip=f"{row['codigo_seguimiento']} ({row['urgencia']})",
                icon=folium.Icon(color=color_pin, icon="info-sign")
            ).add_to(m)
            marcadores_agregados += 1
            
        st_folium(m, width=None, height=500, returned_objects=[])
        if marcadores_agregados == 0:
            st.warning("⚠️ Hay reclamos registrados pero ninguno posee coordenadas geográficas válidas para mostrar en el mapa.")

if st.session_state.pestana_activa == "🚨 Bandeja de Revisión":
    st.markdown("### 🚨 Reclamos Pendientes de Revisión")
    if not df_raw.empty and 'estado' in df_raw.columns:
        df_pendientes = df_raw[df_raw['estado'] == 'PENDIENTE_REVISION']
    else:
        df_pendientes = pd.DataFrame()
    
    if df_pendientes.empty:
        st.success("🎉 ¡No hay reclamos pendientes de revisión!")
    else:
        for _, row in df_pendientes.iterrows():
            rec_id = row['id_reclamo']
            st.markdown(f"#### Reclamo: `{row['codigo_seguimiento']}`")
            
            with st.container():
                c_inf, c_med, c_act = st.columns([3, 2, 3])
                
                with c_inf:
                    st.markdown("**Multimedia / Mensaje Original:**")
                    texto_actual = row['texto_transcripto'] or row['texto_original'] or ""
                    motivo_editado = st.text_area("Motivo / Descripción del Reclamo", value=texto_actual, key=f"rev_motivo_{rec_id}", height=100)
                
                with c_med:
                    url_audio = row.get('url_audio')
                    if pd.notna(url_audio) and str(url_audio).strip():
                        a_path = PROCESADOS_DIR / str(url_audio)
                        if a_path.exists(): st.audio(str(a_path))
                    
                    url_foto = row.get('url_foto')
                    if pd.notna(url_foto) and str(url_foto).strip():
                        i_path = PROCESADOS_DIR / str(url_foto)
                        if i_path.exists(): st.image(str(i_path), width='stretch')

                with c_act:
                    nueva_dir = st.text_input("Dirección Corregida", value=row['direccion'], key=f"rev_dir_{rec_id}")
                    idx_barrio = BARRIOS_AVELLANEDA.index(row['barrio']) if row['barrio'] in BARRIOS_AVELLANEDA else 0
                    nuevo_barrio = st.selectbox("Barrio", BARRIOS_AVELLANEDA, index=idx_barrio, key=f"rev_barrio_{rec_id}")
                    
                    # --- FILTRADO REACTIVO DE CATEGORÍAS / SUBCATEGORÍAS ---
                    lista_cats = list(cats_dict.keys()) if cats_dict else ["Sin Categorizar"]
                    idx_cat_def = lista_cats.index(row['categoria']) if row['categoria'] in lista_cats else 0

                    cat_sel = st.selectbox("Categoría General", lista_cats, index=idx_cat_def, key=f"rev_cat_{rec_id}")
                    id_cat_elegida = cats_dict.get(cat_sel)

                    if id_cat_elegida is not None:
                        lista_subcats = [
                            nombre_sub 
                            for nombre_sub, info in subcats_dict.items() 
                            if int(info["id_cat"]) == int(id_cat_elegida)
                        ]
                    else:
                        lista_subcats = []

                    if not lista_subcats:
                        lista_subcats = ["Sin Subcategoría"]

                    idx_sub_def = lista_subcats.index(row['subcategoria']) if row['subcategoria'] in lista_subcats else 0

                    subcat_sel = st.selectbox("Subcategoría", lista_subcats, index=idx_sub_def, key=f"rev_subcat_{rec_id}")
                    id_subcat_elegida = subcats_dict.get(subcat_sel, {}).get("id_sub")

                    col_btn1, col_btn2 = st.columns(2)
                    with col_btn1:
                        if st.button("✅ Validar y Enviar", type="primary", key=f"rev_btn_validar_{rec_id}"):
                            if actualizar_reclamo_operador(rec_id, "INGRESADO", nueva_dir, nuevo_barrio, motivo_editado, id_cat_elegida, id_subcat_elegida):
                                st.success("Reclamo validado y geocodificado con éxito.")
                                st.rerun()

                    with col_btn2:
                        if st.button("🗑️ Eliminar Reclamo", key=f"rev_btn_del_init_{rec_id}"):
                            st.session_state[f"confirm_delete_{rec_id}"] = True
#                            st.rerun()

            if st.session_state.get(f"confirm_delete_{rec_id}", False):
                st.warning(f"⚠️ **¿Confirmas la eliminación del reclamo `{row['codigo_seguimiento']}`?**")
                motivo_baja = st.text_input("Motivo de baja / desestimación", value="Duplicado / Desestimado", key=f"rev_txt_baja_{rec_id}")
                c_si, c_no = st.columns([1, 4])
                with c_si:
                    if st.button("Confirmar Borrado", type="primary", key=f"rev_btn_del_yes_{rec_id}"):
                        if eliminar_reclamo_transaccional(rec_id, "Operador Dashboard", motivo_baja):
                            st.session_state[f"confirm_delete_{rec_id}"] = False
                            st.success("Reclamo auditado y borrado correctamente.")
                            st.rerun()
                with c_no:
                    if st.button("Cancelar", key=f"rev_btn_del_no_{rec_id}"):
                        st.session_state[f"confirm_delete_{rec_id}"] = False
#                        st.rerun()

            st.markdown("---")

if st.session_state.pestana_activa == "📋 Tabla General & Edición":
    st.markdown("### 📋 Registro General de Reclamos")
    
    # 1. TABLA GENERAL RESUMIDA
    st.dataframe(
        df[[
            "codigo_seguimiento", 
            "fecha", 
            "urgencia", 
            "categoria", 
            "subcategoria", 
            "texto_transcripto", 
            "direccion", 
            "barrio", 
            "estado"
        ]], 
        column_config={
            "codigo_seguimiento": st.column_config.TextColumn("Código", width="medium"),
            "fecha": st.column_config.DatetimeColumn("Fecha", format="DD/MM/YYYY HH:mm", width="small"),
            "urgencia": st.column_config.TextColumn("Urgencia", width="small"),
            "categoria": st.column_config.TextColumn("Categoría", width="medium"),
            "subcategoria": st.column_config.TextColumn("Subcategoría", width="medium"),
            "texto_transcripto": st.column_config.TextColumn("Texto Transcripto / Transcripción", width="large"),
            "direccion": st.column_config.TextColumn("Dirección", width="medium"),
            "barrio": st.column_config.TextColumn("Barrio", width="small"),
            "estado": st.column_config.TextColumn("Estado", width="small")
        },
        use_container_width=True,
#        width=True,
        hide_index=True
    )

    st.markdown("---")
    # 2. FEED DETALLADO EN 2 LÍNEAS POR RECLAMO
    st.markdown("### 🎙️ Detalle de Reclamos y Contenido Multimedia")
    st.caption("Cada reclamo se presenta estructurado en 2 líneas: Metadatos principales y Contenido (Audio/Imagen/Texto).")

    if df.empty:
        st.info("No hay reclamos registrados para mostrar.")
    else:
        for idx, row in df.iterrows():
            with st.container():
                # LÍNEA 1: Cabecera con datos del reclamo
                c_head1, c_head2, c_head3, c_head4 = st.columns([2, 3, 3, 2])
                with c_head1:
                    st.markdown(f"📌 **`{row['codigo_seguimiento']}`**")
                    st.caption(f"📅 {row['fecha']}")
                with c_head2:
                    st.markdown(f"🏢 **{row['categoria']}**")
                    st.caption(f"📁 Subcat: {row['subcategoria']}")
                with c_head3:
                    st.markdown(f"📍 **{row['direccion']}**")
                    st.caption(f"🏡 Barrio: {row['barrio']}")
                with c_head4:
                    st.markdown(f"🚨 Urgencia: **{row['urgencia']}**")
                    st.caption(f"⚙️ Estado: **{row['estado']}**")

                # LÍNEA 2: Contenido Integrado (Audio / Imagen / Texto)
                c_media, c_texto = st.columns([1, 2])
                
                with c_media:
                    # Caso A: Tiene Audio
                    url_audio = row.get("url_audio")
                    tiene_audio = pd.notna(url_audio) and str(url_audio).strip() != ""
                    if tiene_audio:
                        a_path = PROCESADOS_DIR / str(url_audio).strip()
                        if a_path.exists():
                            st.audio(str(a_path))
                        else:
                            st.warning(" Audio no encontrado en procesados")

                    # Caso B: Tiene Foto
                    url_foto = row.get("url_foto")
                    tiene_foto = pd.notna(url_foto) and str(url_foto).strip() != ""
                    if tiene_foto:
                        f_path = PROCESADOS_DIR / str(url_foto).strip()
                        if f_path.exists():
                            st.image(str(f_path), caption="Foto Adjunta", width=200)
                        else:
                            st.caption(f"🖼️ Foto: {url_foto}")

                    if not tiene_audio and not tiene_foto:
                        st.caption("📝 Mensaje de Texto Directo (Sin multimedia adjunta)")

                with c_texto:
                    contenido_texto = row['texto_transcripto'] or row['texto_original'] or "Sin texto disponible"
                    st.text_area(
                        "Transcripción / Contenido del mensaje",
                        value=contenido_texto,
                        height=80,
                        disabled=True,
                        key=f"feed_txt_{row['id_reclamo']}_{idx}"
                    )

                st.markdown("<hr style='margin: 8px 0; border: 0.5px solid #e6e6e6;'>", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### ✏️ Re-Edición de Reclamos Ingresados")
    
    df_ingresados = df_raw[df_raw['estado'] == 'INGRESADO']
    if df_ingresados.empty:
        st.info("No hay reclamos en estado INGRESADO para editar.")
    else:
        opciones_reclamos = {f"{r['codigo_seguimiento']} - {r['direccion']} ({r['barrio']})": r['id_reclamo'] for _, r in df_ingresados.iterrows()}
        seleccion_label = st.selectbox("Seleccionar Reclamo para Editar/Re-geocodificar", list(opciones_reclamos.keys()))
        
        if seleccion_label:
            id_edit = opciones_reclamos[seleccion_label]
            row_edit = df_ingresados[df_ingresados['id_reclamo'] == id_edit].iloc[0]
            rec_id = row_edit['id_reclamo']
            
            c1, c2 = st.columns(2)
            with c1:
                e_dir = st.text_input("Dirección", value=row_edit['direccion'], key=f"ed_dir_{id_edit}")
                idx_b = BARRIOS_AVELLANEDA.index(row_edit['barrio']) if row_edit['barrio'] in BARRIOS_AVELLANEDA else 0
                e_barrio = st.selectbox("Barrio", BARRIOS_AVELLANEDA, index=idx_b, key=f"ed_barrio_{id_edit}")
                e_motivo = st.text_area("Motivo / Descripción", value=row_edit['texto_transcripto'] or row_edit['texto_original'] or "", key=f"ed_motivo_{id_edit}")
            
            with c2:
                lista_cats = list(cats_dict.keys()) if cats_dict else ["Sin Categorizar"]
                idx_cat_curr = lista_cats.index(row_edit['categoria']) if row_edit['categoria'] in lista_cats else 0
                
                # 1. Selección de Categoría General
                e_cat = st.selectbox("Categoría General", lista_cats, index=idx_cat_curr, key=f"ed_cat_{id_edit}")
           
                # 2. Obtener ID de Categoría
                id_c_el = cats_dict.get(e_cat)
                
                # 3. Filtrar subcategorías asociadas
                if id_c_el is not None:
                    lista_subs = [
                        nombre_sub 
                        for nombre_sub, info in subcats_dict.items() 
                        if int(info["id_cat"]) == int(id_c_el)
                    ]
                else:
                    lista_subs = []

                if not lista_subs: 
                    lista_subs = ["Sin Subcategoría"]
                
                idx_sub_curr = lista_subs.index(row_edit['subcategoria']) if row_edit['subcategoria'] in lista_subs else 0
                e_subcat = st.selectbox("Subcategoría", lista_subs, index=idx_sub_curr, key=f"ed_subcat_{id_edit}")
                
                # 4. Obtener ID de Subcategoría
                id_s_el = subcats_dict.get(e_subcat, {}).get("id_sub")


                col_btn1, col_btn2 = st.columns(2)
                with col_btn1:

                    # 5. Botón de guardado con referencias explícitas
                    if st.button("💾 Guardar Cambios y Re-Geocodificar", type="primary", key=f"ed_btn_save_{id_edit}"):
                        if actualizar_reclamo_operador(
                            id_reclamo=id_edit, 
                            estado_nuevo="INGRESADO", 
                            direccion_nueva=e_dir, 
                            barrio_nuevo=e_barrio, 
                            motivo_nuevo=e_motivo, 
                            id_cat=id_c_el, 
                            id_subcat=id_s_el, 
                            estado_anterior="INGRESADO"
                        ):
                            st.success("Reclamo actualizado y geocodificado correctamente.")
                            st.rerun()

                    with col_btn2:
                        if st.button("🗑️ Eliminar Reclamo", key=f"rev_btn_del_init_{rec_id}"):
                            st.session_state[f"confirm_delete_{rec_id}"] = True
#                            st.rerun()

            if st.session_state.get(f"confirm_delete_{rec_id}", False):
                st.warning(f"⚠️ **¿Confirmas la eliminación del reclamo `{row_edit['codigo_seguimiento']}`?**")
                motivo_baja = st.text_input("Motivo de baja / desestimación", value="Duplicado / Desestimado", key=f"rev_txt_baja_{rec_id}")
                c_si, c_no = st.columns([1, 4])
                with c_si:
                    if st.button("Confirmar Borrado", type="primary", key=f"rev_btn_del_yes_{rec_id}"):
                        if eliminar_reclamo_transaccional(rec_id, "Operador Dashboard", motivo_baja):
                            st.session_state[f"confirm_delete_{rec_id}"] = False
                            st.success("Reclamo auditado y borrado correctamente.")
                            st.rerun()
                with c_no:
                    if st.button("Cancelar", key=f"rev_btn_del_no_{rec_id}"):
                        st.session_state[f"confirm_delete_{rec_id}"] = False
#                        st.rerun()


if st.session_state.pestana_activa == "⚙️ CRUD Subcategorías":
    st.markdown("### ⚙️ CRUD de Subcategorías y Vocabulario Vecinal")
    st.caption("Gestiona las subcategorías y sus palabras clave del lenguaje cotidiano para calibrar la extracción de Ollama.")
    
    col_list, col_form = st.columns([2, 1])
    
    with col_list:
        st.markdown("#### Subcategorías Registradas")
        data_subs = []
        for name_sub, info in subcats_dict.items():
            cat_nombre = [k for k, v in cats_dict.items() if v == info["id_cat"]]
            cat_str = cat_nombre[0] if cat_nombre else "S/D"
            data_subs.append({
                "ID": info["id_sub"],
                "Categoría General": cat_str,
                "Subcategoría": name_sub,
                "Palabras Clave (Lenguaje Cotidiano)": info["keywords"]
            })
        st.dataframe(pd.DataFrame(data_subs), width='stretch')

    with col_form:
        st.markdown("#### Administrar Subcategoría")
        sub_nombres = ["--- NUEVA SUBCATEGORÍA ---"] + list(subcats_dict.keys())
        sub_sel_admin = st.selectbox("Seleccionar para Editar", sub_nombres)
        
        if sub_sel_admin == "--- NUEVA SUBCATEGORÍA ---":
            id_s_adm = None
            cat_default_idx = 0
            nom_s_def = ""
            kw_def = ""
        else:
            info_adm = subcats_dict[sub_sel_admin]
            id_s_adm = info_adm["id_sub"]
            nom_s_def = sub_sel_admin
            kw_def = info_adm["keywords"]
            cat_nombre = [k for k, v in cats_dict.items() if v == info_adm["id_cat"]]
            cat_default_idx = list(cats_dict.keys()).index(cat_nombre[0]) if cat_nombre and cat_nombre[0] in cats_dict else 0

        with st.form(key="form_crud_subcategoria"):
            c_gen_sel = st.selectbox("Categoría General Padre", list(cats_dict.keys()) if cats_dict else ["Sin Categorizar"], index=cat_default_idx)
            nombre_sub_input = st.text_input("Nombre de la Subcategoría", value=nom_s_def)
            kw_input = st.text_area("Palabras Clave (Separadas por coma)", value=kw_def, help="Ejemplo: pozo, cráter, bache, rotura de asfalto, fisura")
            
            btn_save_sub = st.form_submit_button("💾 Guardar Subcategoría", type="primary")
            if btn_save_sub:
                if nombre_sub_input.strip():
                    id_c_padre = cats_dict.get(c_gen_sel)
                    if guardar_subcategoria(id_s_adm, id_c_padre, nombre_sub_input.strip(), kw_input.strip()):
                        st.success("Subcategoría guardada correctamente.")
                        st.rerun()
                else:
                    st.error("El nombre de la subcategoría no puede estar vacío.")

if st.session_state.pestana_activa == "📊 Telemetría Inbox":
    st.markdown("### 📊 Telemetría de Servicios e Ingesta")
    conn_tel = get_conn()
    if conn_tel:
        try:
            cur = conn_tel.cursor()
            cur.execute("SELECT COUNT(*) FROM reclamos;")
            total_db = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM reclamos WHERE url_audio IS NOT NULL;")
            total_audios = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM reclamos WHERE url_foto IS NOT NULL;")
            total_fotos = cur.fetchone()[0]
            
            total_textos = total_db - (total_audios + total_fotos)
            
            cur.execute("SELECT COUNT(*) FROM reclamos_eliminados;")
            total_eliminados = cur.fetchone()[0]
            
            cur.close()
            conn_tel.close()

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total Ingresados en BD", total_db)
            m2.metric("Audios Procesados (Whisper)", total_audios)
            m3.metric("Imágenes Recibidas", total_fotos)
            m4.metric("Reclamos Auditados / Desestimados", total_eliminados)

            st.markdown("---")
            st.markdown("#### 📈 Desglose por Formato de Entrada")
            df_formatos = pd.DataFrame({
                "Origen": ["Texto Directo", "Audio (Whisper)", "Imagen"],
                "Cantidad": [total_textos, total_audios, total_fotos]
            })
            st.dataframe(df_formatos, width='stretch')

        except Exception as e:
            st.error(f"Error al obtener métricas de telemetría: {e}")
            if conn_tel: conn_tel.close()
    else:
        st.warning("No se pudo conectar a la base de datos para cargar telemetría.")