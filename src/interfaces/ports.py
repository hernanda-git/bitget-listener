"""Port interfaces (Dependency Inversion), copied in spirit from bybit-listener.

High-level modules depend on these ABCs, never on concrete adapters, so the bot is
venue-swappable and every unit test runs against a fake with zero network.
"""

from __future__ import annotations

import abc
from asyncio import Queue

from src.domain.model import (
    Fill,
    Instrument,
    InstrumentSpec,
    OrderRequest,
    Position,
)


class SignalSource(abc.ABC):
    """Produces raw signals (Telegram @fattyfatclub)."""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    def subscribe(self) -> "Queue": ...


class ExchangeAdapter(abc.ABC):
    """Executes orders on the venue (Bitget V2 mix). Centralizes symbol/rounding."""

    @abc.abstractmethod
    async def get_spec(self, instrument: Instrument) -> InstrumentSpec: ...

    @abc.abstractmethod
    async def place_order(self, order: OrderRequest) -> str:
        """Return the exchange order id."""

    @abc.abstractmethod
    async def cancel_order(self, instrument: Instrument, client_order_id: str) -> bool: ...

    @abc.abstractmethod
    async def cancel_all(self, instrument: Instrument | None = None) -> bool:
        """Dead-man's switch. MUST NOT touch reduce_only protective orders (BUG C)."""

    @abc.abstractmethod
    async def get_open_orders(self, instrument: Instrument | None = None) -> list[dict]: ...

    @abc.abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    async def get_fills(self, instrument: Instrument | None = None) -> list[Fill]: ...

    @abc.abstractmethod
    async def get_balance_usdt(self) -> float: ...

    @abc.abstractmethod
    async def set_leverage(self, instrument: Instrument, leverage: int,
                           hold_side: str | None = None) -> None: ...

    @abc.abstractmethod
    async def set_trading_stop(self, instrument: Instrument, sl: float | None,
                               tp: float | None, size: float | None = None) -> None:
        """Modify SL/TP after open (Bitget plan orders)."""


class MarketStream(abc.ABC):
    """Delivers realtime market + user events via pushes."""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    def mark_price(self, symbol: str) -> float | None: ...


class Notifier(abc.ABC):
    """Sends alerts + receives /commands."""

    @abc.abstractmethod
    async def send(self, text: str) -> None: ...


class RiskPolicy(abc.ABC):
    """Computes coin size + leverage from a USD notional and portfolio state."""

    @abc.abstractmethod
    def compute_size(self, notional_usd: float, mark_price: float,
                     max_leverage: int) -> tuple[float, int]: ...


class PegSource(abc.ABC):
    """Live USDT peg rate (1.0 on Bitget; guard only)."""

    @abc.abstractmethod
    async def peg_rate(self) -> float: ...


class LedgerStore(abc.ABC):
    """Persists trades with REAL fees (BUG A) and REAL liq price (BUG B)."""

    @abc.abstractmethod
    def record_open(self, **kw) -> int: ...

    @abc.abstractmethod
    def record_close(self, trade_id: int, **kw) -> None: ...
