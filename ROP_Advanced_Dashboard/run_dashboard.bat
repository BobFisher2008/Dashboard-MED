@echo off
cd /d "%~dp0"
python -m bokeh serve --show app.py
pause
