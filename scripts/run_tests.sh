#!/usr/bin/env bash
# run_tests.sh — compile check + fakes-only pytest suite (zero network).
set -uo pipefail
cd "$(dirname "$0")/.."
PY="./.venv/bin/python"
echo "== compileall =="
"$PY" -m compileall -q src scripts || exit 1
echo "== pytest =="
PYTHONPATH=. "$PY" -m pytest "$@"
