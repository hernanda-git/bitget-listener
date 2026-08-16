"""GoLiveGate — the hardened two-step authorization for leaving paper mode.

``paper`` is the live/dry flag and it is **persisted** (Postgres ``fatty_config``),
so a restart can never silently resurrect live trading.

Flipping to live requires ALL FOUR conditions from analysis/06 §8 plus a one-time
token that the operator must echo back:

  (a) ``bitget_api_probe.py`` GREEN on the real key,
  (b) fee-aware ShadowLedger expectancy > 0,
  (c) P0–P3 tests green,
  (d) an observed fill with a non-zero REAL fee (BUG A) **and** a real
      liquidation price (BUG B).

The token is single-use, time-limited, and compared with ``secrets.compare_digest``.
There is no override flag and no "force" path: a missing condition means no go-live.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TOKEN_TTL_S = 300


@dataclass
class GateConditions:
    probe_green: bool = False
    expectancy_positive: bool = False
    tests_green: bool = False
    real_fee_observed: bool = False
    real_liq_price_observed: bool = False

    def unmet(self) -> list[str]:
        missing = []
        if not self.probe_green:
            missing.append("(a) bitget_api_probe.py GREEN on the real key")
        if not self.expectancy_positive:
            missing.append("(b) fee-aware expectancy > 0")
        if not self.tests_green:
            missing.append("(c) P0-P3 tests green")
        if not self.real_fee_observed:
            missing.append("(d1) a fill with a non-zero REAL fee (BUG A)")
        if not self.real_liq_price_observed:
            missing.append("(d2) a REAL liquidation price, never 0.00 (BUG B)")
        return missing

    @property
    def all_met(self) -> bool:
        return not self.unmet()


@dataclass
class GoLiveGate:
    """Owns the paper flag. ``paper=True`` until every condition is met AND a token is redeemed."""

    conditions: GateConditions = field(default_factory=GateConditions)
    paper: bool = True
    config_store: "Any | None" = None
    _token: str | None = field(default=None, repr=False)
    _token_ts: float = 0.0

    # ------------------------------------------------------------------- minting
    def mint_token(self) -> tuple[bool, str]:
        """``--go-live`` mints a one-time token, but ONLY if every condition holds."""
        missing = self.conditions.unmet()
        if missing:
            self._token = None
            return False, ("go-live REFUSED — unmet conditions:\n  - " + "\n  - ".join(missing))
        self._token = secrets.token_hex(4)
        self._token_ts = time.time()
        return True, self._token

    def redeem(self, token: str) -> tuple[bool, str]:
        """``/live <token>`` flips paper=0. Single-use, TTL-bound, constant-time compare."""
        if self._token is None:
            return False, "no active go-live token — run --go-live first"
        if (time.time() - self._token_ts) > TOKEN_TTL_S:
            self._token = None
            return False, "go-live token expired — re-run --go-live"
        if not secrets.compare_digest(token.strip(), self._token):
            return False, "invalid go-live token"
        missing = self.conditions.unmet()
        if missing:
            self._token = None
            return False, "conditions changed since minting — go-live REFUSED"
        self._token = None
        self.paper = False
        self._persist()
        log.warning("GO-LIVE: paper flag flipped to 0 — real orders are now possible")
        return True, "LIVE mode enabled (paper=0). Real orders will now be placed."

    def to_paper(self, reason: str = "operator") -> str:
        """Return to paper. Always allowed — de-risking never needs authorization."""
        self.paper = True
        self._token = None
        self._persist()
        return f"paper mode restored ({reason})"

    def toggle(self, reason: str = "operator") -> str:
        """``/toggle``: paper->live is gated, live->paper is free."""
        if not self.paper:
            return self.to_paper(reason)
        missing = self.conditions.unmet()
        if missing:
            return ("cannot switch to live — unmet conditions:\n  - " + "\n  - ".join(missing))
        return "conditions met — run --go-live to mint a token, then /live <token>"

    def _persist(self) -> None:
        if self.config_store is not None and hasattr(self.config_store, "set_paper"):
            try:
                self.config_store.set_paper(self.paper)
            except Exception as exc:   # persistence failure must not silently go live
                log.error("failed to persist paper flag (%s) — forcing paper=True", exc)
                self.paper = True

    # ---------------------------------------------------------------- observers
    def observe_fill(self, fill) -> None:
        """Condition (d1): a real fill with a real non-zero fee."""
        if getattr(fill, "fee", 0) > 0 and not str(
                getattr(fill, "client_order_id", "")).startswith("paper-"):
            self.conditions.real_fee_observed = True

    def observe_position(self, position) -> None:
        """Condition (d2): a real, non-zero liquidation price."""
        liq = getattr(position, "liq_price", None)
        if liq is not None and liq > 0:
            self.conditions.real_liq_price_observed = True

    def observe_expectancy(self, shadow_ledger, min_samples: int = 10) -> None:
        self.conditions.expectancy_positive = shadow_ledger.expectancy_positive(
            None, min_samples=min_samples)

    def status(self) -> dict:
        return {"paper": self.paper, "mode": "PAPER" if self.paper else "LIVE",
                "conditions_met": self.conditions.all_met,
                "unmet": self.conditions.unmet(),
                "token_active": self._token is not None}
