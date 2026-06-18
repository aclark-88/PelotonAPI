# Coremont Clarion - open this morning's verified brief.
#
# Pulls the latest candidates + verdicts that the cloud produced, renders the
# dashboard locally (no SEC needed), and opens it in the default browser.
# Run by a Windows scheduled task each weekday morning (see setup-morning.ps1).

$ErrorActionPreference = "SilentlyContinue"
Set-Location -LiteralPath $PSScriptRoot
$branch = "claude/coremont-edgar-signal-engine-KQYJb"

# Grab just the two data files the cloud/GitHub runs commit (no merge, no conflicts).
git fetch origin $branch 2>$null
git checkout "origin/$branch" -- "briefs/candidates.json" "config/verifications.json" "config/surfaced.json" 2>$null

# Locate a working Python (the bare `python` is a Store stub on this box).
$py = $null
foreach ($c in @("py", "python3", "python")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { try { & $c --version *> $null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {} }
}

if ($py) {
    & $py "tools/morning_brief.py" "--from-candidates" "briefs/candidates.json" *> $null
}

$html = Join-Path $PSScriptRoot "briefs\latest.html"
if (Test-Path $html) { Start-Process $html }
