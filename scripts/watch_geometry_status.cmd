@echo off
setlocal
chcp 65001 >nul
title SII - Progreso de geometrías

for %%I in ("%~dp0..") do set "SCRAPER_ROOT=%%~fI"
cd /d "%SCRAPER_ROOT%"

if not exist ".venv\Scripts\python.exe" (
    echo Falta .venv. Ejecuta primero scripts\setup_geometry_scraper.cmd
    pause
    exit /b 1
)

set "SCRAPER_PERIOD=%~1"
if "%SCRAPER_PERIOD%"=="" set "SCRAPER_PERIOD=2026S2"

:refresh
cls
echo Seguimiento local del scraper SII - %SCRAPER_PERIOD%
echo Actualización automática cada 10 segundos. Cierra esta ventana para salir.
echo.
".venv\Scripts\python.exe" -m sii_geometry status --periodo "%SCRAPER_PERIOD%"
timeout /t 10 /nobreak >nul
goto refresh
