<#
.SYNOPSIS
    Keep Dagster (UI + daemon + sensors) running in the background.

    Started at logon by the "ROP Dagster" scheduled task (install_dagster_autostart.ps1).
    Runs run_dagster.bat without a console window, so there is no window to close by
    accident, and starts it again within a minute whenever it stops. When a Dagster is
    already listening on the UI port (e.g. started by hand), it waits instead of starting
    a second one.

    Output: logs\dagster.log (the previous run's log is kept as dagster.log.1).
#>
param([int]$Port = 3000, [int]$RetrySeconds = 60)

$Project = $PSScriptRoot
$Launcher = Join-Path $Project "run_dagster.bat"
$LogDir = Join-Path $Project "logs"
$Log = Join-Path $LogDir "dagster.log"
$ErrLog = Join-Path $LogDir "dagster.err.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Note([string]$Text) {
    Add-Content -Path (Join-Path $LogDir "dagster_supervisor.log") -Encoding UTF8 -Value "$(Get-Date -Format 's')  $Text"
}

function Test-DagsterUp {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

Write-Note "supervisor started (pid $PID)"
while ($true) {
    if (Test-DagsterUp) {
        Start-Sleep -Seconds $RetrySeconds  # already running (by hand or a previous start)
        continue
    }
    foreach ($f in @($Log, $ErrLog)) {
        if (Test-Path $f) { Move-Item -Force $f "$f.1" }
    }
    Write-Note "starting Dagster"
    $process = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$Launcher`"" -WorkingDirectory $Project `
        -WindowStyle Hidden -RedirectStandardOutput $Log -RedirectStandardError $ErrLog -PassThru
    $process.WaitForExit()
    Write-Note "Dagster exited with code $($process.ExitCode); restarting in $RetrySeconds s"
    Start-Sleep -Seconds $RetrySeconds
}
