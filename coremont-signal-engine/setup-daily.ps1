# setup-daily.ps1 — register a Windows scheduled task that emails the digest
# every morning.
#
#   powershell -ExecutionPolicy Bypass -File .\setup-daily.ps1            # 7:00 AM
#   powershell -ExecutionPolicy Bypass -File .\setup-daily.ps1 -Time 06:30
#
# Remove it later with:
#   Unregister-ScheduledTask -TaskName "CoremontSignalDigest" -Confirm:$false

param([string]$Time = "07:00")

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$script = Join-Path $here "daily-digest.ps1"

if (-not (Test-Path $script)) {
    Write-Host "ERROR: daily-digest.ps1 not found next to this script." -ForegroundColor Red
    exit 1
}

$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# Catch up if the PC was off at the scheduled time; allow it to run on battery.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "CoremontSignalDigest" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily Coremont Signal Engine digest email" -Force | Out-Null

Write-Host "Scheduled 'CoremontSignalDigest' to run daily at $Time." -ForegroundColor Green
Write-Host "Test it now with:  Start-ScheduledTask -TaskName CoremontSignalDigest"
Write-Host "Then check exports\digest.log for output."
