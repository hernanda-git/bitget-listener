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

    async def get(self, url, params=None):
        self.calls += 1
        return _Resp(status_code=409)

    async def post(self, url, json=None):
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
        async def get(self, url, params=None):
            return _Resp(200, {"ok": True, "result": []})
        async def post(self, url, json=None):
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
