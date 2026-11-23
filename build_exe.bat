@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --windowed --name "AI Video Studio Local" --add-data "static;static" --collect-all keyring --collect-all webview desktop.py
if errorlevel 1 exit /b 1
echo.
echo Ejecutable creado en: dist\AI Video Studio Local\AI Video Studio Local.exe
pause
