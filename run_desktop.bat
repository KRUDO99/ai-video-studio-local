@echo off
setlocal
cd /d "%~dp0"
set "APP_DATA_DIR=%LOCALAPPDATA%\AI Video Studio Local"
set "APP_PYTHON=%APP_DATA_DIR%\venv\Scripts\python.exe"
if not exist "%APP_PYTHON%" (
  if not exist "%APP_DATA_DIR%" mkdir "%APP_DATA_DIR%"
  py -3.11 -m venv "%APP_DATA_DIR%\venv"
  if errorlevel 1 (
    echo No se encontro Python 3.11. Instala Python 3.11 y vuelve a iniciar.
    pause
    exit /b 1
  )
)
"%APP_PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo No se pudieron instalar las dependencias. Revisa tu conexion e intenta de nuevo.
  pause
  exit /b 1
)
"%APP_PYTHON%" desktop.py
pause
