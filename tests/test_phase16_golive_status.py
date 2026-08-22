"""P-register: go-live status persistence (probe/test green stamp + TTL load).

Pure, deterministic, zero-network. Verifies the audit gate can reflect a real
green probe/test run without forcing anything.
"""
from __future__ import annotations

import json
import os
import time

from src.notifier.golive_status import DEFAULT_PATH, load, stamp


def test_stamp_then_load_within_ttl(tmp_path, monkeypatch):
    p = tmp_path / ".golive_status.json"
    monkeypatch.setenv("GOLIVE_STATUS_PATH", str(p))
    # re-import after env set so DEFAULT_PATH picks the temp file
    import importlib
    from src.notifier import golive_status
    importlib.reload(golive_status)
    golive_status.stamp("probe_green")
    golive_status.stamp("tests_green")
    out = golive_status.load()
    assert out["probe_green"] is True
    assert out["tests_green"] is True


def test_missing_file_is_fail_safe(tmp_path, monkeypatch):
    p = tmp_path / "nope.json"
    monkeypatch.setenv("GOLIVE_STATUS_PATH", str(p))
    import importlib
    from src.notifier import golive_status
    importlib.reload(golive_status)
    out = golive_status.load()
    assert out == {"probe_green": False, "tests_green": False}


def test_stale_entry_expires(tmp_path, monkeypatch):
    p = tmp_path / ".golive_status.json"
    monkeypatch.setenv("GOLIVE_STATUS_PATH", str(p))
    import importlib
    from src.notifier import golive_status
    importlib.reload(golive_status)
    # Write an entry far in the past (TTL = 1s for the test)
    old = int(time.time() * 1000) - 10_000_000
    p.write_text(json.dumps({"probe_green": old, "tests_green": old}))
    out = golive_status.load(ttl_s=1)
    assert out["probe_green"] is False
    assert out["tests_green"] is False


def test_load_never_raises_on_garbage(tmp_path, monkeypatch):
    p = tmp_path / ".golive_status.json"
    monkeypatch.setenv("GOLIVE_STATUS_PATH", str(p))
    import importlib
    from src.notifier import golive_status
    importlib.reload(golive_status)
    p.write_text("{not valid json")
    out = golive_status.load()
    assert out["probe_green"] is False
