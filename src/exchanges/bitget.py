"""BitgetAdapter — Bitget V2 mix (USDⓈ-M futures) REST adapter.

Structure follows bybit-listener's ``BybitAdapter``; execution rigor (validate →
deterministic repair → single resubmit, magnitude-flip guards) follows mexc-listener.

**Every endpoint, parameter and field name here was VERIFIED by
``scripts/bitget_api_probe.py``** — see `analysis/07-BITGET-IMPLEMENTATION-NOTES.md` §0.
Notably the analysis doc's `umcbl`, `sizeIncrement`, `/market/tickers` and
pre-hashed passphrase were all WRONG and are not used.

Safety contracts implemented here:
- BUG A: ``get_fills`` always carries the REAL ``fee`` (Bitget reports it negative;
  normalized to a positive cost). A fill is never recorded with fee 0 silently.
- BUG B: ``get_positions`` reads the REAL ``liquidationPrice``; it is never coerced to 0.0.
- BUG C: ``cancel_all`` NEVER cancels ``reduceOnly`` protective orders.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import os
import time
import uuid

from typing import Any

import httpx

from src.domain.model import (
    Fill,
    Instrument,
    InstrumentSpec,
    OrderRequest,
    Position,
    Side,
)
from src.exchanges.bitget_auth import BitgetAuth
from src.exchanges.rate_limit import RateLimitGate
from src.interfaces.ports import ExchangeAdapter

log = logging.getLogger(__name__)

# VERIFIED V2 paths (see analysis/07 §0)
P_CONTRACTS = "/api/v2/mix/market/contracts"
P_TICKER = "/api/v2/mix/market/ticker"          # SINGULAR
P_PLACE = "/api/v2/mix/order/place-order"
P_TPSL = "/api/v2/mix/order/place-tpsl-order"
P_CANCEL = "/api/v2/mix/order/cancel-order"
P_OPEN_ORDERS = "/api/v2/mix/order/orders-pending"
P_PLAN_ORDERS = "/api/v2/mix/order/orders-plan-pending"
P_FILLS = "/api/v2/mix/order/fills"
P_POSITIONS = "/api/v2/mix/position/all-position"
P_ACCOUNTS = "/api/v2/mix/account/accounts"     # PLURAL
P_SET_LEVERAGE = "/api/v2/mix/account/set-leverage"

# Bitget business codes that are worth a deterministic repair rather than a hard fail
REPAIRABLE_CODES = {
    "40762",  # order size exceeds/below limit
    "40774",  # size precision
    "45110",  # less than the minimum amount
    "43012",  # insufficient (size) — re-round then resubmit once
    "40808",  # param verification exception
}


class BitgetError(RuntimeError):
    """A Bitget business error (non-`00000` code)."""

    def __init__(self, code: str, msg: str, path: str = "") -> None:
        super().__init__(f"bitget {code} on {path}: {msg}")
        self.code = code
        self.msg = msg
        self.path = path

    @property
    def repairable(self) -> bool:
        return self.code in REPAIRABLE_CODES


class BitgetAdapter(ExchangeAdapter):
    """Bitget V2 mix adapter. All order paths funnel through ``_signed``."""

    def __init__(self, key: str = "", secret: str = "", passphrase: str = "",
                 product_type: str | None = None, base_url: str | None = None,
                 margin_coin: str = "USDT", client: httpx.AsyncClient | None = None,
                 paper: bool = True, recv_window_ms: int = 5000,
                 rate_limit: RateLimitGate | None = None) -> None:
        self.base_url = (base_url or os.getenv("BITGET_REST_BASE", "https://api.bitget.com")).rstrip("/")
        self.product_type = product_type or os.getenv("BITGET_PRODUCT_TYPE", "SUSDT-FUTURES")
        self.margin_coin = margin_coin if self.product_type == "USDT-FUTURES" else "SUSDT"
        self.paper = paper
        self.recv_window_ms = recv_window_ms
        self._auth: BitgetAuth | None = None
        if key and secret and passphrase:
            self._auth = BitgetAuth(key, secret, passphrase)
        self._client = client
        self._owns_client = client is None
        self._spec_cache: dict[str, InstrumentSpec] = {}
        # Rate-limit gate: throttles every outbound call + handles 429/ban risk.
        self.rate_limit = rate_limit or RateLimitGate()

    # ------------------------------------------------------------------ plumbing
    @property
    def is_demo(self) -> bool:
        """True when pointed at Bitget's demo product (`SUSDT-FUTURES`)."""
        return self.product_type.startswith("S")

    def venue_symbol(self, symbol: str) -> str:
        """Translate a canonical symbol into the venue's symbol for this product type.

        VERIFIED against the live API: the demo product does NOT list ``BTCUSDT``.
        ``productType=SUSDT-FUTURES`` exposes exactly three contracts — ``SBTCSUSDT``,
        ``SETHSUSDT``, ``SXRPSUSDT`` — i.e. an ``S`` prefix and a ``SUSDT`` quote.
        Without this translation every demo order fails with NO_SPEC, which is
        precisely how the first end-to-end paper run broke.
        """
        sym = symbol.upper()
        if not self.is_demo:
            return sym
        if sym.startswith("S") and sym.endswith("SUSDT"):
            return sym                      # already a demo symbol
        base = sym[:-4] if sym.endswith("USDT") else sym
        return f"S{base}SUSDT"

    def canonical_symbol(self, venue_symbol: str) -> str:
        """Inverse of :meth:`venue_symbol`, so responses map back to our domain."""
        sym = venue_symbol.upper()
        if not self.is_demo:
            return sym
        if sym.startswith("S") and sym.endswith("SUSDT"):
            return f"{sym[1:-5]}USDT"
        return sym

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _qs(params: dict) -> str:
        clean = {k: v for k, v in params.items() if v is not None}
        if not clean:
            return ""
        return "?" + "&".join(f"{k}={v}" for k, v in clean.items())

    async def _public(self, path: str, params: dict | None = None) -> Any:
        await self.rate_limit.acquire("public")
        client = await self._http()
        r = await client.get(f"{self.base_url}{path}", params=params or {})
        self.rate_limit.on_success()
        body = r.json()
        if body.get("code") != "00000":
            raise BitgetError(str(body.get("code")), str(body.get("msg")), path)
        return body.get("data")

    async def _signed(self, method: str, path: str, params: dict | None = None,
                      body: dict | None = None) -> Any:
        """Signed request. ``request_path`` includes the query string (VERIFIED)."""
        if self._auth is None:
            raise BitgetError("NO_CREDS",
                              "no Bitget API credentials configured (set BITGET_API_KEY/"
                              "SECRET/PASSPHRASE in .env)", path)
        # Throttle by category: order endpoints are the strict 1/s path, account
        # endpoints 5/s, everything else the 10/s signed default.
        category = "order" if path == P_PLACE else (
            "account" if path in (P_SET_LEVERAGE, P_ACCOUNTS) else "signed")
        await self.rate_limit.acquire(category)
        request_path = path + self._qs(params or {})
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        headers = self._auth.headers(method, request_path, payload)
        client = await self._http()
        r = await client.request(method.upper(), f"{self.base_url}{request_path}",
                                 content=payload or None, headers=headers)
        if r.status_code == 429:
            retry_after = None
            try:
                retry_after = float(r.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            await self.rate_limit.on_429(retry_after)
            raise BitgetError("429", "too frequent (rate limited)", path)
        try:
            data = r.json()
        except Exception:
            raise BitgetError(str(r.status_code), r.text[:200], path)
        if data.get("code") != "00000":
            raise BitgetError(str(data.get("code")), str(data.get("msg")), path)
        self.rate_limit.on_success()
        return data.get("data")

    # ----------------------------------------------------------- instrument spec
    @staticmethod
    def _step_from_places(places: Any) -> float:
        """Bitget publishes DECIMAL PLACES, not a step. `volumePlace=4` -> 0.0001."""
        return 10 ** -int(places)

    async def get_spec(self, instrument: Instrument) -> InstrumentSpec:
        if instrument.symbol in self._spec_cache:
            return self._spec_cache[instrument.symbol]
        rows = await self._public(P_CONTRACTS, {"productType": self.product_type,
                                                "symbol": self.venue_symbol(instrument.symbol)})
        if not rows:
            raise BitgetError("NO_SPEC", f"no contract for {instrument.symbol}", P_CONTRACTS)
        # get_spec_for_symbol does the parsing so a non-Instrument caller (the
        # tradability check) can resolve a raw ticker string without fabricating
        # an Instrument.
        spec = await self.get_spec_for_symbol(instrument.symbol)
        self._spec_cache[instrument.symbol] = spec
        return spec

    async def get_spec_for_symbol(self, symbol: str) -> InstrumentSpec:
        """Resolve a raw symbol string (e.g. from a live signal) to a spec."""
        rows = await self._public(P_CONTRACTS, {"productType": self.product_type,
                                                "symbol": self.venue_symbol(symbol)})
        if not rows:
            raise BitgetError("NO_SPEC", f"no contract for {symbol}", P_CONTRACTS)
        row = rows[0]
        min_notional = float(row.get("minTradeUSDT") or 5)
        try:
            tk = await self._public(P_TICKER, {"productType": self.product_type,
                                               "symbol": self.venue_symbol(symbol)})
            price = float((tk[0].get("lastPr") or tk[0].get("last") or 0)) if tk else 0.0
        except Exception:
            price = 0.0
        if price > 0:
            min_notional = max(min_notional, float(row["minTradeNum"]) * price)
        spec = self._parse_spec({**row, "symbol": symbol}, min_notional=min_notional)
        # Surface the raw status so the tradability check can refuse non-live pairs.
        _status = str(row.get("symbolStatus") or row.get("status") or "normal").lower()
        spec = dataclasses.replace(
            spec, status=_status,
            tradable=_status in ("normal", "live", "trading", ""))
        self._spec_cache[symbol] = spec
        return spec

    @classmethod
    def _parse_spec(cls, row: dict, min_notional: float | None = None) -> InstrumentSpec:
        """Decode the VERIFIED contract shape into step sizes."""
        return InstrumentSpec(
            symbol=row["symbol"],
            price_step=cls._step_from_places(row["pricePlace"]),
            qty_step=cls._step_from_places(row["volumePlace"]),
            min_qty=float(row["minTradeNum"]),
            size_multiplier=float(row.get("sizeMultiplier") or row["minTradeNum"]),
            min_notional=min_notional if min_notional is not None
                         else float(row.get("minTradeUSDT") or 5),
            max_leverage=int(float(row.get("maxLever") or 20)),
            maker_fee=float(row.get("makerFeeRate") or 0.0002),
            taker_fee=float(row.get("takerFeeRate") or 0.0006),
        )

    # ------------------------------------------------------------------ rounding
    @staticmethod
    def _floor_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        # round() guards binary FP so 0.29/0.01 doesn't collapse to 0.28
        return math.floor(round(value / step, 9)) * step

    @classmethod
    def round_price(cls, spec: InstrumentSpec, price: float) -> float:
        """Step price DOWN to the tick, with a magnitude-flip guard (MEXC Edge C).

        A $0.16 coin must never come back as $100: if rounding changes the order of
        magnitude, the original price is returned untouched for the validator to reject.
        """
        if price is None or price <= 0:
            return price
        rounded = cls._floor_step(price, spec.price_step)
        if rounded <= 0:
            return price
        if abs(math.log10(rounded / price)) > 0.5:   # magnitude flip -> refuse
            log.error("round_price magnitude flip %s: %s -> %s", spec.symbol, price, rounded)
            return price
        return round(rounded, 12)

    @classmethod
    def round_qty(cls, spec: InstrumentSpec, qty: float, price_ref: float | None = None,
                  reduce_only: bool = False) -> float:
        """Step qty to the size multiplier and satisfy min qty.

        The min-notional inflation loop is INTENTIONALLY SKIPPED for reduce-only
        (close/partial) orders: a close qty is derived from the position size
        itself, so bumping it to satisfy Bitget's $min_notional would sell more
        than is held and trip ``40804`` ("number of closed positions cannot
        exceed the number held"). Min-notional only matters when *opening* a new
        position.
        """
        step = max(spec.qty_step, spec.size_multiplier)
        q = cls._floor_step(qty, step)
        if q < spec.min_qty:
            q = spec.min_qty
        if not reduce_only and price_ref and price_ref > 0:
            guard = 0
            while q * price_ref < spec.min_notional and guard < 10_000_000:
                q = round(q + step, 12)
                guard += 1
        return round(q, 12)

    # -------------------------------------------------------------------- market
    async def get_mark_price(self, instrument: Instrument) -> float:
        rows = await self._public(P_TICKER, {"productType": self.product_type,
                                             "symbol": self.venue_symbol(instrument.symbol)})
        row = rows[0] if isinstance(rows, list) else rows
        return float(row.get("markPrice") or row["lastPr"])

    # --------------------------------------------------------------------- order
    def _order_body(self, order: OrderRequest, spec: InstrumentSpec) -> dict:
        """Translate an OrderRequest into the VERIFIED V2 place-order body.

        One-way mode: ``side`` is the raw direction and closes use ``reduceOnly=YES``
        (no V1-style ``open_long``/``close_short`` compound strings, which the
        analysis doc wrongly assumed).
        """
        body = {
            "symbol": self.venue_symbol(order.instrument.symbol),
            "productType": self.product_type,
            "marginMode": order.margin_mode,
            "marginCoin": self.margin_coin,
            "size": str(self.round_qty(spec, order.qty, order.price,
                                      reduce_only=order.reduce_only)),
            "side": "buy" if order.side is Side.BUY else "sell",
            "orderType": order.order_type,
            "clientOid": order.client_order_id or self.new_client_oid(),
        }
        if order.order_type == "limit":
            if order.price is None:
                raise ValueError("limit order requires a price")
            body["price"] = str(self.round_price(spec, order.price))
            body["force"] = order.time_in_force
        else:
            body["force"] = "ioc" if order.time_in_force == "gtc" else order.time_in_force
        if order.reduce_only:
            body["reduceOnly"] = "YES"
        else:
            # Live protection is attached once after the fill through
            # place-tpsl-order. Sending presets here as well creates a second
            # venue-generated fill_price plan alongside the bot's mark_price
            # plan. Paper mode keeps the preset fields for fake-backed tests.
            if self.paper:
                if order.sl is not None:
                    body["presetStopLossPrice"] = str(self.round_price(spec, order.sl))
                if order.tp is not None:
                    body["presetStopSurplusPrice"] = str(self.round_price(spec, order.tp))
        return body

    @staticmethod
    def new_client_oid(prefix: str = "bg") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:24]}"

    async def place_order(self, order: OrderRequest) -> str:
        """Place one order. Paper mode short-circuits BEFORE any network call."""
        spec = await self.get_spec(order.instrument)
        body = self._order_body(order, spec)
        if self.paper:
            oid = body["clientOid"]
            log.info("PAPER place_order %s %s size=%s price=%s reduce_only=%s",
                     body["symbol"], body["side"], body["size"], body.get("price"),
                     order.reduce_only)
            return f"paper-{oid}"
        try:
            data = await self._signed("POST", P_PLACE, body=body)
        except BitgetError as exc:
            if not exc.repairable:
                raise
            log.warning("order rejected (%s) — deterministic repair, single resubmit", exc.code)
            self._spec_cache.pop(order.instrument.symbol, None)   # reload real spec
            spec = await self.get_spec(order.instrument)
            body = self._order_body(order, spec)
            data = await self._signed("POST", P_PLACE, body=body)
        return str(data.get("orderId") or data.get("clientOid"))

    async def place_protective(self, order: OrderRequest, plan_type: str = "loss_plan") -> str:
        """Place a reduce-only SL/TP plan order (never auto-cancelled — BUG C)."""
        spec = await self.get_spec(order.instrument)
        trigger = order.trigger_price if order.trigger_price is not None else order.price
        if trigger is None:
            raise ValueError("protective order requires trigger_price or price")
        body = self._build_tpsl_body(spec, order.instrument.symbol, plan_type, trigger,
                                     "buy" if order.side is Side.SELL else "sell",
                                     order.margin_mode, str(self.round_qty(spec, order.qty, trigger)),
                                     order.client_order_id)
        if self.paper:
            return f"paper-{body['clientOid']}"
        data = await self._signed("POST", P_TPSL, body=body)
        return str(data.get("orderId") or data.get("clientOid"))

    def _build_tpsl_body(self, spec, symbol: str, plan_type: str, trigger: float,
                         hold_side: str, margin_mode: str, size: str,
                         client_order_id: str | None) -> dict:
        """Build the place-tpsl-order body.

        Extracted so the field set can be unit-tested WITHOUT a network round-trip.
        All of these fields are REQUIRED by Bitget's ``/mix/order/place-tpsl-order``:
        omitting any of ``side``/``tradeSide``/``orderType``/``timeInForce``/``reduceOnly``
        makes Bitget accept the request (return an orderId) but silently DROP the order
        so it never rests in orders-pending (BUG #3 silent drop). ``holdSide`` must be
        ``buy``/``sell`` (the POSITION side), never ``long``/``short`` (BUG #1, 43011).
        """
        return {
            "symbol": self.venue_symbol(symbol),
            "productType": self.product_type,
            "marginMode": margin_mode,
            "marginCoin": self.margin_coin,
            "planType": plan_type,                       # loss_plan | profit_plan
            "triggerPrice": str(self.round_price(spec, trigger)),
            "triggerType": "mark_price",
            "holdSide": hold_side,
            "side": hold_side,
            "tradeSide": "close",
            "orderType": "market",
            "timeInForce": "normal",
            "reduceOnly": "true",
            "size": size,
            "clientOid": client_order_id or self.new_client_oid("bgp"),
        }

    async def cancel_order(self, instrument: Instrument, client_order_id: str) -> bool:
        if self.paper:
            log.info("PAPER cancel_order %s %s", instrument.symbol, client_order_id)
            return True
        body = {"symbol": self.venue_symbol(instrument.symbol), "productType": self.product_type,
                "clientOid": client_order_id}
        await self._signed("POST", P_CANCEL, body=body)
        return True

    async def get_open_orders(self, instrument: Instrument | None = None) -> list[dict]:
        params = {"productType": self.product_type}
        if instrument:
            params["symbol"] = self.venue_symbol(instrument.symbol)
        data = await self._signed("GET", P_OPEN_ORDERS, params=params)
        if isinstance(data, dict):
            return list(data.get("entrustedList") or [])
        return list(data or [])

    @staticmethod
    def is_protective(raw_order: dict) -> bool:
        """True when an exchange order row is a protective reduce-only order (BUG C)."""
        if str(raw_order.get("reduceOnly", "")).upper() in ("YES", "TRUE", "1"):
            return True
        if raw_order.get("planType") in ("loss_plan", "profit_plan", "pos_loss", "pos_profit"):
            return True
        return str(raw_order.get("tradeSide", "")).lower() == "close"

    async def cancel_all(self, instrument: Instrument | None = None) -> bool:
        """Cancel working ENTRY orders only.

        **BUG C:** protective ``reduceOnly`` / plan orders are explicitly skipped, so
        the dead-man's switch can never strip a position's stop-loss. There is no
        blanket ``cancel-all`` call here on purpose — we enumerate and filter.
        """
        if self.paper:
            log.info("PAPER cancel_all (protective orders preserved)")
            return True
        orders = await self.get_open_orders(instrument)
        skipped = 0
        for row in orders:
            if self.is_protective(row):
                skipped += 1
                continue
            sym = row.get("symbol") or (instrument.symbol if instrument else None)
            if not sym:
                continue
            body = {"symbol": sym, "productType": self.product_type,
                    "orderId": row.get("orderId")}
            try:
                await self._signed("POST", P_CANCEL, body=body)
            except BitgetError as exc:
                log.error("cancel_all failed for %s: %s", row.get("orderId"), exc)
        if skipped:
            log.info("cancel_all preserved %d protective reduce-only order(s)", skipped)
        return True

    # ------------------------------------------------------------------ positions
    async def get_positions(self) -> list[Position]:
        data = await self._signed("GET", P_POSITIONS,
                                  params={"productType": self.product_type,
                                          "marginCoin": self.margin_coin})
        out: list[Position] = []
        for row in data or []:
            size = float(row.get("total") or 0)
            if size == 0:
                continue
            # Map the venue symbol back to canonical so the rest of the bot only
            # ever sees BTCUSDT, never the demo's SBTCSUSDT.
            row = {**row, "symbol": self.canonical_symbol(
                str(row.get("symbol") or row.get("instId") or ""))}
            out.append(self._parse_position(row))
        return out

    async def get_protection(self, instrument: Instrument) -> tuple[float | None, float | None]:
        """Return the REAL resting SL/TP for a symbol, read from the exchange.

        Bug #3 fix: ``/api/state`` must surface the protection that is actually
        live on the venue, not the bot's local (paper-only) ``sl_price``/``tp_price``.
        Bitget exposes protection two ways and we merge both:

        * position-level ``stopLoss``/``takeProfit`` (set via the preset / position
          TP-SL path, ``presetStopLossPrice``), and
        * pending ``loss_plan`` / ``profit_plan`` orders (the ``place-tpsl-order``
          path used by ``set_trading_stop``).

        Either source is authoritative; a plan order wins when both exist because
        it is the explicit, separately-managed stop. Returns ``(sl, tp)`` with
        ``None`` for any level not actually resting on the exchange.
        """
        sym = self.canonical_symbol(instrument.symbol)
        pos_sl = pos_tp = None
        try:
            rows = await self.get_positions()
            for p in rows:
                if p.instrument.symbol == sym:
                    pos_sl, pos_tp = p.sl_price, p.tp_price
                    break
        except Exception as exc:
            log.warning("get_protection position read failed for %s: %s", sym, exc)
        plan_sl = plan_tp = None
        try:
            plan_data = await self._signed(
                "GET", P_PLAN_ORDERS,
                params={"productType": self.product_type,
                        "symbol": self.venue_symbol(instrument.symbol),
                        "planType": "profit_loss"},
            )
            orders = list((plan_data or {}).get("entrustedList") or [])
            for o in orders:
                if str(o.get("symbol") or "").upper() != sym.upper():
                    continue
                if not self.is_protective(o):
                    continue
                px = self._plan_trigger_price(o)
                if px is None:
                    continue
                ptype = str(o.get("planType") or "")
                if ptype in ("loss_plan", "pos_loss"):
                    plan_sl = px
                elif ptype in ("profit_plan", "pos_profit"):
                    plan_tp = px
        except Exception as exc:
            log.warning("get_protection plan-order read failed for %s: %s", sym, exc)
        sl = plan_sl if plan_sl is not None else pos_sl
        tp = plan_tp if plan_tp is not None else pos_tp
        return sl, tp

    @staticmethod
    def _plan_trigger_price(order: dict) -> float | None:
        """Extract the trigger price from a Bitget plan/tpsl order row."""
        raw = order.get("triggerPrice") or order.get("price") or order.get("triggerPriceType")
        if raw in (None, "", 0):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_position(cls, row: dict) -> Position:
        """BUG B: ``liquidationPrice`` is kept REAL — ``None`` if absent, never 0.00.

        Bug #3: ``stopLoss``/``takeProfit`` are the position-level protective levels
        (set via the preset path). Empty strings -> ``None`` so the state surface
        never shows a phantom zero stop. Pending plan orders are merged in via
        ``get_protection`` (see ``PositionManager.sync``).
        """
        liq_raw = row.get("liquidationPrice")
        liq = float(liq_raw) if liq_raw not in (None, "", "0", "0.0") else None
        sl_raw = row.get("stopLoss")
        tp_raw = row.get("takeProfit")
        sl = float(sl_raw) if sl_raw not in (None, "", 0) else None
        tp = float(tp_raw) if tp_raw not in (None, "", 0) else None
        hold = str(row.get("holdSide", "long")).lower()
        return Position(
            instrument=Instrument.from_token(row["symbol"]),
            side=Side.BUY if hold == "long" else Side.SELL,
            size=float(row.get("total") or 0),
            entry_price=float(row.get("openPriceAvg") or row.get("averageOpenPrice") or 0),
            leverage=int(float(row.get("leverage") or 1)),
            sl_price=sl,
            tp_price=tp,
            liq_price=liq,
            unrealized_pnl=float(row.get("unrealizedPL") or 0),
            margin_mode=str(row.get("marginMode") or "isolated"),
            updated_at_ms=int(row.get("uTime") or 0) or None,
        )

    # ---------------------------------------------------------------------- fills
    async def get_fills(self, instrument: Instrument | None = None) -> list[Fill]:
        params = {"productType": self.product_type}
        if instrument:
            params["symbol"] = self.venue_symbol(instrument.symbol)
        data = await self._signed("GET", P_FILLS, params=params)
        rows = data.get("fillList") if isinstance(data, dict) else data
        return [self._parse_fill({**r, "symbol": self.canonical_symbol(
            str(r.get("symbol") or r.get("instId") or ""))}) for r in (rows or [])]

    @classmethod
    def _parse_fill(cls, row: dict) -> Fill:
        """BUG A: record the REAL fee. Bitget reports it NEGATIVE -> normalize to cost."""
        fee = 0.0
        fee_asset = "USDT"
        details = row.get("feeDetail") or []
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except Exception:
                details = []
        for d in details or []:
            fee += abs(float(d.get("totalFee") or d.get("fee") or 0))
            fee_asset = d.get("feeCoin") or fee_asset
        if not details and row.get("fee") is not None:
            fee = abs(float(row["fee"]))
        trade_side = str(row.get("tradeSide", "")).lower()
        return Fill(
            client_order_id=str(row.get("clientOid") or ""),
            price=float(row.get("price") or 0),
            qty=float(row.get("baseVolume") or row.get("size") or 0),
            side=str(row.get("side", "")).upper(),
            fee=fee,
            fee_asset=fee_asset,
            ts_ms=int(row.get("cTime") or 0),
            is_reduce=trade_side == "close",
            exec_type=str(row.get("tradeScope") or row.get("orderType") or ""),
            order_id=str(row.get("orderId") or ""),
            symbol=str(row.get("symbol") or ""),
        )

    # -------------------------------------------------------------------- account
    async def get_balance_usdt(self) -> float:
        data = await self._signed("GET", P_ACCOUNTS,
                                  params={"productType": self.product_type})
        for row in data or []:
            if row.get("marginCoin", "").upper() == self.margin_coin.upper():
                return float(row.get("available") or 0)
        return 0.0

    async def set_leverage(self, instrument: Instrument, leverage: int,
                           hold_side: str | None = None,
                           cap: int | None = None) -> None:
        spec = await self.get_spec(instrument)
        # Never use the pair's headline max leverage for a real fill: that was 75x
        # on PUMP, which produced a 10x fill and a liquidation price inside the
        # stop-loss. The risk config's max_leverage is the authority.
        if cap is not None:
            leverage = min(int(leverage), int(cap))
        lev = max(1, min(int(leverage), spec.max_leverage))
        if self.paper:
            log.info("PAPER set_leverage %s x%d (capped by pair max %d)",
                     instrument.symbol, lev, spec.max_leverage)
            return
        body = {"symbol": self.venue_symbol(instrument.symbol), "productType": self.product_type,
                "marginCoin": self.margin_coin, "leverage": str(lev)}
        if hold_side:
            body["holdSide"] = hold_side
        await self._signed("POST", P_SET_LEVERAGE, body=body)

    async def set_trading_stop(self, instrument: Instrument, sl: float | None,
                               tp: float | None, size: float | None = None,
                               hold_side: str | None = None,
                               margin_mode: str | None = None) -> None:
        """Attach/modify protective levels post-open via plan orders.

        Bitget requires ``holdSide`` (the position direction) on the tpsl endpoint,
        and ``marginMode`` must match the position's actual mode (crossed/isolated)
        or the order is rejected. Both are derived from the live position when not
        supplied, so callers (auto-protect on open, /sl_modify) don't have to pass them.

        CRITICAL (silent-drop hardening): Bitget's ``place-tpsl-order`` endpoint
        ACCEPTS the request and returns a (fake) ``orderId`` even when it silently
        drops the order so it never rests in ``orders-pending`` and never sets the
        position's ``stopLoss``/``takeProfit``. We must VERIFY the protection is
        actually live on the venue afterwards; if it is not, raise so the caller
        escalates loudly instead of believing the position is protected. A blind
        "PLACED" on a phantom order is worse than a visible failure.
        """
        spec = await self.get_spec(instrument)
        if hold_side is None or margin_mode is None:
            live = next((p for p in await self.get_positions()
                         if p.instrument.symbol == instrument.symbol), None)
            if live is not None:
                # Bitget V2 accepts "buy"/"sell" for the position side (not "long"/"short").
                hold_side = hold_side or ("buy" if live.side is Side.BUY else "sell")
                margin_mode = margin_mode or live.margin_mode
        hold_side = hold_side or "buy"
        margin_mode = margin_mode or "crossed"
        for price, plan in ((sl, "loss_plan"), (tp, "profit_plan")):
            if price is None:
                continue
            if self.paper:
                log.info("PAPER set_trading_stop %s %s @ %s", instrument.symbol, plan, price)
                continue
            # Protection is always reduce-only. Never inflate its quantity to
            # satisfy the opening minimum-notional rule: doing so can exceed
            # the actual held position and Bitget rejects it with 43023.
            qty = str(self.round_qty(spec, size, price, reduce_only=True)) if size else "0"
            body = self._build_tpsl_body(spec, instrument.symbol, plan, price,
                                         hold_side, margin_mode, qty,
                                         self.new_client_oid("bgp"))
            await self._signed("POST", P_TPSL, body=body)
        # VERIFY the protection actually rests on the venue. Bitget silently drops
        # the order (returns orderId, rests nothing), so trusting the response is
        # how positions end up naked while the bot thinks they are protected.
        if not self.paper:
            live_sl, live_tp = await self.get_protection(instrument)
            want_sl = float(sl) if sl is not None else None
            want_tp = float(tp) if tp is not None else None
            sl_ok = (want_sl is None) or (live_sl is not None
                    and abs(live_sl - want_sl) / max(want_sl, 1e-9) < 0.01)
            tp_ok = (want_tp is None) or (live_tp is not None
                    and abs(live_tp - want_tp) / max(want_tp, 1e-9) < 0.01)
            if not (sl_ok and tp_ok):
                raise BitgetError(
                    "PROTECTION_NOT_RESTED",
                    f"set_trading_stop {instrument.symbol}: Bitget accepted but protection "
                    f"did not rest (wanted sl={want_sl} tp={want_tp}, got sl={live_sl} tp={live_tp}). "
                    f"Position is NAKED on the venue.",
                    P_TPSL)
