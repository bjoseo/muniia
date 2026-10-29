#!/bin/bash
set -e
# Posicionarse dinámicamente en la raíz del proyecto
CDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$CDIR"

echo "🧹 Limpiando procesos anteriores..."
pkill -f 'procesar_inbox_v4.py' || true
pkill -f 'Monitor_Whatsapp_Baileys.js' || true
pkill -f 'streamlit' || true

#**********************

# Crear carpetas
mkdir -p "$CDIR/whatsapp_inbox/procesados"
mkdir -p "$CDIR/auth_info_baileys"

# Verificar Node y .env
if ! command -v node >/dev/null 2>&1; then
  echo "[ERROR] node no encontrado. Instala: sudo apt install nodejs npm"
  exit 1
fi

if [ ! -f "$CDIR/.env" ]; then
  echo "[WARN] No existe $CDIR/.env - Creando uno por defecto con DB_PASS=postgres"
  cat > "$CDIR/.env" << 'ENV'
DB_HOST=localhost
DB_PORT=5432
DB_NAME=muniia
DB_USER=postgres
DB_PASS=postgres
ENV
fi

if [ ! -d "$CDIR/node_modules" ]; then
  echo "[INFO] Instalando dependencias npm..."
  npm install @whiskeysockets/baileys qrcode-terminal pg dotenv
fi

# Fix line endings
dos2unix "$CDIR/Monitor_Whatsapp_Baileys.js" 2>/dev/null || true

LOG_WHATSAPP="$CDIR/whatsapp_monitor.log"
touch "$LOG_WHATSAPP"

echo "[INFO] Lanzando Monitor_Whatsapp_Baileys.js en segundo plano con setsid..."

# FIX DEFINITIVO: setsid + nohup + stdin desde /dev/null + desacoplado
# Esto es lo que hace que sobreviva a wsl -e bash -c "..."
setsid nohup node "$CDIR/Monitor_Whatsapp_Baileys.js" >> "$LOG_WHATSAPP" 2>&1 < /dev/null &
PID=$!
echo $PID > "$CDIR/monitor.pid"

# Desacoplar completamente
disown $PID 2>/dev/null || true
echo "🟢 Monitor WhatsApp iniciado."


#**********************


# 1. Iniciar Procesador Python (con -u para desactivar buffering)
nohup "$CDIR/.venv/bin/python" -u "$CDIR/procesar_inbox_v4.py" > "$CDIR/procesador_python.log" 2>&1 &
disown
echo "🟢 Procesador Python iniciado."

# 2. Iniciar Monitor de WhatsApp
#nohup node "$CDIR/Monitor_Whatsapp_Baileys.js" > "$CDIR/whatsapp_monitor.log" 2>&1 &
#disown
#echo "🟢 Monitor WhatsApp iniciado."

# 3. Iniciar Dashboard Streamlit
nohup "$CDIR/.venv/bin/python" -u -m streamlit run "$CDIR/scr/dashboard.py" --server.port 8501 --server.headless true > "$CDIR/dashboard.log" 2>&1 &
disown

echo "🟢 Dashboard Streamlit iniciado en http://localhost:8501"

# Breve pausa para asegurar que los logs escriban la inicialización
sleep 2