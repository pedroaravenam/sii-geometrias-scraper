@echo off
setlocal
chcp 65001 >nul

for %%I in ("%~dp0..") do set "SCRAPER_ROOT=%%~fI"
cd /d "%SCRAPER_ROOT%"

if not exist ".venv\Scripts\python.exe" (
    echo Falta .venv. Ejecuta primero scripts\setup_geometry_scraper.cmd
    exit /b 1
)

".venv\Scripts\python.exe" -m sii_geometry scrape %*
