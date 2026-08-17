#!/usr/bin/env bash
# run_tests.sh — compile check + fakes-only pytest suite (zero network),
# plus the no-creds paper smoke against the REAL Bitget demo product.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="./.venv/bin/python"
echo "== compileall =="
"$PY" -m compileall -q src scripts || exit 1
echo "== pytest (fakes-only, zero network) =="
PYTHONPATH=. "$PY" -m pytest "$@" || exit 1
echo "== smoke_paper_run (real demo product, no creds) =="
LEDGER_PATH="$(mktemp -u)" PYTHONPATH=. "$PY" scripts/smoke_paper_run.py || exit 1
