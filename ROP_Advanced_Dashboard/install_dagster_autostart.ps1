<#
.SYNOPSIS
    Start Dagster automatically at logon (Windows Task Scheduler), hidden, and keep it running.

.EXAMPLE
    .\install_dagster_autostart.ps1              # install and start now
    .\install_dagster_autostart.ps1 -Status      # task state, supervisor notes, last Dagster log lines
    .\install_dagster_autostart.ps1 -Uninstall   # stop and remove the task (Dagster itself keeps running until closed)
#>
param(
    [string]$TaskName = "ROP Dagster",
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$Project = $PSScriptRoot
$Supervisor = Join-Path $Project "dagster_supervisor.ps1"
$LogDir = Join-Path $Project "logs"

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { Write-Host "Task '$TaskName' is not installed."; exit 1 }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $up = [bool](Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue)
    Write-Host "Task: $($task.State)  Last run: $($info.LastRunTime)  Dagster UI on :3000: $up"
    $notes = Join-Path $LogDir "dagster_supervisor.log"
    if (Test-Path $notes) { Get-Content $notes -Tail 5 -Encoding UTF8 }
    $log = Join-Path $LogDir "dagster.err.log"
    if (Test-Path $log) { Get-Content $log -Tail 10 -Encoding UTF8 }
    exit 0
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "Task '$TaskName' is not installed."
    }
    exit 0
}

& python -c "import dagster, dagster_webserver" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Missing packages. Run: python -m pip install -r `"$Project\requirements.txt`""
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Project `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Supervisor`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Keeps Dagster (inbox and source-file sensors) running for the ROP dashboard" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed and started '$TaskName'."
Write-Host "  UI:    http://localhost:3000"
Write-Host "  Logs:  $LogDir\dagster.log, dagster_supervisor.log"
Write-Host "Check with: .\install_dagster_autostart.ps1 -Status"
