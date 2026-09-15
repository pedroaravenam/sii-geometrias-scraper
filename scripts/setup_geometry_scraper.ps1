$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repositoryRoot ".venv"
$pythonPath = Join-Path $venvPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host "Creando entorno Python local en .venv..."
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv $venvPath
    }
    else {
        python -m venv $venvPath
    }
}

Push-Location $repositoryRoot
try {
    Write-Host "Actualizando pip e instalando dependencias geoespaciales..."
    & $pythonPath -m pip install --upgrade pip
    & $pythonPath -m pip install -r (Join-Path $repositoryRoot "requirements-geometry.txt")

    Write-Host "Verificando instalación..."
    & $pythonPath -m sii_geometry doctor
    Write-Host "Configurando respaldo centralizado..."
    & $pythonPath -m sii_geometry configure-storage
}
finally {
    Pop-Location
}

Write-Host "Listo. Para abrir el selector:"
Write-Host ".\.venv\Scripts\python.exe -m sii_geometry scrape"
