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

import contextlib
import hashlib
import json
import os
import socket
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS equity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bot         TEXT NOT NULL DEFAULT 'bitget-listener',
    ts_ms       INTEGER NOT NULL,
    equity_usd  REAL NOT NULL,
    fees_usd    REAL NOT NULL DEFAULT 0,
    open_pos    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_log(ts_ms);
CREATE TABLE IF NOT EXISTS signal_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    source_timestamp TEXT,
    received_timestamp INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    has_media INTEGER NOT NULL DEFAULT 0,
    reply_to_message_id INTEGER,
    payload_hash TEXT NOT NULL,
    source_process TEXT NOT NULL DEFAULT '',
    source_host TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'RECEIVED',
    UNIQUE(channel, message_id)
);
CREATE INDEX IF NOT EXISTS idx_signal_receipts_trace ON signal_receipts(trace_id);
CREATE TABLE IF NOT EXISTS trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON trace_events(trace_id, id);
CREATE TABLE IF NOT EXISTS pending_setups (
    setup_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at INTEGER NOT NULL,
    terminal_at INTEGER
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
    """Fee-aware trade ledger.

    Threading discipline (found by a real failure, not theory): the FastAPI
    dashboard reads the ledger from a worker thread while the trading loop writes
    from the event loop, and a stock ``sqlite3.Connection`` then raises
    ``SQLite objects created in a thread can only be used in that same thread``.

    So: file-backed ledgers open a SHORT-LIVED connection per operation, and every
    access is serialized with a lock. ``:memory:`` must keep one shared connection
    (a per-call one would see an empty database), so it is opened with
    ``check_same_thread=False`` and guarded by the same lock.
    """

    def __init__(self, path: str = ":memory:", bot: str = "bitget-listener") -> None:
        self.path = path
        self.bot = bot
        self._lock = threading.RLock()
        self._shared: sqlite3.Connection | None = None
        if path == ":memory:":
            self._shared = self._new_conn()
        else:
            self._new_conn().close()      # create the schema on disk once

    def record_received(self, trace_id: str, message_id: int, channel: str,
                        raw_text: str, has_media: bool = False,
                        source_timestamp: str | None = None,
                        reply_to_message_id: int | None = None) -> bool:
        """Durably record Telegram intake before parsing or vision."""
        digest = hashlib.sha256((raw_text or "").encode()).hexdigest()
        now = int(time.time() * 1000)
        process = f"python:{os.getpid()}"
        host = socket.gethostname()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO signal_receipts
                   (trace_id,message_id,channel,source_timestamp,received_timestamp,
                    raw_text,has_media,reply_to_message_id,payload_hash,
                    source_process,source_host,schema_version,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trace_id, message_id, channel, source_timestamp, now, raw_text or "",
                 1 if has_media else 0, reply_to_message_id, digest,
                 process, host, 1, now),
            )
            conn.execute(
                """INSERT INTO trace_events
                   (trace_id,message_id,stage,status,payload_json,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (trace_id, message_id, "RECEIVED", "DUPLICATE" if cur.rowcount == 0 else "OK",
                 json.dumps({"has_media": bool(has_media), "payload_hash": digest,
                            "raw_text": raw_text or "", "channel": channel,
                            "source_timestamp": source_timestamp,
                            "reply_to_message_id": reply_to_message_id,
                            "source_process": process, "source_host": host,
                            "schema_version": 1}), now),
            )
            conn.commit()
            return cur.rowcount == 1

    def record_stage(self, trace_id: str, message_id: int, stage: str,
                     status: str = "OK", reason: str = "", payload: dict | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO trace_events
                   (trace_id,message_id,stage,status,reason,payload_json,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (trace_id, message_id, stage, status, reason or "",
                 json.dumps(payload or {}, default=str), int(time.time() * 1000)),
            )
            conn.commit()

    def get_trace(self, trace_id: str) -> list[dict]:
        with self._conn() as conn:
            events = [dict(row) for row in conn.execute(
                "SELECT * FROM trace_events WHERE trace_id=? ORDER BY id", (trace_id,)).fetchall()]
            receipt = conn.execute(
                "SELECT * FROM signal_receipts WHERE trace_id=? ORDER BY id LIMIT 1",
                (trace_id,)).fetchone()
            if events and receipt is not None:
                events[0]["receipt"] = dict(receipt)
            return events

    def save_pending_setup(self, setup_id: str, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO pending_setups(setup_id,payload_json,status,created_at)
                   VALUES (?,?, 'PENDING', ?)
                   ON CONFLICT(setup_id) DO UPDATE SET payload_json=excluded.payload_json,
                   status='PENDING', terminal_at=NULL""",
                (setup_id, json.dumps(payload, default=str), int(time.time() * 1000)),
            )
            conn.commit()

    def pending_setups(self) -> list[dict]:
        with self._conn() as conn:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT payload_json FROM pending_setups WHERE status='PENDING' ORDER BY created_at").fetchall()]

    def finish_pending_setup(self, setup_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE pending_setups SET status=?, terminal_at=? WHERE setup_id=?",
                         (status, int(time.time() * 1000), setup_id))
            conn.commit()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.executescript(DDL)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(signal_receipts)")}
        for name, definition in {
            "source_process": "TEXT NOT NULL DEFAULT ''",
            "source_host": "TEXT NOT NULL DEFAULT ''",
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE signal_receipts ADD COLUMN {name} {definition}")
        conn.commit()
        return conn

    @contextlib.contextmanager
    def _conn(self):
        """Yield a usable connection, serialized across threads."""
        with self._lock:
            if self._shared is not None:
                yield self._shared
                return
            conn = self._new_conn()
            try:
                yield conn
            finally:
                conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        """Back-compat accessor for callers that hold a connection briefly."""
        if self._shared is not None:
            return self._shared
        return self._new_conn()

    # ------------------------------------------------------------------ writing
    def record_open(self, symbol: str, side: str, qty: float, entry_price: float,
                    open_fee: float, leverage: int = 1, sl_price: float | None = None,
                    tp_price: float | None = None, liq_price: float | None = None,
                    paper: bool = True, client_order_id: str | None = None,
                    decision_id: str | None = None, **_ignored) -> int:
        """Insert an open trade. ``open_fee`` is mandatory (BUG A)."""
        if open_fee is None:
            raise ValueError("open_fee is mandatory — BUG A forbids fee-less records")
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO trades_log (bot,symbol,side,qty,entry_price,leverage,open_fee,"
                "sl_price,tp_price,liq_price,paper,client_order_id,decision_id,opened_at_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.bot, symbol.upper(), side.upper(), qty, entry_price, leverage, open_fee,
                 sl_price, tp_price, liq_price, 1 if paper else 0, client_order_id,
                 decision_id, int(time.time() * 1000)))
            conn.commit()
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
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades_log SET exit_price=?, close_fee=?, gross_usd=?, net_usd=?,"
                " exit_reason=?, estimated_exit=?, closed_at_ms=? WHERE id=?",
                (exit_price, close_fee, gross, net, exit_reason, 1 if estimated_exit else 0,
                 int(time.time() * 1000), trade_id))
            conn.execute(
                "INSERT INTO pair_outcomes (pair,win,net_usd,ts_ms) VALUES (?,?,?,?)",
                (row["symbol"], 1 if net > 0 else 0, net, int(time.time() * 1000)))
            conn.commit()
        return TradeRow(id=trade_id, symbol=row["symbol"], side=row["side"], qty=row["qty"],
                        entry_price=row["entry_price"], open_fee=row["open_fee"],
                        close_fee=close_fee, exit_price=exit_price, net_usd=net)

    def record_partial_close(self, trade_id: int, close_qty: float, exit_price: float,
                             close_fee: float, exit_reason: str = "") -> TradeRow:
        """Realize one close leg while preserving the remaining open quantity."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM trades_log WHERE id=? AND closed_at_ms IS NULL",
                               (trade_id,)).fetchone()
            if row is None:
                raise KeyError(f"no open trade {trade_id}")
            if close_qty <= 0 or close_qty >= row["qty"]:
                raise ValueError("partial close quantity must be >0 and < open quantity")
            alloc_open_fee = row["open_fee"] * (close_qty / row["qty"])
            remaining_qty = row["qty"] - close_qty
            remaining_fee = row["open_fee"] - alloc_open_fee
            direction = 1 if row["side"].upper() in ("BUY", "LONG") else -1
            gross = (exit_price - row["entry_price"]) * close_qty * direction
            net = gross - (alloc_open_fee + close_fee)
            now = int(time.time() * 1000)
            conn.execute("UPDATE trades_log SET qty=?, open_fee=? WHERE id=?",
                         (remaining_qty, remaining_fee, trade_id))
            conn.execute(
                "INSERT INTO trades_log (bot,symbol,side,qty,entry_price,exit_price,leverage,"
                "open_fee,close_fee,gross_usd,net_usd,sl_price,tp_price,liq_price,exit_reason,"
                "estimated_exit,paper,opened_at_ms,closed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["bot"], row["symbol"], row["side"], close_qty, row["entry_price"],
                 exit_price, row["leverage"], alloc_open_fee, close_fee, gross, net,
                 row["sl_price"], row["tp_price"], row["liq_price"], exit_reason, 0,
                 row["paper"], row["opened_at_ms"], now))
            conn.execute("INSERT INTO pair_outcomes (pair,win,net_usd,ts_ms) VALUES (?,?,?,?)",
                         (row["symbol"], 1 if net > 0 else 0, net, now))
            conn.commit()
            return TradeRow(id=trade_id, symbol=row["symbol"], side=row["side"], qty=close_qty,
                            entry_price=row["entry_price"], open_fee=alloc_open_fee,
                            close_fee=close_fee, exit_price=exit_price, net_usd=net)

    def record_outcome(self, pair: str, win: bool, net_usd: float = 0.0) -> None:
        """Append a pair outcome directly (used by the eval store)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO pair_outcomes (pair,win,net_usd,ts_ms) VALUES (?,?,?,?)",
                (pair.upper(), 1 if win else 0, net_usd, int(time.time() * 1000)))
            conn.commit()

    # ------------------------------------------------------------------ reading
    def get(self, trade_id: int):
        with self._conn() as conn:
            return conn.execute("SELECT * FROM trades_log WHERE id=?",
                                (trade_id,)).fetchone()

    def open_trades(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM trades_log WHERE closed_at_ms IS NULL").fetchall()]

    def closed_trades(self, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM trades_log WHERE closed_at_ms IS NOT NULL"
                " ORDER BY closed_at_ms DESC LIMIT ?", (limit,)).fetchall()]

    def net_pnls(self) -> list[float]:
        with self._conn() as conn:
            return [float(r[0]) for r in conn.execute(
                "SELECT net_usd FROM trades_log WHERE net_usd IS NOT NULL").fetchall()]

    def total_fees(self) -> float:
        with self._conn() as conn:
            return float(conn.execute(
                "SELECT COALESCE(SUM(open_fee+close_fee),0) FROM trades_log").fetchone()[0])

    def outcomes_for(self, pair: str) -> list[bool]:
        with self._conn() as conn:
            return [bool(r[0]) for r in conn.execute(
                "SELECT win FROM pair_outcomes WHERE pair=? ORDER BY id",
                (pair.upper(),)).fetchall()]

    def nets_for(self, pair: str) -> list[float]:
        with self._conn() as conn:
            return [float(r[0]) for r in conn.execute(
                "SELECT net_usd FROM pair_outcomes WHERE pair=?",
                (pair.upper(),)).fetchall()]

    # ------------------------------------------------------------------ equity history
    def record_equity(self, ts_ms: int, equity_usd: float,
                      fees_usd: float = 0.0, open_pos: int = 0) -> None:
        """Append one wallet-equity sample (the historical series for the chart)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO equity_log (bot, ts_ms, equity_usd, fees_usd, open_pos) "
                "VALUES (?,?,?,?,?)",
                (self.bot, int(ts_ms), float(equity_usd), float(fees_usd), int(open_pos)))
            conn.commit()

    def equity_history(self, limit: int = 500) -> list[dict]:
        """Newest-last wallet-equity samples, for charting / table."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts_ms, equity_usd, fees_usd, open_pos FROM equity_log "
                "ORDER BY ts_ms ASC LIMIT ?", (limit,)).fetchall()
            return [{"ts_ms": int(r[0]), "equity_usd": float(r[1]),
                     "fees_usd": float(r[2]), "open_pos": int(r[3])} for r in rows]

    def close(self) -> None:
        with self._lock:
            if self._shared is not None:
                self._shared.close()
                self._shared = None
