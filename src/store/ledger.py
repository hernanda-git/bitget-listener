"""TradeLedger — fee-aware trade record (SQLite by default, Postgres-compatible SQL).

The ledger is the single place where money is accounted, so it enforces the two
inherited bug fixes at the storage boundary:

- **BUG A:** ``open_fee``/``close_fee`` are NOT NULL columns. ``net_usd`` is always
  ``gross_usd - (open_fee + close_fee)``; there is no code path that writes a net
  figure computed without fees.
- **BUG B:** a liquidation exit must carry a positive ``exit_price``. Writing 0.00
  for a liquidation raises instead of silently corrupting the P&L history.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

DDL = """
CREATE TABLE IF NOT EXISTS trades_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot           TEXT    NOT NULL DEFAULT 'bitget-listener',
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    qty           REAL    NOT NULL,
    entry_price   REAL    NOT NULL,
    exit_price    REAL,
    leverage      INTEGER NOT NULL DEFAULT 1,
    open_fee      REAL    NOT NULL DEFAULT 0,
    close_fee     REAL    NOT NULL DEFAULT 0,
    gross_usd     REAL,
    net_usd       REAL,
    sl_price      REAL,
    tp_price      REAL,
    liq_price     REAL,
    exit_reason   TEXT,
    estimated_exit INTEGER NOT NULL DEFAULT 0,
    paper         INTEGER NOT NULL DEFAULT 1,
    client_order_id TEXT UNIQUE,
    decision_id   TEXT,
    opened_at_ms  INTEGER,
    closed_at_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades_log(symbol);
CREATE TABLE IF NOT EXISTS pair_outcomes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pair    TEXT NOT NULL,
    win     INTEGER NOT NULL,
    net_usd REAL NOT NULL DEFAULT 0,
    ts_ms   INTEGER
);
"""


class LiquidationBookingError(RuntimeError):
    """A liquidation may never be booked at 0.00 (BUG B)."""


@dataclass
class TradeRow:
    id: int
    symbol: str
    side: str
    qty: float
    entry_price: float
    open_fee: float
    close_fee: float = 0.0
    exit_price: float | None = None
    net_usd: float | None = None


class TradeLedger:
    def __init__(self, path: str = ":memory:", bot: str = "bitget-listener") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.bot = bot
        self.conn.executescript(DDL)
        self.conn.commit()

    # ------------------------------------------------------------------ writing
    def record_open(self, symbol: str, side: str, qty: float, entry_price: float,
                    open_fee: float, leverage: int = 1, sl_price: float | None = None,
                    tp_price: float | None = None, liq_price: float | None = None,
                    paper: bool = True, client_order_id: str | None = None,
                    decision_id: str | None = None, **_ignored) -> int:
        """Insert an open trade. ``open_fee`` is mandatory (BUG A)."""
        if open_fee is None:
            raise ValueError("open_fee is mandatory — BUG A forbids fee-less records")
        cur = self.conn.execute(
            "INSERT INTO trades_log (bot,symbol,side,qty,entry_price,leverage,open_fee,"
            "sl_price,tp_price,liq_price,paper,client_order_id,decision_id,opened_at_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.bot, symbol.upper(), side.upper(), qty, entry_price, leverage, open_fee,
             sl_price, tp_price, liq_price, 1 if paper else 0, client_order_id, decision_id,
             int(time.time() * 1000)))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def record_close(self, trade_id: int, exit_price: float, close_fee: float,
                     exit_reason: str = "", estimated_exit: bool = False,
                     **_ignored) -> TradeRow:
        """Close a trade; ``net = gross - (open_fee + close_fee)`` (BUG A/B)."""
        row = self.get(trade_id)
        if row is None:
            raise KeyError(f"no trade {trade_id}")
        if exit_reason == "liquidation" and (exit_price is None or exit_price <= 0):
            raise LiquidationBookingError(
                f"trade {trade_id}: refusing to book a liquidation at {exit_price} (BUG B)")
        if close_fee is None:
            raise ValueError("close_fee is mandatory — BUG A forbids fee-less records")
        direction = 1 if row["side"].upper() in ("BUY", "LONG") else -1
        gross = (exit_price - row["entry_price"]) * row["qty"] * direction
        net = gross - (row["open_fee"] + close_fee)
        self.conn.execute(
            "UPDATE trades_log SET exit_price=?, close_fee=?, gross_usd=?, net_usd=?,"
            " exit_reason=?, estimated_exit=?, closed_at_ms=? WHERE id=?",
            (exit_price, close_fee, gross, net, exit_reason, 1 if estimated_exit else 0,
             int(time.time() * 1000), trade_id))
        self.conn.execute(
            "INSERT INTO pair_outcomes (pair,win,net_usd,ts_ms) VALUES (?,?,?,?)",
            (row["symbol"], 1 if net > 0 else 0, net, int(time.time() * 1000)))
        self.conn.commit()
        return TradeRow(id=trade_id, symbol=row["symbol"], side=row["side"], qty=row["qty"],
                        entry_price=row["entry_price"], open_fee=row["open_fee"],
                        close_fee=close_fee, exit_price=exit_price, net_usd=net)

    # ------------------------------------------------------------------ reading
    def get(self, trade_id: int):
        cur = self.conn.execute("SELECT * FROM trades_log WHERE id=?", (trade_id,))
        return cur.fetchone()

    def open_trades(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM trades_log WHERE closed_at_ms IS NULL")
        return [dict(r) for r in cur.fetchall()]

    def closed_trades(self, limit: int = 200) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM trades_log WHERE closed_at_ms IS NOT NULL"
            " ORDER BY closed_at_ms DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def net_pnls(self) -> list[float]:
        cur = self.conn.execute(
            "SELECT net_usd FROM trades_log WHERE net_usd IS NOT NULL")
        return [float(r[0]) for r in cur.fetchall()]

    def total_fees(self) -> float:
        cur = self.conn.execute("SELECT COALESCE(SUM(open_fee+close_fee),0) FROM trades_log")
        return float(cur.fetchone()[0])

    def outcomes_for(self, pair: str) -> list[bool]:
        cur = self.conn.execute(
            "SELECT win FROM pair_outcomes WHERE pair=? ORDER BY id", (pair.upper(),))
        return [bool(r[0]) for r in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()
