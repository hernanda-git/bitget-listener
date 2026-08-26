"""ConfigStore — DB-backed multi-bot config with a PERSISTED paper flag.

Grafted from hyperliquid-fatty-trader (the strongest runtime of the four): the bot's
row lives in the existing ``fatty_config`` Postgres DB, so ``/toggle`` survives a
restart and a redeploy cannot silently resurrect live trading.

Two rules kept from the sibling:
- **Secrets are env-var NAMES, never values.** The DB stores ``BITGET_API_KEY``, and
  the process resolves it from the environment at use time.
- **Fail closed.** Any DB/parse error resolves ``paper`` to ``True``.

``InMemoryConfigStore`` is the fakes-only implementation used by the unit tests, so
the suite needs no database.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS fatty_config (
    bot_name   TEXT PRIMARY KEY,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    paper      BOOLEAN NOT NULL DEFAULT TRUE,
    venue      TEXT NOT NULL DEFAULT 'bitget',
    params     TEXT NOT NULL DEFAULT '{}',
    secret_env TEXT NOT NULL DEFAULT '{}',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

DEFAULT_PARAMS: dict = {
    "risk_per_trade_usd": 5.0,
    "max_position_usd": 100.0,
    "max_concurrent": 2,
    "max_daily_loss_pct": 30.0,
    "max_leverage": 20,
    "min_notional_usd": 5.0,
    "cooldown_s": 30,
    "whitelist_only": True,
    "product_type": "SUSDT-FUTURES",
    "risk_profile": "normal",
    "high_risk_min_margin_usd": 3.0,
    "high_risk_min_leverage": 30,
    "high_risk_minimum_margin_exception_usd": 3.0,
}


@dataclass
class BotConfig:
    bot_name: str = "bitget-listener"
    enabled: bool = True
    paper: bool = True                 # fail-closed default
    venue: str = "bitget"
    params: dict = field(default_factory=lambda: dict(DEFAULT_PARAMS))
    secret_env: dict = field(default_factory=dict)

    def resolve_secret(self, logical: str) -> str:
        """Resolve a logical secret name through the env (values never live in the DB)."""
        return os.getenv(self.secret_env.get(logical, logical), "")


class InMemoryConfigStore:
    """Fakes-only config store (unit tests; zero network, zero DB)."""

    def __init__(self, config: BotConfig | None = None) -> None:
        self.config = config or BotConfig()
        self.writes: list[bool] = []

    def load(self) -> BotConfig:
        return self.config

    def set_paper(self, paper: bool) -> None:
        self.config.paper = bool(paper)
        self.writes.append(bool(paper))

    def get_paper(self) -> bool:
        return self.config.paper

    def set_params(self, params: dict) -> None:
        self.config.params.update(params)


class PostgresConfigStore:
    """psycopg-backed store. Single driver (psycopg), matching the sibling contract."""

    def __init__(self, bot_name: str = "bitget-listener", dsn: str | None = None) -> None:
        self.bot_name = bot_name
        self.dsn = dsn or self._dsn_from_env()

    @staticmethod
    def _dsn_from_env() -> str:
        return (f"host={os.getenv('PGHOST','localhost')} port={os.getenv('PGPORT','5432')} "
                f"dbname={os.getenv('PGDATABASE','fatty_config')} "
                f"user={os.getenv('PGUSER','postgres')} "
                f"password={os.getenv('PGPASSWORD','')}")

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn, connect_timeout=10)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(DDL)
            conn.commit()

    def load(self) -> BotConfig:
        """Load the row. Any failure returns a fail-closed paper=True config."""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT bot_name, enabled, paper, venue, params, secret_env"
                    " FROM fatty_config WHERE bot_name=%s", (self.bot_name,))
                row = cur.fetchone()
            if row is None:
                return BotConfig(bot_name=self.bot_name)
            params = row[4] if isinstance(row[4], dict) else json.loads(row[4] or "{}")
            secret_env = row[5] if isinstance(row[5], dict) else json.loads(row[5] or "{}")
            merged = dict(DEFAULT_PARAMS)
            merged.update(params)
            return BotConfig(bot_name=row[0], enabled=bool(row[1]), paper=bool(row[2]),
                             venue=row[3] or "bitget", params=merged, secret_env=secret_env)
        except Exception as exc:
            log.error("config load failed (%s) — failing CLOSED to paper=True", exc)
            return BotConfig(bot_name=self.bot_name, paper=True)

    def upsert(self, config: BotConfig) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO fatty_config (bot_name,enabled,paper,venue,params,secret_env)"
                " VALUES (%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (bot_name) DO UPDATE SET enabled=EXCLUDED.enabled,"
                " paper=EXCLUDED.paper, venue=EXCLUDED.venue, params=EXCLUDED.params,"
                " secret_env=EXCLUDED.secret_env, updated_at=CURRENT_TIMESTAMP",
                (config.bot_name, config.enabled, config.paper, config.venue,
                 json.dumps(config.params), json.dumps(config.secret_env)))
            conn.commit()

    def set_paper(self, paper: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE fatty_config SET paper=%s, updated_at=CURRENT_TIMESTAMP"
                         " WHERE bot_name=%s", (bool(paper), self.bot_name))
            conn.commit()

    def get_paper(self) -> bool:
        return self.load().paper

    def set_params(self, params: dict) -> None:
        cfg = self.load()
        cfg.params.update(params)
        self.upsert(cfg)
