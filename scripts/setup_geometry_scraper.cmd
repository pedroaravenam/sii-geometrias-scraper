@echo off
setlocal
chcp 65001 >nul

for %%I in ("%~dp0..") do set "SCRAPER_ROOT=%%~fI"
cd /d "%SCRAPER_ROOT%"

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno Python local en .venv...
    where py >nul 2>nul
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 exit /b 1
)

echo Actualizando pip e instalando dependencias geoespaciales...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements-geometry.txt
if errorlevel 1 exit /b 1

echo Verificando instalacion...
".venv\Scripts\python.exe" -m sii_geometry doctor
if errorlevel 1 exit /b 1

echo Configurando respaldo centralizado...
".venv\Scripts\python.exe" -m sii_geometry configure-storage
if errorlevel 1 exit /b 1

echo.
echo Listo. Para abrir el selector:
echo .venv\Scripts\python.exe -m sii_geometry scrape
