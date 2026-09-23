@echo off
cd /d "%~dp0"
python -m bokeh serve --show app.py --use-xheaders --allow-websocket-origin=asiapharma-med.website --allow-websocket-origin=192.168.2.151:5006 --allow-websocket-origin=localhost:5006
pause
