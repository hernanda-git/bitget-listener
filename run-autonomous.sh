#!/usr/bin/env bash
# ============================================================================
# run-autonomous.sh  —  Bootstrap + launch the bitget-listener autonomous build
#
# On the OTHER machine (fresh checkout of hernanda-git/bitget-listener):
#   1. git clone https://github.com/hernanda-git/bitget-listener
#   2. cd bitget-listener
#   3. bash run-autonomous.sh
#
# What it does:
#   - Extracts the 4 sibling repos from sibling-repos/siblings.tar.gz
#   - Verifies Python 3.11+ and pip/uv
#   - Installs/updates Hermes Agent CLI (pip install hermes-agent)
#   - Auths Hermes (interactive first run; token cached after)
#   - Launches Hermes with MASTER-PROMPT.md as the goal, in autonomous mode
#
# It NEVER places a real Bitget order (the bot itself is paper-by-default; see
# MASTER-PROMPT.md hard rules). This script only builds the code + runs tests.
# ============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "==> [bitget-listener autonomous bootstrap] repo dir: $REPO_DIR"

# ---- 1. Extract sibling repos (if not already) -------------------------------
if [ ! -d "learnernoearner-listener" ]; then
  echo "==> Extracting sibling repos from sibling-repos/siblings.tar.gz ..."
  tar -xzf sibling-repos/siblings.tar.gz
  echo "    extracted: $(ls -d */ | tr '\n' ' ')"
else
  echo "==> Sibling repos already present, skipping extraction."
fi

# ---- 2. Python check --------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.11+ then re-run." >&2
  exit 1
fi
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
echo "==> Python $PYV detected."
python3 - "$PYV" <<'PY'
import sys
v=sys.argv[1].split(".")
if not (int(v[0])>=3 and int(v[1])>=11):
    sys.exit("ERROR: need Python >=3.11 (got %s)." % sys.argv[1])
PY

# ---- 3. Hermes Agent CLI ----------------------------------------------------
if command -v hermes >/dev/null 2>&1; then
  echo "==> Hermes already installed: $(hermes --version 2>/dev/null || echo unknown)"
else
  echo "==> Installing Hermes Agent CLI ..."
  python3 -m pip install --upgrade pip
  python3 -m pip install hermes-agent
fi

# ---- 4. Auth (interactive on first run; cached afterwards) ------------------
echo "==> Ensuring Hermes is authed (you may be prompted once) ..."
hermes auth status >/dev/null 2>&1 || hermes auth login

# ---- 5. Launch autonomous build --------------------------------------------
echo "==> Launching autonomous build from MASTER-PROMPT.md ..."
echo "    (paper-by-default; no real order will be placed without the go-live gate)"
echo ""

# Preferred: dedicated autonomous/agentic run. Fall back to `hermes run` if the
# subcommand differs across versions. Edit the line below to match the installed CLI.
if hermes autonomous --help >/dev/null 2>&1; then
  hermes autonomous \
    --goal-file "$REPO_DIR/MASTER-PROMPT.md" \
    --repo "$REPO_DIR" \
    --self-heal-loop \
    --cadence 5m \
    --branch main \
    --push
elif hermes agent --help >/dev/null 2>&1; then
  hermes agent \
    --prompt-file "$REPO_DIR/MASTER-PROMPT.md" \
    --workspace "$REPO_DIR" \
    --autonomous
else
  # Last resort: pipe the prompt as a one-shot instruction.
  hermes run --file "$REPO_DIR/MASTER-PROMPT.md" --workspace "$REPO_DIR"
fi
