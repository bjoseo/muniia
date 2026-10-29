"""
MuniIA - Dashboard Operativo v4.6
Fixes: Borrado Transaccional Atómico, Mapeo Robusto PostGIS y Semáforo de Urgencia en Folium.
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

# Cargar variables de entorno
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

st.set_page_config(page_title="MuniIA - Centro de Control Operativo", layout="wide", page_icon="🏙️")

# Desactivar refresco automático si el usuario está confirmando un borrado
if HAS_AUTOREFRESH and not any(k.startswith("confirm_delete_") and v for k, v in st.session_state.items()):
    st_autorefresh(interval=12000, limit=None, key="muniia_dashboard_autorefresh")

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
    conn = get_conn()
    cats_dict, subcats_dict = {}, {}
    if not conn: return cats_dict, subcats_dict
    try:
        cur = conn.cursor()
        cur.execute("SELECT id_cat_general, nombre_general FROM categorias_generales ORDER BY nombre_general;")
        for row in cur.fetchall():
            cats_dict[row[1]] = row[0]

        cur.execute("SELECT id_subcategoria, id_cat_general, nombre_subcategoria FROM subcategorias ORDER BY nombre_subcategoria;")
        for row in cur.fetchall():
            subcats_dict[row[2]] = {"id_sub": row[0], "id_cat": row[1]}
        
        cur.close()
        conn.close()
    except Exception:
        if conn: conn.close()
    return cats_dict, subcats_dict

def load_reclamos():
    """Carga los reclamos procesando coordenadas limpias de PostGIS o datos legacy."""
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
            COALESCE(cg.nombre_general, 'Sin Categorizar') as categoria,
            COALESCE(sc.nombre_subcategoria, 'Sin Subcategoría') as subcategoria,
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
        
        # Casting seguro de coordenadas
        df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
        df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
        return df
    except Exception as e:
        print(f"Error cargando reclamos: {e}")
        if conn: conn.close()
        return pd.DataFrame()

def actualizar_reclamo_operador(id_reclamo, estado_nuevo, direccion_nueva, barrio_nuevo, motivo_nuevo, id_cat, id_subcat):
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
            VALUES (%s, 'PENDIENTE_REVISION', %s, NOW(), 'Operador Dashboard', %s)
        """, (id_reclamo, estado_nuevo, f"Validación manual: {direccion_nueva}"))

        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ Error actualizando reclamo: {e}")
        if conn: conn.rollback(); conn.close()
        return False

def eliminar_reclamo_transaccional(id_reclamo, operador, motivo_baja):
    """Ejecuta borrado en cascada y respaldo de auditoría en PostgreSQL."""
    conn = get_conn()
    if not conn: return False
    
    sql_crear_tabla = """
        CREATE TABLE IF NOT EXISTS public.reclamos_eliminados (
            id_eliminado SERIAL PRIMARY KEY,
            id_reclamo INT,
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

tab_mapa, tab_revision, tab_tabla, tab_inbox = st.tabs([
    "🗺️ Mapa Interactivo", "🚨 Bandeja de Revisión", "📋 Tabla General", "📊 Telemetría Inbox"
])

with tab_mapa:
    if df.empty:
        st.info("Sin datos para mostrar.")
    elif HAS_FOLIUM:
        m = folium.Map(location=[-34.6865, -58.3486], zoom_start=13, tiles="OpenStreetMap")
        
        # Leyenda visual de colores por urgencia
        st.caption("🔴 **Alta/Crítica** | 🟠 **Media** | 🟢 **Baja** | 🟡 **Pendiente Revisión**")
        
        marcadores_agregados = 0
        for _, row in df.iterrows():
            if pd.isna(row['lat']) or pd.isna(row['lon']): continue
            
            # Asignación de color según nivel de urgencia y estado
            urg = str(row['urgencia']).upper()
            if row['estado'] == 'PENDIENTE_REVISION':
                color_pin = "orange"
            elif urg in ["ALTA", "CRITICA", "URGENTE"]:
                color_pin = "red"
            elif urg == "BAJA":
                color_pin = "green"
            else:
                color_pin = "blue" # MEDIA por defecto
            
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
            st.warning("⚠️ Hay reclamos registrados pero ninguno posee coordenadas geográficas válidas aún.")

with tab_revision:
    st.markdown("### 🚨 Reclamos Pendientes de Revisión")
    df_pendientes = df_raw[df_raw['estado'] == 'PENDIENTE_REVISION']
    
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
                    motivo_editado = st.text_area("Motivo / Descripción del Reclamo", value=texto_actual, key=f"motivo_{rec_id}", height=100)
                
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
                    nueva_dir = st.text_input("Dirección Corregida", value=row['direccion'], key=f"dir_{rec_id}")
                    
                    idx_barrio = BARRIOS_AVELLANEDA.index(row['barrio']) if row['barrio'] in BARRIOS_AVELLANEDA else 0
                    nuevo_barrio = st.selectbox("Barrio", BARRIOS_AVELLANEDA, index=idx_barrio, key=f"barrio_{rec_id}")
                    
                    lista_cats = list(cats_dict.keys()) if cats_dict else ["Sin Categorizar"]
                    cat_sel = st.selectbox("Categoría General", lista_cats, key=f"cat_{rec_id}")
                    
                    id_cat_elegida = cats_dict.get(cat_sel)
                    lista_subcats = [s for s, info in subcats_dict.items() if info["id_cat"] == id_cat_elegida] if id_cat_elegida else ["Sin Subcategoría"]
                    if not lista_subcats: lista_subcats = ["Sin Subcategoría"]
                    
                    subcat_sel = st.selectbox("Subcategoría", lista_subcats, key=f"subcat_{rec_id}")
                    id_subcat_elegida = subcats_dict.get(subcat_sel, {}).get("id_sub")

                    col_btn1, col_btn2 = st.columns(2)
                    with col_btn1:
                        if st.button("✅ Validar y Enviar", type="primary", key=f"btn_validar_{rec_id}"):
                            if actualizar_reclamo_operador(rec_id, "INGRESADO", nueva_dir, nuevo_barrio, motivo_editado, id_cat_elegida, id_subcat_elegida):
                                st.success("Reclamo validado y geocodificado con éxito.")
                                st.rerun()

                    with col_btn2:
                        if st.button("🗑️ Eliminar Reclamo", key=f"btn_del_init_{rec_id}"):
                            st.session_state[f"confirm_delete_{rec_id}"] = True
                            st.rerun()

            # Confirmación de borrado
            if st.session_state.get(f"confirm_delete_{rec_id}", False):
                st.warning(f"⚠️ **¿Confirmas la eliminación del reclamo `{row['codigo_seguimiento']}`?**")
                motivo_baja = st.text_input("Motivo de baja / desestimación", value="Duplicado / Desestimado", key=f"txt_baja_{rec_id}")
                
                c_si, c_no = st.columns([1, 4])
                with c_si:
                    if st.button("Confirmar Borrado", type="primary", key=f"btn_del_yes_{rec_id}"):
                        if eliminar_reclamo_transaccional(rec_id, "Operador Dashboard", motivo_baja):
                            st.session_state[f"confirm_delete_{rec_id}"] = False
                            st.success("Reclamo auditado y borrado correctamente.")
                            st.rerun()
                with c_no:
                    if st.button("Cancelar", key=f"btn_del_no_{rec_id}"):
                        st.session_state[f"confirm_delete_{rec_id}"] = False
                        st.rerun()

            st.markdown("---")

with tab_tabla:
    st.dataframe(
        df[["codigo_seguimiento", "fecha", "urgencia", "categoria", "subcategoria", "texto_transcripto", "direccion", "barrio", "estado"]], 
        width='stretch'
    )

with tab_inbox:
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