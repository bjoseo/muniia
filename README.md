# TRABAJO PRÁCTICO INTEGRADOR

# Sistema Inteligente de Gestión de Reclamos Ciudadanos con Voz

# MuniIA-Reclamos - MVP

Descripción General
Actualmente, la gestión de reclamos ciudadanos en municipios del conurbano bonaerense, como Avellaneda, se realiza de forma fragmentada y mayormente manual. Un vecino reporta un bache, un foco quemado o un basural por canales no integrados: llamada telefónica, mensaje de WhatsApp, formulario web, redes sociales o atención presencial. Esto genera pérdida de información (audios de WhatsApp que no se transcriben, fotos sin georreferenciar), duplicidad y falta de trazabilidad (el mismo reclamo entra por 3 canales distintos y se cuenta 3 veces), demoras en la derivación (una persona tiene que escuchar, interpretar y derivar manualmente al área correspondiente) y falta de feedback al contribuyente. El problema no es la falta de voluntad del municipio, sino la falta de un sistema integrador que convierta lenguaje natural (texto y voz) en datos estructurados, accionables y medibles. El proyecto propone unificar esos canales en una única base de datos inteligente, con IA para transcribir, clasificar y priorizar automáticamente.

## Contenido de la carpeta

- `Monitor_Whatsapp_Baileys.js`: conecta con WhatsApp y guarda los mensajes recibidos en la bandeja de entrada.
- `procesar_inbox_v4.py`: procesa los mensajes y audios de la bandeja y registra los reclamos en PostgreSQL/PostGIS.
- `scr/dashboard.py`: dashboard web para consultar y administrar los reclamos.
- `docker-compose.yml`: configura PostgreSQL con PostGIS y pgAdmin.
- `iniciar_muniIA_demo.bat` e `iniciar.sh`: inician los servicios en Windows y WSL/Ubuntu, respectivamente.
- `detener_muniIA.bat`: detiene los procesos de la aplicación y los contenedores.
- `.env.ejemplo`: plantilla de configuración de la base de datos.
- `init.sql/`, `img/`, `msg/`, `shp/`, `sql/`, `auth_info_baileys/` y `whatsapp_inbox/`: carpetas de inicialización, recursos, archivos de entrada y datos de sesión. Algunas se crean durante la ejecución; otras pueden existir solo en copias locales y no están incluidas en GitHub.

## Descargar el proyecto

Se puede clonar con Git desde PowerShell:

```powershell
cd C:\
git clone https://github.com/bjoseo/muniia.git
cd muniia
```

O descargar un ZIP desde [GitHub](https://github.com/bjoseo/muniia) con **Code > Download ZIP** y extraerlo. Para los pasos siguientes se supone que el proyecto quedó en `C:\muniia`; si se eligió otra ubicación, hay que ajustar la ruta equivalente en WSL.

## Requisitos

- Windows con Docker Desktop instalado y en ejecución, con integración WSL 2 habilitada.
- Una distribución WSL llamada `Ubuntu`.
- Git (si se clona el proyecto).
- Python 3, Node.js y npm disponibles dentro de Ubuntu.
- `ffmpeg` para el procesamiento de audios.

## Configuración y ejecución

1. En la carpeta del proyecto, crear la configuración local a partir de la plantilla:

   ```powershell
   Copy-Item .env.ejemplo .env
   notepad .env
   ```

   Definir en `.env` una contraseña propia para `DB_PASS`. No publicar este archivo ni compartirlo con credenciales reales.

2. Abrir Ubuntu desde PowerShell y preparar el entorno Python. El ejemplo instala las dependencias utilizadas por el procesador y el dashboard:

   ```powershell
   wsl -d Ubuntu
   ```

   Ya dentro de Ubuntu:

   ```bash
   sudo apt update
   sudo apt install -y python3-venv ffmpeg nodejs npm
   cd /mnt/c/muniia
   python3 -m venv .venv
   .venv/bin/pip install --upgrade pip
   .venv/bin/pip install psycopg2-binary python-dotenv pydub openai-whisper streamlit pandas streamlit-autorefresh folium streamlit-folium
   exit
   ```

3. Desde el Explorador de Windows, ejecutar `iniciar_muniIA_demo.bat` (o desde PowerShell: `.\iniciar_muniIA_demo.bat`). El script levanta PostGIS y pgAdmin con Docker y luego inicia el monitor de WhatsApp, el procesador y el dashboard en Ubuntu. La primera ejecución también instala las dependencias de Node.js.

4. Abrir el dashboard en <http://localhost:8501>. pgAdmin queda disponible en <http://localhost:5050>; sus credenciales se configuran en `.env`.

Para detener los servicios, ejecutar `detener_muniIA.bat`.

## Consideraciones

- La primera conexión de WhatsApp puede mostrar un código QR en los registros del monitor para vincular el dispositivo. La sesión queda guardada en `auth_info_baileys/`; es información privada y no debe publicarse ni compartirse.
- Los datos y contraseñas de `.env`, los archivos de sesión y los logs son locales. No subirlos a GitHub.
- GitHub no incluye las carpetas locales ignoradas (`img/`, `msg/`, `shp/`, `sql/` y `whatsapp_inbox/`), ni las credenciales de WhatsApp. Si la instalación requiere datos o scripts SQL adicionales, hay que obtenerlos por separado y cargarlos en la base antes de procesar reclamos.
- El proyecto es un MVP de demostración. Antes de usarlo, verificar que el esquema y los datos requeridos estén cargados en PostgreSQL/PostGIS; levantar el contenedor por sí solo no garantiza que la base esté inicializada con ellos.
