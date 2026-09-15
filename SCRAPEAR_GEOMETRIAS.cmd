@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto storage
call scripts\setup_geometry_scraper.cmd
if errorlevel 1 exit /b 1

:storage
if exist "config\sii_geometry.local.json" goto run
".venv\Scripts\python.exe" -m sii_geometry configure-storage
if errorlevel 1 exit /b 1

:run
call scripts\run_geometry_scraper.cmd %*
