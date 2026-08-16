"""Pure domain types for bitget-listener. No IO, no async, no framework imports.

Adapted from bybit-listener's frozen-domain discipline (the cleanest of the four
siblings) with MEXC's explicit ``Side`` enum grafted on.

All prices are USDT (Bitget USDⓈ-M perpetuals settle in USDT, same as
Binance/Bybit/MEXC — unlike Hyperliquid's USDC, so no cross-stable normalization
is needed; the ``StablecoinPegGuard`` remains as a no-op safety net).

Two contracts encode the inherited bug fixes:
- ``Fill.fee`` is the REAL exchange fee, always recorded (BUG A).
- ``Position.liq_price`` is the REAL exchange liquidation price, never 0.00 (BUG B).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal


class Side(enum.Enum):
    """Internal direction. Translated to Bitget V2 ``side``+``tradeSide`` by the adapter."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


@dataclass(frozen=True)
class Instrument:
    """Bitget canonical instrument. ``symbol`` is e.g. ``BTCUSDT`` (no separator).

    VERIFIED from a live response: Bitget V2 uses the same bare ``BTCUSDT`` form
    for BOTH REST (``symbol``) and WS (``instId``), so no REST⇄WS symbol
    translation is needed (unlike MEXC's ``BTC_USDT`` underscore split).
    """

    symbol: str                      # "BTCUSDT"
    base: str                        # "BTC"
    quote: str = "USDT"
    venue: str = "bitget"
    product_type: str = "USDT-FUTURES"   # VERIFIED V2 value (NOT the dead "umcbl")

    @classmethod
    def from_token(cls, token: str, product_type: str = "USDT-FUTURES") -> "Instrument":
        """Map a channel TOKEN (``BTC``) to a Bitget ``BTCUSDT`` instrument."""
        token = token.upper().strip()
        if token.endswith("USDT"):
            base, symbol = token[:-4], token
        else:
            base, symbol = token, f"{token}USDT"
        return cls(symbol=symbol, base=base, quote="USDT", venue="bitget",
                   product_type=product_type)


@dataclass(frozen=True)
class InstrumentSpec:
    """Exchange trading filters for one symbol.

    Field names map to the VERIFIED Bitget V2 ``/api/v2/mix/market/contracts``
    response (``volumePlace``/``pricePlace``/``minTradeNum``/``sizeMultiplier``/
    ``minTradeUSDT``), decoded into step sizes by the adapter.
    """

    symbol: str
    price_step: float                # from pricePlace (decimals)
    qty_step: float                  # from volumePlace (decimals)
    min_qty: float                   # minTradeNum
    size_multiplier: float           # sizeMultiplier (qty must be a multiple)
    min_notional: float              # minTradeUSDT
    max_leverage: int = 20
    maker_fee: float = 0.0002        # VERIFIED VIP0 USDT-M
    taker_fee: float = 0.0006        # VERIFIED VIP0 USDT-M


@dataclass
class RawSignal:
    """Unnormalized signal as produced by the source (Telegram @fattyfatclub)."""

    symbol_usdt: str = ""
    base: str = ""
    side: Literal["BUY", "SELL"] = "BUY"
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    tp_extra: list[float] = field(default_factory=list)
    notional_usd: float = 0.0
    leverage: int = 1
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)
    source: str = "fattyfatclub"
    message_id: int | None = None
    ts_ms: int | None = None
    has_media: bool = False
    channel: str = ""
    raw_text: str = ""
    image_path: str = ""
    is_management: bool = False
    management_action: str | None = None


@dataclass
class Signal:
    """Normalized, execution-ready signal. All prices in USDT (post-peg-guard)."""

    instrument: Instrument
    side: Side
    entry: float | None
    sl: float | None
    tp: float | None
    notional_usd: float
    leverage: int
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)
    source: str = "fattyfatclub"
    message_id: int | None = None
    ts_ms: int | None = None
    peg_rate: float = 1.0


@dataclass
class OrderRequest:
    """A single order to be placed on Bitget USDⓈ-M futures.

    Centralizes the venue fields so every path (entry, manage, close, cancel)
    uses one builder; ``BitgetAdapter._order_body`` translates this into the
    VERIFIED V2 ``side``/``tradeSide`` pair.
    """

    instrument: Instrument
    side: Side
    order_type: Literal["market", "limit"] = "market"
    qty: float = 0.0                     # base-asset size (e.g. BTC)
    price: float | None = None           # required for limit
    reduce_only: bool = False            # protective orders: NEVER auto-cancelled (BUG C)
    time_in_force: Literal["gtc", "ioc", "fok", "post_only"] = "gtc"
    trigger_price: float | None = None   # plan/trigger order
    sl: float | None = None              # attached preset SL
    tp: float | None = None              # attached preset TP
    leverage: int = 1
    margin_mode: Literal["isolated", "crossed"] = "isolated"
    client_order_id: str | None = None   # clientOid, idempotency key
    decision_id: str | None = None       # links order -> decision for dedup


@dataclass
class Position:
    """Local position state, WS-synced from the exchange."""

    instrument: Instrument
    side: Side
    size: float
    entry_price: float
    leverage: int = 1
    sl_price: float | None = None
    tp_price: float | None = None
    liq_price: float | None = None    # REAL exchange value; never booked as 0.00 (BUG B)
    unrealized_pnl: float = 0.0
    margin_mode: str = "isolated"
    updated_at_ms: int | None = None


@dataclass
class Fill:
    """A real execution. ``fee`` is the REAL exchange fee, recorded always (BUG A)."""

    client_order_id: str
    price: float
    qty: float
    side: str
    fee: float                       # <- REAL fee (Bitget reports it NEGATIVE; adapter normalizes)
    fee_asset: str = "USDT"
    ts_ms: int = 0
    is_reduce: bool = False
    exec_type: str = ""              # "normal" | "liquidation" | "adl" ...
    order_id: str = ""
    symbol: str = ""


@dataclass
class TradeDecision:
    """Output of the decision pipeline (validator fast-path or LLM)."""

    action: Literal["ENTER", "MANAGE", "IGNORE"]
    signal: Signal | None
    leverage: int = 1
    size_coin: float = 0.0
    sl: float | None = None
    tp: float | None = None
    reason: str = ""
    reflexivity_tilt: float = 0.0
