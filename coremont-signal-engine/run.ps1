# run.ps1 — Windows PowerShell launcher for the Coremont Signal Engine.
#
#   .\run.ps1                       # set up (if needed), seed, and serve on :8000
#   $env:PORT=9000; .\run.ps1       # use a different port
#
# If PowerShell blocks the script ("running scripts is disabled"), run:
#   powershell -ExecutionPolicy Bypass -File .\run.ps1
#
# Requires Python 3.11+ from https://www.python.org/downloads/ (tick "Add to PATH").

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$Port = if ($env:PORT) { $env:PORT } else { "8000" }

function Test-Py($exe, $pre) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $false }
    & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

# Find a Python 3.11+ interpreter (Windows usually exposes 'py' and/or 'python').
$PyExe = $null; $PyPre = @()
if     (Test-Py "py" @("-3"))   { $PyExe = "py";     $PyPre = @("-3") }
elseif (Test-Py "python" @())   { $PyExe = "python"; $PyPre = @() }
else {
    Write-Host "ERROR: Need Python 3.11+ but couldn't find it." -ForegroundColor Red
    Write-Host "  Install from https://www.python.org/downloads/ and CHECK 'Add python.exe to PATH'." -ForegroundColor Red
    Write-Host "  Then close and reopen PowerShell and re-run .\run.ps1" -ForegroundColor Red
    exit 1
}
Write-Host "-> Using Python: $(& $PyExe @PyPre --version)"

# Virtualenv (call the venv's python by path — no activation, avoids policy issues).
if (-not (Test-Path ".venv")) {
    Write-Host "-> Creating virtualenv (.venv)..."
    & $PyExe @PyPre -m venv .venv
}
$VPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VPy)) {
    Write-Host "ERROR: virtualenv python not found at $VPy" -ForegroundColor Red
    exit 1
}

Write-Host "-> Installing dependencies..."
& $VPy -m pip install --quiet --upgrade pip
& $VPy -m pip install --quiet -r requirements.txt

Write-Host "-> Initializing + seeding database..."
& $VPy -m app.cli initdb
& $VPy -m app.cli ingest --seed

Write-Host ""
Write-Host "==================================================================="
Write-Host "  Coremont Signal Engine running:  http://localhost:$Port"
Write-Host "  (Ctrl-C to stop)"
Write-Host "==================================================================="
Write-Host ""
& $VPy -m uvicorn app.web.server:app --reload --host 127.0.0.1 --port $Port
