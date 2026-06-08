# run.ps1 — Windows PowerShell launcher for the Coremont Signal Engine.
#
#   .\run.ps1                       # set up, pull TODAY'S live SEC data, serve on :8000
#   $env:SEED=1; .\run.ps1          # use bundled sample data instead (offline)
#   $env:PORT=9000; .\run.ps1       # use a different port
#
# If PowerShell blocks the script ("running scripts is disabled"), run:
#   powershell -ExecutionPolicy Bypass -File .\run.ps1
#
# Requires Python 3.11+ from https://www.python.org/downloads/ (tick "Add to PATH").

Set-Location -Path $PSScriptRoot

$Port = if ($env:PORT) { $env:PORT } else { "8000" }

# Probe a candidate interpreter. Returns "MAJOR.MINOR" on success, else $null.
# Robust against Windows' Store-alias and install-manager stubs (which print
# noise like "Python was not found..." instead of a version) and never throws.
function Get-PyVersion($exe, $pre) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $null }
    $old = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        $out = (& $exe @pre -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>&1 | Out-String).Trim()
    } catch { $out = "" } finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE -ne 0) { return $null }
    foreach ($line in ($out -split "`r?`n")) {
        if ($line.Trim() -match '^\d+\.\d+$') { return $line.Trim() }
    }
    return $null
}

function Version-OK($v) {
    if (-not $v) { return $false }
    $p = $v.Split("."); return ([int]$p[0] -gt 3) -or ([int]$p[0] -eq 3 -and [int]$p[1] -ge 11)
}

# Find a real Python 3.11+ interpreter.
$PyExe = $null; $PyPre = @()
foreach ($c in @(@("py", @("-3")), @("python", @()), @("python3", @()), @("py", @()))) {
    $v = Get-PyVersion $c[0] $c[1]
    if (Version-OK $v) { $PyExe = $c[0]; $PyPre = $c[1]; $PyVer = $v; break }
}

if (-not $PyExe) {
    Write-Host ""
    Write-Host "ERROR: No real Python 3.11+ found (only Windows stubs are present)." -ForegroundColor Red
    Write-Host "  Install Python, then reopen PowerShell and re-run this script:" -ForegroundColor Yellow
    Write-Host "    1. Go to https://www.python.org/downloads/  ->  Download Python 3.12" -ForegroundColor Yellow
    Write-Host "    2. Run the installer and TICK 'Add python.exe to PATH' (bottom of first screen)" -ForegroundColor Yellow
    Write-Host "    3. Close ALL PowerShell windows, open a new one" -ForegroundColor Yellow
    Write-Host "    4. Verify with:  py --version   (should say 3.12.x)" -ForegroundColor Yellow
    Write-Host "    5. cd into this folder and run:  powershell -ExecutionPolicy Bypass -File .\run.ps1" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  (Tip: if 'python' opens the Microsoft Store, turn OFF the alias under" -ForegroundColor DarkGray
    Write-Host "   Settings > Apps > Advanced app settings > App execution aliases.)" -ForegroundColor DarkGray
    exit 1
}

$ErrorActionPreference = "Stop"
Write-Host "-> Using Python $PyVer ($PyExe $($PyPre -join ' '))"

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

Write-Host "-> Initializing database..."
& $VPy -m app.cli initdb

if ($env:SEED) {
    Write-Host "-> Loading bundled SAMPLE data (because `$env:SEED is set)..."
    & $VPy -m app.cli ingest --seed
} else {
    Write-Host "-> Pulling TODAY'S live SEC Form D data (set `$env:SEED=1 for offline sample)..."
    & $VPy -m app.cli ingest
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   Live SEC pull failed (offline / network?). Falling back to sample data." -ForegroundColor Yellow
        & $VPy -m app.cli ingest --seed
    }
}

Write-Host ""
Write-Host "==================================================================="
Write-Host "  Coremont Signal Engine running:  http://localhost:$Port"
Write-Host "  (Ctrl-C to stop)"
Write-Host "==================================================================="
Write-Host ""
& $VPy -m uvicorn app.web.server:app --reload --host 127.0.0.1 --port $Port
