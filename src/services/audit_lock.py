"""Self-healing lock file so concurrent build/run ticks cannot collide.

Per MASTER-PROMPT: a tick that finds a *fresh* lock (<30 min old, same phase,
owned by a live process) skips; a *stale* lock self-heals (is overwritten). The
lock records ``{phase, utc_ts, pid, host}`` so a crashed tick can be detected and
the next tick recovers on its own instead of wedging forever.

This is the concrete mechanism the MASTER-PROMPT calls "self-healing lock-file
discipline". It is wired into ``src/main.py`` (the long-running ``run`` tick) and
exposed as ``scripts/self_heal_locks.py`` for a watchdog cron to call.
"""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = ".audit.lock"
STALE_SECONDS = 30 * 60  # a lock older than 30 min is considered stale


class AuditLockError(RuntimeError):
    """Raised when a *fresh* lock held by another live process is in the way."""


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)          # signal 0: error only if no such process
        return True
    except ProcessLookupError:
        return False             # process is dead -> lock is reclaimable
    except PermissionError:
        return True              # process exists (different uid) -> treat as alive


class AuditLock:
    """Filesystem lock at ``path`` guarding a single in-flight tick/process."""

    def __init__(self, path: str = DEFAULT_PATH, phase: str = "run",
                 stale_seconds: int = STALE_SECONDS) -> None:
        self.path = Path(path)
        self.phase = phase
        self.stale_seconds = stale_seconds
        self._held = False

    # ------------------------------------------------------------- introspection
    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def is_fresh_and_held(self) -> bool:
        rec = self.read()
        if not rec:
            return False
        age = time.time() - float(rec.get("ts", 0))
        return age < self.stale_seconds and _process_alive(rec.get("pid"))

    # ----------------------------------------------------------------- acquire
    def acquire(self) -> dict:
        """Write the lock, self-healing any stale/dead-owned one.

        Raises ``AuditLockError`` if a *fresh* lock owned by another live process
        is already present (a genuine concurrent tick).
        """
        existing = self.read()
        if existing:
            age = time.time() - float(existing.get("ts", 0))
            if age < self.stale_seconds and _process_alive(existing.get("pid")):
                raise AuditLockError(
                    f"fresh lock held by pid={existing.get('pid')} "
                    f"phase={existing.get('phase')!r} age={age:.0f}s "
                    f"(self={os.getpid()})")
            # stale (too old) or dead owner -> self-heal below
        rec = {
            "phase": self.phase,
            "ts": time.time(),
            "utc": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "host": socket.gethostname(),
        }
        self.path.write_text(json.dumps(rec))
        self._held = True
        return rec

    # ----------------------------------------------------------------- release
    def release(self) -> None:
        if self._held and self.path.exists():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._held = False

    # ------------------------------------------------------------- context mgr
    @contextmanager
    def held(self):
        self.acquire()
        try:
            yield self
        finally:
            self.release()
