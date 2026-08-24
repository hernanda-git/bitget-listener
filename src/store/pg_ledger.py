"""PostgresLedger — fee-aware trade ledger backed by the shared ``bot_common`` DB.

This is a drop-in replacement for the SQLite ``TradeLedger``. Every method keeps
the same signature so the orchestrator, dashboard and shadow ledger are untouched.
All rows are partitioned by ``bot`` (``bitget-listener``) in the shared
``fatty_config`` database, so the data is general, queryable and survives restarts.

BUG A / BUG B invariants from the original ledger are preserved:
* ``open_fee`` / ``close_fee`` are mandatory on write.
* a liquidation may never be booked at 0.00.
"""

from __future__ import annotations

import os
from typing import Any

from bot_common import repos

BOT = "bitget-listener"


class LiquidationBookingError(RuntimeError):
    """A liquidation may never be booked at 0.00 (BUG B)."""


class TradeRow:
    def __init__(self, id, symbol, side, qty, entry_price, open_fee,
                 close_fee=0.0, exit_price=None, net_usd=None):
        self.id = id
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.entry_price = entry_price
        self.open_fee = open_fee
        self.close_fee = close_fee
        self.exit_price = exit_price
        self.net_usd = net_usd


class PostgresLedger:
    """Shared-DB ledger. Mirrors the SQLite TradeLedger's public surface."""

    def __init__(self, bot: str = BOT) -> None:
        self.bot = bot

    # --------------------------------------------------------------- trace spine
    def record_received(self, trace_id: str, message_id: int, channel: str,
                        raw_text: str, has_media: bool = False,
                        source_timestamp: str | None = None,
                        reply_to_message_id: int | None = None) -> bool:
        return repos.Traces.record_received(
            self.bot, trace_id, message_id, channel, raw_text=raw_text,
            has_media=has_media, source_timestamp=source_timestamp,
            reply_context={"reply_to_message_id": reply_to_message_id}
            if reply_to_message_id is not None else {},
            service_identity=os.getenv("BOT_NAME", self.bot),
            source_process="bitget-listener")

    def record_stage(self, trace_id: str, message_id: int, stage: str,
                     status: str = "OK", reason: str = "",
                     payload: dict | None = None) -> None:
        repos.Traces.record_stage(self.bot, trace_id, message_id, stage, status,
                                  reason, payload=payload,
                                  service_identity=os.getenv("BOT_NAME", self.bot))

    def get_trace(self, trace_id: str) -> list[dict]:
        return repos.Traces.get_trace(self.bot, trace_id)

    # ------------------------------------------------------------------ writing
    def record_open(self, symbol: str, side: str, qty: float, entry_price: float,
                    open_fee: float, leverage: int = 1, sl_price: float | None = None,
                    tp_price: float | None = None, liq_price: float | None = None,
                    paper: bool = True, client_order_id: str | None = None,
                    decision_id: str | None = None, source_msg_id: int | None = None,
                    **_ignored) -> int:
        if open_fee is None:
            raise ValueError("open_fee is mandatory — BUG A forbids fee-less records")
        return repos.Trades.record_open(
            self.bot, symbol, side, qty, entry_price, open_fee, leverage=leverage,
            sl_price=sl_price, tp_price=tp_price, liq_price=liq_price, paper=paper,
            client_order_id=client_order_id, source_msg_id=source_msg_id)

    def record_close(self, trade_id: int, exit_price: float, close_fee: float,
                     exit_reason: str = "", estimated_exit: bool = False,
                     **_ignored) -> TradeRow:
        row = repos.Trades.open_trades(self.bot)
        # open_trades returns only open rows; find the matching id for direction
        found = next((r for r in row if r["id"] == trade_id), None)
        if found is None:
            # fall back to a direct lookup via closed+open; we stored qty/side at open
            # but record_close needs the row; query by id through a small helper.
            found = self.get(trade_id)
        if found is None:
            raise KeyError(f"no trade {trade_id}")
        if exit_reason == "liquidation" and (exit_price is None or exit_price <= 0):
            raise LiquidationBookingError(
                f"trade {trade_id}: refusing to book a liquidation at {exit_price} (BUG B)")
        if close_fee is None:
            raise ValueError("close_fee is mandatory — BUG A forbids fee-less records")
        res = repos.Trades.record_close(
            trade_id, exit_price, close_fee, bot=self.bot,
            exit_reason=exit_reason, estimated_exit=estimated_exit)
        if res is None:
            raise KeyError(f"no trade {trade_id}")
        return TradeRow(id=trade_id, symbol=res["symbol"], side=found["side"],
                        qty=found["qty"], entry_price=found["entry_price"],
                        open_fee=found["open_fee"], close_fee=close_fee,
                        exit_price=exit_price, net_usd=res.get("net_usd"))

    def record_partial_close(self, trade_id: int, close_qty: float, exit_price: float,
                             close_fee: float, exit_reason: str = "") -> TradeRow:
        row = self.get(trade_id)
        if row is None or row.get("closed_at_ms") is not None:
            raise KeyError(f"no open trade {trade_id}")
        res = repos.Trades.record_partial_close(
            trade_id, close_qty, exit_price, close_fee, bot=self.bot,
            exit_reason=exit_reason)
        alloc_open_fee = float(row["open_fee"]) * (close_qty / float(row["qty"]))
        return TradeRow(id=trade_id, symbol=row["symbol"], side=row["side"], qty=close_qty,
                        entry_price=row["entry_price"], open_fee=alloc_open_fee,
                        close_fee=close_fee, exit_price=exit_price,
                        net_usd=res.get("net_usd"))

    def record_outcome(self, pair: str, win: bool, net_usd: float = 0.0) -> None:
        # The shadow ledger calls this; we fold it into the trade's pair_outcomes
        # equivalent by appending a synthetic closed-trade-less outcome row is not
        # needed — the eval store reads nets_for/outcomes_for from trades. No-op
        # against the shared DB (outcomes are derived from bc_trades.net_usd).
        return None

    # ------------------------------------------------------------------ reading
    def get(self, trade_id: int):
        from bot_common.db import get_conn
        with get_conn() as c:
            return c.execute("SELECT * FROM bc_trades WHERE id=%s", (trade_id,)).fetchone()

    def open_trades(self) -> list[dict]:
        return repos.Trades.open_trades(self.bot)

    def closed_trades(self, limit: int = 200) -> list[dict]:
        return repos.Trades.closed_trades(self.bot, limit=limit)

    def net_pnls(self) -> list[float]:
        return repos.Trades.net_pnls(self.bot)

    def total_fees(self) -> float:
        return repos.Trades.total_fees(self.bot)

    def outcomes_for(self, pair: str) -> list[bool]:
        from bot_common.db import get_conn
        with get_conn() as c:
            rows = c.execute(
                "SELECT net_usd FROM bc_trades WHERE bot=%s AND symbol=%s "
                "AND closed_at_ms IS NOT NULL", (self.bot, pair.upper())).fetchall()
        return [r["net_usd"] > 0 for r in rows]

    def nets_for(self, pair: str) -> list[float]:
        from bot_common.db import get_conn
        with get_conn() as c:
            rows = c.execute(
                "SELECT net_usd FROM bc_trades WHERE bot=%s AND symbol=%s "
                "AND closed_at_ms IS NOT NULL", (self.bot, pair.upper())).fetchall()
        return [float(r["net_usd"]) for r in rows]

    # ------------------------------------------------------------------ equity
    def record_equity(self, ts_ms: int, equity_usd: float,
                      fees_usd: float = 0.0, open_pos: int = 0) -> None:
        repos.Equity.record(self.bot, equity_usd, fees_usd=fees_usd, open_pos=open_pos)

    def equity_history(self, limit: int = 500) -> list[dict]:
        return repos.Equity.history(self.bot, limit=limit)

    def close(self) -> None:
        return None
