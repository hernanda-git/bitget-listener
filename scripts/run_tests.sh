#!/usr/bin/env bash
# run_tests.sh — compile check + fakes-only pytest suite (zero network),
# plus the no-creds paper smoke against the REAL Bitget demo product.
#
# bot_common lives in /opt/bots (sibling of this repo). The live systemd unit
# sets PYTHONPATH to include both, so the test harness must mirror that or the
# smoke_paper_run.py leg (which imports src.main -> pg_ledger -> bot_common)
# fails with ModuleNotFoundError. Derive it from this script's location.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
# /opt/bots/bitget-listener -> parent /opt/bots holds bot_common
SIBLING_PARENT="$(dirname "$REPO_ROOT")"
export PYTHONPATH="$REPO_ROOT:$SIBLING_PARENT${PYTHONPATH:+:$PYTHONPATH}"
PY="./.venv/bin/python"
echo "== compileall =="
"$PY" -m compileall -q src scripts || exit 1
echo "== pytest (fakes-only, zero network) =="
"$PY" -m pytest "$@" || exit 1
# Record the green test run so the live bot's audit gate can reflect reality
# (see src/notifier/golive_status.py). Advisory only.
"$PY" - <<'PY' 2>/dev/null || true
from src.notifier.golive_status import stamp
stamp("tests_green")
PY
echo "== smoke_paper_run (real demo product, no creds) =="
LEDGER_PATH="$(mktemp -u)" "$PY" scripts/smoke_paper_run.py || exit 1
