@echo off
rem Start the Dagster UI and daemon for the ROP warehouse.
rem UI: http://localhost:3000 - the daemon runs the inbox sensor and the schedule.
setlocal
cd /d "%~dp0"
set DAGSTER_HOME=%~dp0dagster_home
if not exist "%DAGSTER_HOME%" mkdir "%DAGSTER_HOME%"
set PYTHONIOENCODING=utf-8
python -m dagster dev -m dagster_rop.definitions
