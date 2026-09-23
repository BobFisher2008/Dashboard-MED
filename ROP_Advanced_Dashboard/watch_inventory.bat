@echo off
REM Watch inbox\ and refresh the dashboard data whenever an inventory file is dropped in.
cd /d "%~dp0"
python watch_inventory.py %*
pause
