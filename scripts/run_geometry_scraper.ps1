$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $repositoryRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Falta .venv. Ejecuta primero scripts\setup_geometry_scraper.ps1"
}

Push-Location $repositoryRoot
try {
    & $pythonPath -m sii_geometry scrape @args
}
finally {
    Pop-Location
}
