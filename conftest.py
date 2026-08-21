"""Test bootstrap.

Makes the suite runnable from a clean checkout by guaranteeing two things the
live service gets from its systemd unit but pytest does not:

1. ``bot_common`` (a shared package that lives one level up, in /opt/bots) is
   importable. Several src modules ``from bot_common import ...`` at import time,
   so without this the orchestrator/golive test modules fail to *collect*.
2. The project root is on sys.path so ``import src...`` works under pytest.

The Postgres-backed tests (orchestrator, golive) need a DSN. They read the same
env vars the running bot uses (PGHOST/PGUSER/PGDATABASE/PGPASSWORD). If those are
unset we export the local fatty_config defaults so ``pytest`` works on the box
without extra flags. Tests that hit the DB are best-effort: they skip rather than
fail when no Postgres is reachable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOTS_ROOT = ROOT.parent  # /opt/bots

for p in (str(ROOT), str(BOTS_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Local defaults so `pytest` works on the deployment box without env wiring.
_DEFAULTS = {
    "PGHOST": "localhost",
    "PGUSER": "app",
    "PGDATABASE": "fatty_config",
    "PGPASSWORD": os.getenv("PGPASSWORD", ""),
}
for k, v in _DEFAULTS.items():
    os.environ.setdefault(k, v)
