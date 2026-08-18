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

Telegram allows exactly ONE long-poll ``getUpdates`` per bot token. If a second
consumer exists (e.g. the same token used by another bot instance / deploy),
Telegram returns HTTP 409 "terminated by other getUpdates". We detect that, log
it ONCE as a clear warning (not every cycle), back off, and expose the conflict
in ``/api/state`` via ``tg_cmd_conflict`` so the operator knows command control
is degraded and can make the token single-instance. We never crash-loop on it.
"""
from __future__ import annotations

import asyncio
import logging
import os

from src.notifier.telegram import CommandRouter, get_client

log = logging.getLogger(__name__)

POLL_TIMEOUT_S = 25
CONFLICT_BACKOFF_S = 30  # longer backoff when Telegram reports a 409
POLL_INTERVAL_S = 1      # min gap between cycles — prevents busy-spin if getUpdates returns immediately

# Module-level flag surfaced by the dashboard / /api/state (tg_cmd_conflict).
tg_cmd_conflict: bool = False


async def poll_commands(router: CommandRouter, stop: asyncio.Event | None = None) -> None:
    global tg_cmd_conflict
    token = os.getenv("TG_BOT_TOKEN", "")
    if not token:
        log.warning("[tg-cmd] TG_BOT_TOKEN not set — command poller disabled")
        return

    base = f"https://api.telegram.org/bot{token}"
    offset = 0
    log.warning("[tg-cmd] command poller started (operator-gated)")
    client = await get_client()
    conflict_reported = False
    while not (stop and stop.is_set()):
        try:
            r = await client.get(
                f"{base}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT_S},
            )
            if r.status_code == 409:
                # "Conflict: terminated by other getUpdates" — a second consumer
                # of this token exists somewhere. Back off; report once.
                if not conflict_reported:
                    log.error(
                        "[tg-cmd] Telegram 409 CONFLICT: another getUpdates consumer "
                        "is using this bot token (only one long-poll allowed per token). "
                        "Command control is DEGRADED until the token is single-instance. "
                        "Self-healing: backing off %ds.", CONFLICT_BACKOFF_S)
                    conflict_reported = True
                    tg_cmd_conflict = True
                await asyncio.sleep(CONFLICT_BACKOFF_S)
                continue
            data = r.json()
        except Exception as exc:  # network / TLS / timeout
            log.error("[tg-cmd] getUpdates failed: %s", exc)
            await asyncio.sleep(5)
            continue

        if not data.get("ok"):
            log.error("[tg-cmd] getUpdates not ok: %s", data)
            await asyncio.sleep(5)
            continue

        # Recovered: a prior 409 cleared (the other consumer let go).
        if conflict_reported:
            log.warning("[tg-cmd] getUpdates conflict cleared — command control restored.")
            conflict_reported = False
            tg_cmd_conflict = False

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

        # Pace the loop so a fast-returning getUpdates (or an empty result)
        # cannot busy-spin and starve the stop signal or hammer the API.
        await asyncio.sleep(POLL_INTERVAL_S)
        if stop and stop.is_set():
            break
