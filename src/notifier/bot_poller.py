"""Telegram Bot-API command poller for operator control.

The ``@fattyfatclub`` signal *listener* uses Telethon (user API). This poller is
separate: it uses the Bot API (``TG_BOT_TOKEN``) to receive operator commands
(``/status``, ``/toggle``, ``/paper``, ``/live``, ``/close``, ``/sl_to_entry``)
sent as direct messages to the bot, and routes them through ``CommandRouter``.

Sending (``TelegramNotifier``) and receiving here share one token but different
API surfaces, so there is no conflict with the Telethon listener. State-changing
commands are authorized by ``CommandRouter`` against ``TG_OPERATOR_ID``.

Non-destructive: it only reads ``getUpdates`` and replies; it never places orders
itself. Any order effect goes through the ``Orchestrator``/``GoLiveGate`` path,
which remains gated.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from src.notifier.telegram import CommandRouter

log = logging.getLogger(__name__)

POLL_TIMEOUT_S = 25
HTTP_TIMEOUT_S = POLL_TIMEOUT_S + 15


async def poll_commands(router: CommandRouter, stop: asyncio.Event | None = None) -> None:
    token = os.getenv("TG_BOT_TOKEN", "")
    if not token:
        log.warning("[tg-cmd] TG_BOT_TOKEN not set — command poller disabled")
        return

    base = f"https://api.telegram.org/bot{token}"
    offset = 0
    log.warning("[tg-cmd] command poller started (operator-gated)")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        while not (stop and stop.is_set()):
            try:
                r = await client.get(
                    f"{base}/getUpdates",
                    params={"offset": offset, "timeout": POLL_TIMEOUT_S},
                )
                data = r.json()
            except Exception as exc:  # network / TLS / timeout
                log.error("[tg-cmd] getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue

            if not data.get("ok"):
                log.error("[tg-cmd] getUpdates not ok: %s", data)
                await asyncio.sleep(5)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                text = msg.get("text", "")
                sender = msg.get("from", {}).get("id")
                if not text or not text.startswith("/"):
                    continue
                try:
                    reply = await router.handle(text, sender)
                except Exception:  # never let a bad command kill the poller
                    log.exception("[tg-cmd] handler error for %r", text)
                    reply = "internal error handling command"
                if reply:
                    chat_id = msg.get("chat", {}).get("id")
                    try:
                        await client.post(
                            f"{base}/sendMessage",
                            json={"chat_id": chat_id, "text": reply, "parse_mode": "HTML"},
                        )
                    except Exception as exc:
                        log.error("[tg-cmd] reply send failed: %s", exc)

            if stop and stop.is_set():
                break
