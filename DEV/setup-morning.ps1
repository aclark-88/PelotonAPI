# setup-morning.ps1 — install the weekday "morning brief" auto-open on THIS PC.
#
# Run ONCE:  double-click this file, or in PowerShell:
#     powershell -ExecutionPolicy Bypass -File .\setup-morning.ps1
#
# Thereafter Windows runs morning-open.ps1 every weekday at 7:15 AM (local): it
# pulls the cloud-produced briefs\candidates.json and OPENS briefs\latest.html in
# your default browser — the "a web page just opens with coffee" behaviour.
#
# The 7:15 AM trigger sits after the 6:00 AM ET cloud SEC scan, so the hand-off
# file is ready. If the laptop is asleep/off at 7:15, it runs at next wake.
#
# Remove later:  Unregister-ScheduledTask -TaskName "CoremontMorningBrief" -Confirm:$false

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$TaskName = "CoremontMorningBrief"
$open     = Join-Path $PSScriptRoot "morning-open.ps1"
if (-not (Test-Path $open)) {
    Write-Host "ERROR: morning-open.ps1 not found next to this script." -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$open`"" `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 7:15am

$settings = New-ScheduledTaskSettings -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

# Reinstall cleanly so re-running this script just updates the task.
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Coremont Clarion: pull the cloud SEC scan and open the morning brief dashboard." | Out-Null

Write-Host ""
Write-Host "Installed scheduled task '$TaskName' - weekdays at 7:15 AM." -ForegroundColor Green
Write-Host "Each morning it pulls the cloud scan and opens briefs\latest.html in your browser."
Write-Host ""
Write-Host "Try it right now:   Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Cyan
Write-Host "Remove it later:    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false" -ForegroundColor DarkGray
