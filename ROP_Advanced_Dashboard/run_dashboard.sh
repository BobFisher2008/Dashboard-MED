#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1
python -m bokeh serve --show app.py --allow-websocket-origin=192.168.2.99:5006 --allow-websocket-origin=localhost:5006
