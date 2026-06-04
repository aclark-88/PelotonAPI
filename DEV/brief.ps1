# Coremont Clarion - Morning Brief (one-click).
#
# Double-click this file, or run from PowerShell:   .\brief.ps1
# Pass-through options work too, e.g.:               .\brief.ps1 --days 7 --cap-13f 120
#
# It runs the EDGAR scan, builds briefs\latest.html, and opens the dashboard.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# Locate a real Python (the bare python command is a Windows Store stub on this box).
$py = $null
foreach ($c in @("py", "python3", "python")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { try { & $c --version *> $null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {} }
}
if (-not $py) { Write-Host "No working Python found. Install Python 3.10+ and retry." -ForegroundColor Red; exit 1 }

Write-Host "Coremont Clarion - building this morning brief..." -ForegroundColor Cyan
& $py "tools/morning_brief.py" @args

$html = Join-Path $PSScriptRoot "briefs\latest.html"
if (Test-Path $html) {
    Write-Host "Opening dashboard: $html" -ForegroundColor Green
    Start-Process $html
} else {
    Write-Host "No dashboard was produced - check the output above." -ForegroundColor Yellow
}
