# daily-digest.ps1 — refresh data and email the daily digest.
# This is what the Windows scheduled task runs each morning. You normally never
# call it by hand; run .\run.ps1 once first to create the .venv, then
# .\setup-daily.ps1 to schedule this.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$VPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VPy)) {
    Write-Host "ERROR: .venv not found. Run .\run.ps1 once to set up, then retry." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $PSScriptRoot "exports"))) {
    New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot "exports") | Out-Null
}
$log = Join-Path $PSScriptRoot "exports\digest.log"

"--- $(Get-Date -Format s) : daily digest run ---" | Out-File -FilePath $log -Append -Encoding utf8

# Pull latest data + build + email the digest. (digest does live SEC ingest with
# automatic fallback to the bundled sample, builds the HTML, and emails it if
# SMTP is configured in .env.)
& $VPy -m app.cli digest 2>&1 | Tee-Object -FilePath $log -Append
