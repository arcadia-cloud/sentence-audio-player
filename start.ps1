param([int]$Port = 8765)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venv)) {
    Write-Host "Creating venv..."
    python -m venv (Join-Path $root ".venv")
    & $venv -m pip install --upgrade pip
    & $venv -m pip install -r (Join-Path $root "requirements.txt")
}

$env:PYTHONUTF8 = "1"
Push-Location (Join-Path $root "backend")
try {
    Write-Host "Listening Studio -> http://127.0.0.1:$Port"
    & $venv -m uvicorn app.main:app --host 127.0.0.1 --port $Port
}
finally {
    Pop-Location
}
