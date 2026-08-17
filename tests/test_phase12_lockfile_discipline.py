"""Regression + unit tests for the MASTER-PROMPT self-healing lock-file discipline.

The build previously *claimed* ``self_heal_locks.py`` existed and was cron-wired,
but shipped neither the file nor the wiring. These tests pin the real mechanism:
``.audit.lock`` records ``{phase, utc_ts, pid, host}``; a fresh lock owned by a
live process blocks a concurrent tick; a stale or dead-owned lock self-heals.
"""

from __future__ import annotations

import os
import time

from src.services.audit_lock import AuditLock, AuditLockError


def test_fresh_lock_records_phase_and_ts(tmp_path):
    lock = AuditLock(path=str(tmp_path / ".audit.lock"), phase="run")
    with lock.held() as l:
        rec = l.read()
        assert rec is not None
        assert rec["phase"] == "run"
        assert "ts" in rec and "utc" in rec and "pid" in rec and "host" in rec
        assert l.is_fresh_and_held()
    # released on context exit
    assert lock.read() is None


def test_concurrent_fresh_lock_is_refused(tmp_path):
    p1 = str(tmp_path / "a.lock")
    l1 = AuditLock(path=p1, phase="run")
    l1.acquire()  # held by THIS (live) process
    l2 = AuditLock(path=p1, phase="run")
    try:
        l2.acquire()
        raise AssertionError("second live process must be refused")
    except AuditLockError:
        pass
    finally:
        l1.release()


def test_stale_lock_self_heals(tmp_path):
    p1 = str(tmp_path / "stale.lock")
    rec = {"phase": "run", "ts": time.time() - 3600, "utc": "",
           "pid": 999999, "host": "x"}  # 1h old, impossible pid
    AuditLock(path=p1).path.write_text(__import__("json").dumps(rec))
    lock = AuditLock(path=p1, phase="run")
    # a stale/dead-owned lock must not raise; acquire overwrites it
    out = lock.acquire()
    assert out["pid"] == os.getpid()
    lock.release()


def test_self_heal_script_reclaims_stale_dead_lock(tmp_path):
    import subprocess
    import sys
    p1 = tmp_path / "sh.lock"
    dead = {"phase": "run", "ts": time.time() - 9999, "utc": "",
            "pid": 123456, "host": "ghost"}
    p1.write_text(__import__("json").dumps(dead))
    env = dict(os.environ)
    env["LOCK_PATH"] = str(p1)
    r = subprocess.run([sys.executable, "scripts/self_heal_locks.py"],
                       capture_output=True, text=True, env=env,
                       cwd="/root/bitget-listener")
    assert r.returncode == 0, r.stderr
    assert not p1.exists(), "self-heal must remove the stale/dead lock"
