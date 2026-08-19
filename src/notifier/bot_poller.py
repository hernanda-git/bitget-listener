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
# The HTTP read timeout MUST exceed the server-side long-poll or EVERY poll is
# aborted client-side before Telegram can answer. The shared notifier client
# defaults to 15s, which is SHORTER than POLL_TIMEOUT_S=25 — that mismatch made
# the poller fail on a 20s cycle forever (15s timeout + 5s error sleep) and, worse,
# aborted the request before the 409-conflict status check could ever run, so
# `tg_cmd_conflict` stayed false while command control was actually dead.
REQUEST_TIMEOUT_S = POLL_TIMEOUT_S + 10
ERROR_BACKOFF_S = 5     # backoff after a transport failure (tunable for tests)

# Module-level flag surfaced by the dashboard / /api/state (tg_cmd_conflict).
tg_cmd_conflict: bool = False
# Consecutive transport failures. Surfaced as `tg_cmd_degraded` so a poller that
# cannot reach Telegram AT ALL is visible to the operator instead of only being
# an ERROR line in the journal. On a LIVE account a silently deaf command
# channel means /halt and /close do not reach the bot.
tg_cmd_consecutive_errors: int = 0
DEGRADED_AFTER_ERRORS = 3


def tg_cmd_degraded() -> bool:
    """True when command control is not known-good (409 conflict or transport down)."""
    return bool(tg_cmd_conflict) or tg_cmd_consecutive_errors >= DEGRADED_AFTER_ERRORS


async def register_bot_commands(router) -> bool:
    """Publish the command list via setMyCommands so Telegram's "/" popup shows it.

    This is what makes typing a bare "/" in the chat render a tappable menu.
    Best-effort: a failure here must never stop the bot from trading, so it is
    logged and swallowed. Descriptions are capped at Telegram's 256-char limit
    and names must be lowercase a-z/0-9/_ (the API rejects anything else).
    """
    token = os.getenv("TG_BOT_TOKEN", "")
    if not token:
        return False
    cmds = [{"command": name, "description": (f"{desc} — {args}" if args
                                              else desc)[:256]}
            for name, args, desc, *_rest in router.COMMANDS]
    try:
        client = await get_client()
        r = await client.post(
            f"https://api.telegram.org/bot{token}/setMyCommands",
            json={"commands": cmds}, timeout=REQUEST_TIMEOUT_S)
        data = r.json()
        if data.get("ok"):
            log.warning("[tg-cmd] registered %d commands with Telegram "
                        "(the \"/\" menu is live)", len(cmds))
            return True
        log.error("[tg-cmd] setMyCommands rejected: %s", data)
    except Exception as exc:
        log.error("[tg-cmd] setMyCommands failed: %s: %s", type(exc).__name__, exc)
    return False


async def poll_commands(router: CommandRouter, stop: asyncio.Event | None = None) -> None:
    global tg_cmd_conflict, tg_cmd_consecutive_errors
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
                timeout=REQUEST_TIMEOUT_S,
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
            # NEVER log bare str(exc): httpx timeout exceptions stringify to '',
            # which produced a blank, undiagnosable error line for every cycle.
            tg_cmd_consecutive_errors += 1
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            if tg_cmd_consecutive_errors == 1:
                log.error("[tg-cmd] getUpdates failed: %s", detail)
            elif tg_cmd_consecutive_errors == DEGRADED_AFTER_ERRORS:
                log.error("[tg-cmd] getUpdates failed %d consecutive times (%s) — "
                          "command control DEGRADED (/halt, /close may not reach the "
                          "bot). Surfaced as tg_cmd_degraded in /api/state.",
                          tg_cmd_consecutive_errors, detail)
            elif tg_cmd_consecutive_errors % 30 == 0:
                # Heartbeat only: do not flood the journal every 20s forever.
                log.error("[tg-cmd] getUpdates still failing (%d consecutive): %s",
                          tg_cmd_consecutive_errors, detail)
            await asyncio.sleep(ERROR_BACKOFF_S)
            continue

        if not data.get("ok"):
            log.error("[tg-cmd] getUpdates not ok: %s", data)
            await asyncio.sleep(5)
            continue

        # A successful, well-formed poll: the transport is healthy again.
        if tg_cmd_consecutive_errors:
            if tg_cmd_consecutive_errors >= DEGRADED_AFTER_ERRORS:
                log.warning("[tg-cmd] getUpdates recovered after %d consecutive "
                            "failures — command control restored.",
                            tg_cmd_consecutive_errors)
            tg_cmd_consecutive_errors = 0

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
