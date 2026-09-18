@echo off
set /p INVENTORY=Inventory file full path: 
set /p SNAPSHOT=Snapshot date (YYYY-MM-DD): 
cd /d "%~dp0"
python refresh_inventory.py --inventory "%INVENTORY%" --snapshot-date "%SNAPSHOT%"
pause
