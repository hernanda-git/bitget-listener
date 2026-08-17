#!/usr/bin/env python3
"""self_heal_locks.py — watchdog for the build/run lock-file discipline.

MASTER-PROMPT requires a ``.audit.lock`` (``{phase, utc_ts}``) so concurrent
ticks do not collide, plus a self-heal step. This script is what a cron hook
calls to enforce that discipline: it inspects ``.audit.lock`` and *self-heals*
a stale or dead-owned lock so the next tick can proceed, printing the action it
took.

    python scripts/self_heal_locks.py            # heal .audit.lock in cwd
    LOCK_PATH=/tmp/x python scripts/self_heal_locks.py

Exit codes: 0 = fresh/held or healed cleanly; 2 = a fresh lock is held by a
live process and must NOT be touched (the caller should skip, not crash).
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.audit_lock import AuditLock, STALE_SECONDS  # noqa: E402


def main() -> int:
    path = os.getenv("LOCK_PATH", ".audit.lock")
    lock = AuditLock(path=path)
    rec = lock.read()
    if not rec:
        print("self-heal: no .audit.lock present — nothing to do")
        return 0
    age = time.time() - float(rec.get("ts", 0))
    alive = lock.is_fresh_and_held()
    if alive:
        print(f"self-heal: FRESH lock held by live pid={rec.get('pid')} "
              f"phase={rec.get('phase')!r} age={age:.0f}s — SKIP (do not touch)")
        return 2
    # stale by age, or owned by a dead process -> reclaim
    print(f"self-heal: reclaiming STALE/dead lock "
          f"(pid={rec.get('pid')} age={age:.0f}s >= {STALE_SECONDS}s) -> removed")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
