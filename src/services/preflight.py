"""Venue-state preflight — refuse to trade into a misconfigured account.

Two real incidents motivate this module, both from the first live PUMP entry:

1. ``40774`` — the account sat in ``hedge_mode`` while the adapter builds
   one-way order bodies (plain ``side``, ``reduceOnly`` on close, no
   ``tradeSide``). Every entry was rejected outright by the venue.

2. The fill landed ``isolated`` at 10x, putting the liquidation price at
   0.00270709 while the trader's stop was 0.002882 — liquidation sat INSIDE
   the stop, so the position would have been force-closed before the stop
   could ever protect it. Switching to ``crossed`` moved liquidation to
   -0.00144 (unreachable).

Neither failure was visible until money was on the line. This checks both at
startup and reports precisely what is wrong, so a broken account state can
never silently eat signals or produce a structurally unsound position.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

P_ACCOUNT = "/api/v2/mix/account/account"
P_SET_POS_MODE = "/api/v2/mix/account/set-position-mode"
P_SET_MARGIN_MODE = "/api/v2/mix/account/set-margin-mode"


@dataclass
class PreflightResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    pos_mode: str | None = None
    margin_mode: str | None = None

    @property
    def summary(self) -> str:
        if self.ok and not self.fixed:
            return f"venue state ok (posMode={self.pos_mode}, margin={self.margin_mode})"
        if self.ok:
            return "venue state repaired: " + "; ".join(self.fixed)
        return "venue state BAD: " + "; ".join(self.problems)


async def preflight_venue(adapter, probe_symbol: str, *, repair: bool = True,
                          want_margin_mode: str = "crossed") -> PreflightResult:
    """Verify (and optionally repair) position mode and margin mode.

    ``repair`` only ever runs when NO positions are open — Bitget rejects a
    mode change on an open position, and forcing one mid-trade would alter the
    liquidation price of live money. Read-only when positions exist.
    """
    res = PreflightResult(ok=True)
    try:
        acct = await adapter._signed(
            "GET", P_ACCOUNT,
            params={"symbol": adapter.venue_symbol(probe_symbol),
                    "productType": adapter.product_type,
                    "marginCoin": adapter.margin_coin})
    except Exception as exc:
        res.ok = False
        res.problems.append(f"cannot read account state: {type(exc).__name__}: {exc}")
        return res

    res.pos_mode = str(acct.get("posMode") or "")
    res.margin_mode = str(acct.get("marginMode") or "")

    try:
        open_positions = await adapter.get_positions()
    except Exception:
        open_positions = []
    can_change = not open_positions

    # ---- position mode: the adapter speaks one-way only -------------------
    if res.pos_mode and res.pos_mode != "one_way_mode":
        msg = (f"posMode={res.pos_mode} but this adapter builds one-way orders "
               f"(venue rejects with 40774)")
        if repair and can_change:
            try:
                await adapter._signed("POST", P_SET_POS_MODE,
                                      body={"productType": adapter.product_type,
                                            "posMode": "one_way_mode"})
                res.fixed.append(f"posMode {res.pos_mode} -> one_way_mode")
                res.pos_mode = "one_way_mode"
            except Exception as exc:
                res.ok = False
                res.problems.append(f"{msg}; repair failed: {exc}")
        else:
            res.ok = False
            res.problems.append(
                msg + ("; cannot repair with positions open" if not can_change
                       else ""))

    # ---- margin mode: isolated can put liquidation inside the stop --------
    if want_margin_mode and res.margin_mode and res.margin_mode != want_margin_mode:
        msg = (f"marginMode={res.margin_mode}, wanted {want_margin_mode} "
               f"(isolated can place liquidation inside the stop-loss)")
        if repair and can_change:
            try:
                await adapter._signed(
                    "POST", P_SET_MARGIN_MODE,
                    body={"symbol": adapter.venue_symbol(probe_symbol),
                          "productType": adapter.product_type,
                          "marginCoin": adapter.margin_coin,
                          "marginMode": want_margin_mode})
                res.fixed.append(f"marginMode {res.margin_mode} -> {want_margin_mode}")
                res.margin_mode = want_margin_mode
            except Exception as exc:
                # Not fatal: cross is a safety preference, not a hard requirement.
                res.problems.append(f"{msg}; repair failed: {exc}")
        else:
            res.problems.append(msg)

    if res.fixed:
        log.warning("[preflight] %s", res.summary)
    elif res.problems:
        log.error("[preflight] %s", res.summary)
    else:
        log.info("[preflight] %s", res.summary)
    return res
