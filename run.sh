#!/usr/bin/env bash
set -euo pipefail

SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"

if [ -f ".venv/bin/python3" ]; then
    PYTHON_BIN=".venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
else
    PYTHON_BIN="python"
fi

exec "$PYTHON_BIN" -m controller.main --url "$SIM_URL"
