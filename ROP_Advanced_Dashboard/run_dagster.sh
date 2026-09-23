#!/usr/bin/env bash
# Start the Dagster UI and daemon for the ROP warehouse (Linux / macOS).
set -euo pipefail
cd "$(dirname "$0")"
export DAGSTER_HOME="$PWD/dagster_home"
export PYTHONIOENCODING=utf-8
mkdir -p "$DAGSTER_HOME"
exec python -m dagster dev -m dagster_rop.definitions
