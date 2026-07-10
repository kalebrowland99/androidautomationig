#!/bin/bash
cd "$(dirname "$0")" || exit 1
source .venv/bin/activate
pip install -q -r tools/requirements.txt
open "http://127.0.0.1:8080" 2>/dev/null &
exec python3 -m dashboard
