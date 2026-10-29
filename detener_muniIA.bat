@echo off
echo ============================================
echo   Deteniendo MuniIA (Docker y Procesos WSL)
echo ============================================
echo.

echo [1/3] Deteniendo demonios de Python y Node.js en WSL...
wsl bash -c "pkill -f 'procesar_inbox_v4.py' || true" >nul 2>&1
wsl bash -c "pkill -f 'Monitor_Whatsapp_Baileys.js' || true" >nul 2>&1

echo [2/3] Finalizando proceso de Streamlit (Dashboard)...
wsl bash -c "pkill -f 'streamlit' || true" >nul 2>&1

echo [3/3] Deteniendo contenedores de Docker (PostGIS / PgAdmin)...
cd /d "%~dp0"
docker compose down >nul 2>&1

echo.
echo ============================================
echo   [OK] Todos los servicios fueron detenidos
echo        correctamente y de forma limpia.
echo ============================================
echo.
pause