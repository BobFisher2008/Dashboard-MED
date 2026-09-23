#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1
python -m bokeh serve --show app.py --use-xheaders --allow-websocket-origin=asiapharma-med.website --allow-websocket-origin=192.168.2.151:5006 --allow-websocket-origin=localhost:5006
