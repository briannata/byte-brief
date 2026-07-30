<#
    Registers byte-brief as a daily Windows scheduled task.

    Usage:
        powershell -ExecutionPolicy Bypass -File .\install_task.ps1
        powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Time 07:30
        powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Remove

    Runs as you, in the background, with no console window. If the machine is
    asleep or off at the scheduled time, the task runs at the next opportunity
    instead of silently skipping the day.
#>

param(
    [string]$Time = "08:00",
    [string]$TaskName = "ByteBrief-DailyHN",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $scriptDir "hn_brief.py"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "No task named '$TaskName' is registered." -ForegroundColor Yellow
    } else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    }
    return
}

if (-not (Test-Path $target)) {
    throw "Cannot find hn_brief.py next to this script (looked at $target)"
}
if (-not (Test-Path (Join-Path $scriptDir "config.json"))) {
    Write-Host "Warning: config.json not found. Create it before the first run." -ForegroundColor Yellow
}

# Prefer pythonw.exe -- it has no console window, so the task runs invisibly.
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $pythonCmd) { throw "python.exe is not on your PATH." }
$pythonw = Join-Path (Split-Path -Parent $pythonCmd.Source) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $pythonCmd.Source }

# Validate the time before handing it to the scheduler.
try {
    $parsed = [datetime]::ParseExact($Time, "HH:mm", $null)
} catch {
    throw "-Time must look like 08:00 or 19:45 (24-hour). Got '$Time'."
}

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$target`"" -WorkingDirectory $scriptDir

$trigger = New-ScheduledTaskTrigger -Daily -At $parsed

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Daily top-10 Hacker News email digest." `
    -Force | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName' -- runs daily at $Time." -ForegroundColor Green
Write-Host "  Interpreter : $pythonw"
Write-Host "  Script      : $target"
Write-Host ""
Write-Host "Run it right now to test:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check the log afterward:" -ForegroundColor Cyan
Write-Host "  Get-Content '$(Join-Path $scriptDir "logs\byte-brief.log")' -Tail 20"
Write-Host ""
