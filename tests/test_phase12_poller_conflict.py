"""P12: command poller — 409 conflict tolerance + observability.

No network. Verifies the poller detects Telegram's 409 'terminated by other
getUpdates' (a second consumer of the same token), backs off instead of
crash-looping, and exports the conflict flag for /api/state.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.notifier import bot_poller
from src.notifier.bot_poller import poll_commands


class _Resp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {"ok": True, "result": []}

    def json(self):
        return self._data


async def _await(obj):
    return obj


class _Client:
    """Simulates Telegram: 409s forever (a ghost poller holds the token)."""
    def __init__(self):
        self.calls = 0

    async def get(self, url, params=None, timeout=None):
        self.calls += 1
        return _Resp(status_code=409)

    async def post(self, url, json=None, timeout=None):
        return _Resp()


async def test_409_conflict_sets_flag_and_backs_off(monkeypatch):
    bot_poller.tg_cmd_conflict = False
    monkeypatch.setenv("TG_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(bot_poller, "get_client", lambda: _await(_Client()))
    router = SimpleNamespace(handle=lambda *a, **k: None)

    stop = asyncio.Event()
    task = asyncio.create_task(poll_commands(router, stop=stop))
    await asyncio.sleep(0.3)          # let it hit the 409 path
    stop.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bot_poller.tg_cmd_conflict is True


async def test_no_conflict_when_ok(monkeypatch):
    bot_poller.tg_cmd_conflict = False
    monkeypatch.setenv("TG_BOT_TOKEN", "123:abc")

    class _OkClient:
        async def get(self, url, params=None, timeout=None):
            return _Resp(200, {"ok": True, "result": []})
        async def post(self, url, json=None, timeout=None):
            return _Resp()

    monkeypatch.setattr(bot_poller, "get_client", lambda: _await(_OkClient()))
    router = SimpleNamespace(handle=lambda *a, **k: None)
    stop = asyncio.Event()
    task = asyncio.create_task(poll_commands(router, stop=stop))
    await asyncio.sleep(0.2)
    stop.set()
    # Poller should exit gracefully on the stop signal (no CancelledError needed).
    await asyncio.wait_for(task, timeout=5)
    assert bot_poller.tg_cmd_conflict is False


# ---------------------------------------------------------------- timeout bug
# The live bot logged `[tg-cmd] getUpdates failed:` (blank reason) every ~20.36s
# forever. Cause: the shared notifier client is built with timeout=15 while the
# server-side long-poll is POLL_TIMEOUT_S=25, so httpx aborted EVERY request at
# 15s (15 + the 5s error sleep = the observed 20s cycle). Worse, aborting before
# the response arrived meant the `status_code == 409` branch could never run, so
# tg_cmd_conflict stayed False while command control was in fact dead.
def test_request_timeout_exceeds_the_long_poll():
    """The HTTP read timeout MUST be longer than the server-side long-poll."""
    assert bot_poller.REQUEST_TIMEOUT_S > bot_poller.POLL_TIMEOUT_S


async def test_getupdates_passes_an_explicit_timeout_longer_than_the_poll(monkeypatch):
    """Regression: the call must override the 15s shared-client default."""
    bot_poller.tg_cmd_conflict = False
    bot_poller.tg_cmd_consecutive_errors = 0
    monkeypatch.setenv("TG_BOT_TOKEN", "123:abc")
    seen = {}

    class _CapturingClient:
        async def get(self, url, params=None, timeout=None):
            seen["timeout"] = timeout
            seen["poll"] = (params or {}).get("timeout")
            return _Resp(200, {"ok": True, "result": []})

        async def post(self, url, json=None, timeout=None):
            return _Resp()

    monkeypatch.setattr(bot_poller, "get_client", lambda: _await(_CapturingClient()))
    stop = asyncio.Event()
    task = asyncio.create_task(
        poll_commands(SimpleNamespace(handle=lambda *a, **k: None), stop=stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert seen["timeout"] is not None, "no explicit timeout — inherits the 15s default"
    assert seen["timeout"] > seen["poll"], (
        f"read timeout {seen['timeout']}s must exceed long-poll {seen['poll']}s")


# ------------------------------------------------- degraded-visibility for ops
async def test_repeated_transport_failure_marks_command_control_degraded(monkeypatch):
    """A poller that cannot reach Telegram must be VISIBLE, not just noisy.

    httpx timeout exceptions stringify to '' — the old code logged bare str(exc),
    producing a blank line, and nothing was exported to /api/state.
    """
    import httpx

    bot_poller.tg_cmd_conflict = False
    bot_poller.tg_cmd_consecutive_errors = 0
    monkeypatch.setenv("TG_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(bot_poller, "DEGRADED_AFTER_ERRORS", 2)
    monkeypatch.setattr(bot_poller, "ERROR_BACKOFF_S", 0)

    class _DeadClient:
        async def get(self, url, params=None, timeout=None):
            raise httpx.ReadTimeout("")   # str() == '' — the real blank-log case

        async def post(self, url, json=None, timeout=None):
            return _Resp()

    monkeypatch.setattr(bot_poller, "get_client", lambda: _await(_DeadClient()))
    assert bot_poller.tg_cmd_degraded() is False

    stop = asyncio.Event()
    task = asyncio.create_task(
        poll_commands(SimpleNamespace(handle=lambda *a, **k: None), stop=stop))
    for _ in range(200):
        if bot_poller.tg_cmd_consecutive_errors >= 2:
            break
        await asyncio.sleep(0.005)
    stop.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bot_poller.tg_cmd_consecutive_errors >= 2
    assert bot_poller.tg_cmd_degraded() is True
    bot_poller.tg_cmd_consecutive_errors = 0


async def test_error_counter_resets_after_a_good_poll(monkeypatch):
    bot_poller.tg_cmd_conflict = False
    bot_poller.tg_cmd_consecutive_errors = 7
    monkeypatch.setenv("TG_BOT_TOKEN", "123:abc")

    class _OkClient:
        async def get(self, url, params=None, timeout=None):
            return _Resp(200, {"ok": True, "result": []})

        async def post(self, url, json=None, timeout=None):
            return _Resp()

    monkeypatch.setattr(bot_poller, "get_client", lambda: _await(_OkClient()))
    stop = asyncio.Event()
    task = asyncio.create_task(
        poll_commands(SimpleNamespace(handle=lambda *a, **k: None), stop=stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert bot_poller.tg_cmd_consecutive_errors == 0
    assert bot_poller.tg_cmd_degraded() is False
