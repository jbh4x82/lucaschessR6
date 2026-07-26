#!/bin/bash
# Start Lucas Chess R6 on macOS. Double-click in Finder or run from a terminal.
cd "$(dirname "$0")/bin" || exit 1

VENV_PYTHON="../.venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "The virtual environment is missing. Create it with:"
    echo "  python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

exec "$VENV_PYTHON" LucasR.py "$@"
