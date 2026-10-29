@echo off
setlocal enabledelayedexpansion
echo ============================================
echo   MuniIA - Demo - Avellaneda
echo ============================================

echo [1/3] Verificando Docker Desktop...
docker ps >nul 2>&1
if errorlevel 1 (
  echo Docker Desktop no esta corriendo. Abriendo...
  start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
  echo Esperando 10 seg a que inicie...
  timeout /t 10
)

echo [2/3] Levantando PostGIS y servicios Docker...
cd /d "%~dp0"
::docker rm -f muniia_postgis muniia_pgadmin >nul 2>&1
docker compose down --remove-orphans >nul 2>&1
docker compose up -d db pgadmin
echo Esperando PostGIS healthy...
timeout /t 8 /nobreak >nul

echo [3/3] Levantando Servicios en WSL...
wsl -d Ubuntu -e bash -c "cd \"$(wslpath '%~dp0')\" && chmod +x iniciar.sh && ./iniciar.sh"
::wsl -e bash -c "cd $(wslpath '%~dp0') && ./iniciar.sh"

echo.
echo Esperando 30 segundos a que levanten los servicios...
timeout /t 30

echo.
echo Verificando procesos WSL...
wsl -d Ubuntu -e bash -c "ps aux | grep -E 'Monitor_Whatsapp_Baileys.js|node' | grep -v grep; echo '--- LOG Monitor ---'; tail -30 \"$(wslpath '%~dp0')/whatsapp_monitor.log\" 2>/dev/null || tail -30 ~/MuniIA/whatsapp_monitor.log"



echo Abriendo navegador...
start http://localhost:8501

echo.
echo ============================================
echo   MuniIA iniciado correctamente!
echo   - Dashboard: http://localhost:8501
echo   - Logs: dashboard.log / procesador_python.log
echo   - Monitor: wsl -d Ubuntu cat ~/MuniIA/whatsapp.log
echo   - Si pide QR: wsl -d Ubuntu -e bash -c "cd ~/MuniIA && node Monitor.js"

echo ============================================

pause