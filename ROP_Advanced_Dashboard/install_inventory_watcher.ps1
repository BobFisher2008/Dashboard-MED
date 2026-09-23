<#
.SYNOPSIS
    Run the inventory watcher in the background at logon (Windows Task Scheduler).

.EXAMPLE
    .\install_inventory_watcher.ps1                 # install and start now
    .\install_inventory_watcher.ps1 -WatchDir "D:\Exports\Inventory"
    .\install_inventory_watcher.ps1 -Status         # show task state and last log lines
    .\install_inventory_watcher.ps1 -Uninstall      # stop and remove the task
#>
param(
    [string]$WatchDir = (Join-Path $PSScriptRoot "inbox"),
    [string]$ExtraArgs = "",
    [string]$TaskName = "ROP Inventory Watcher",
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$Project = $PSScriptRoot
$LogFile = Join-Path $Project "logs\inventory_watcher.log"

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { Write-Host "Task '$TaskName' is not installed."; exit 1 }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "State: $($task.State)  Last run: $($info.LastRunTime)  Last result: $($info.LastTaskResult)"
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 15 -Encoding UTF8 }
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

# pythonw.exe runs without a console window; fall back to python.exe.
$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

& $python -c "import watchdog, duckdb" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Missing packages. Run: python -m pip install -r `"$Project\requirements.txt`""
}

New-Item -ItemType Directory -Force -Path $WatchDir | Out-Null
$script = Join-Path $Project "watch_inventory.py"
$arguments = "`"$script`" --watch-dir `"$WatchDir`" --project `"$Project`" $ExtraArgs".Trim()

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Refreshes the ROP dashboard warehouse when an inventory file lands in $WatchDir" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed and started '$TaskName'."
Write-Host "  Watching: $WatchDir"
Write-Host "  Log:      $LogFile"
Write-Host "Check with: .\install_inventory_watcher.ps1 -Status"
