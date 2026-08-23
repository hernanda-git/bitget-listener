"""Durable "last known green" status for the go-live audit gate.

The go-live ``GateConditions`` (probe_green / tests_green) used to be impossible
to satisfy on a live box: they were only ever set transiently inside a
``--go-live`` invocation from ``PROBE_GREEN``/``TESTS_GREEN`` env vars that the
live process never sets. So the audit gate showed (a)/(c) as permanently unmet
even when the operator had genuinely run a green probe and a green test suite.

This module makes the gate *reflect reality*: the standalone probe script and the
test runner stamp a timestamp here on success, and the live bot reads it back at
startup (within a TTL) to seed ``probe_green`` / ``tests_green``. It is purely an
audit signal — it never flips ``paper`` and never forces a trade path. A stale or
missing file simply leaves the flags False (fail-safe).

No network. Best-effort file IO only.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

StatusKey = Literal["probe_green", "tests_green", "real_fee_observed",
                    "real_liq_price_observed"]

# Default location: repo-local, git-ignored intent (it is runtime state, not source).
DEFAULT_PATH = os.getenv(
    "GOLIVE_STATUS_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), ".golive_status.json"),
)

# A green check is trusted for this long. After that it must be re-run.
TTL_S = int(os.getenv("GOLIVE_STATUS_TTL_S", str(24 * 3600)))


def _now_ms() -> int:
    return int(time.time() * 1000)


def stamp(key: StatusKey, path: str = DEFAULT_PATH) -> None:
    """Record that ``key`` went green just now. Best-effort, never raises."""
    try:
        data: dict[str, int] = {}
        if os.path.exists(path):
            try:
                data = json.loads(Path(path).read_text() or "{}")
            except Exception:
                data = {}
        data[key] = _now_ms()
        Path(path).write_text(json.dumps(data))
        log.info("go-live status stamped %s at %s", key, path)
    except Exception as exc:  # noqa: BLE001 - status is advisory only
        log.debug("go-live status stamp failed (%s): %s", key, exc)


def load(path: str = DEFAULT_PATH, ttl_s: int = TTL_S) -> dict[StatusKey, bool]:
    """Return which conditions count as green within TTL. Fail-safe -> all False."""
    out: dict[StatusKey, bool] = {
        "probe_green": False,
        "tests_green": False,
        "real_fee_observed": False,
        "real_liq_price_observed": False,
    }
    try:
        if not os.path.exists(path):
            return out
        data = json.loads(Path(path).read_text() or "{}")
        now = _now_ms()
        for k in out:
            ts = data.get(k)
            if isinstance(ts, int) and (now - ts) <= ttl_s * 1000:
                out[k] = True
            else:
                log.debug("go-live status %s stale/absent (age_s=%s, ttl_s=%s)",
                          k, (now - ts) / 1000 if isinstance(ts, int) else "n/a", ttl_s)
    except Exception as exc:  # noqa: BLE001 - never let status break startup
        log.debug("go-live status load failed: %s", exc)
    return out
