"""FakeExchangeAdapter — in-memory venue for fakes-only unit tests (zero network).

Deliberately records fills with a NON-ZERO fee and positions with a REAL
liquidation price, so a test that regresses BUG A or BUG B fails loudly instead of
passing against a fee-free fantasy exchange.
"""

from __future__ import annotations

from src.domain.model import (
    Fill,
    Instrument,
    InstrumentSpec,
    OrderRequest,
    Position,
    Side,
)
from src.interfaces.ports import ExchangeAdapter

DEFAULT_SPEC = InstrumentSpec(
    symbol="BTCUSDT", price_step=0.1, qty_step=0.0001, min_qty=0.0001,
    size_multiplier=0.0001, min_notional=5.0, max_leverage=125,
    maker_fee=0.0002, taker_fee=0.0006,
)


class FakeExchangeAdapter(ExchangeAdapter):
    def __init__(self, spec: InstrumentSpec | None = None, mark: float = 60_000.0,
                 balance: float = 1_000.0, paper: bool = True) -> None:
        self.spec = spec or DEFAULT_SPEC
        self.mark = mark
        self.balance = balance
        self.paper = paper
        self.placed: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.leverage_calls: list[tuple[str, int]] = []
        self.stops: list[tuple[str, float | None, float | None]] = []
        self.fills: list[Fill] = []
        self.positions: list[Position] = []
        self.open_orders: list[dict] = []
        self.fail_next_code: str | None = None

    async def get_spec(self, instrument: Instrument) -> InstrumentSpec:
        return self.spec

    async def get_mark_price(self, instrument: Instrument) -> float:
        return self.mark

    async def place_order(self, order: OrderRequest) -> str:
        if self.fail_next_code:
            from src.exchanges.bitget import BitgetError
            code, self.fail_next_code = self.fail_next_code, None
            raise BitgetError(code, "fake rejection", "/fake")
        self.placed.append(order)
        oid = order.client_order_id or f"fake-{len(self.placed)}"
        taker = self.spec.taker_fee
        price = order.price or self.mark
        self.fills.append(Fill(
            client_order_id=oid, price=price, qty=order.qty,
            side=order.side.value, fee=abs(price * order.qty * taker),
            ts_ms=1, is_reduce=order.reduce_only, exec_type="normal",
            order_id=oid, symbol=order.instrument.symbol,
        ))
        return oid

    async def place_protective(self, order: OrderRequest, plan_type: str = "loss_plan") -> str:
        self.open_orders.append({"orderId": f"prot-{len(self.open_orders)}",
                                 "symbol": order.instrument.symbol,
                                 "planType": plan_type, "reduceOnly": "YES"})
        return f"prot-{len(self.open_orders)}"

    async def cancel_order(self, instrument: Instrument, client_order_id: str) -> bool:
        self.cancelled.append(client_order_id)
        return True

    async def cancel_all(self, instrument: Instrument | None = None) -> bool:
        from src.exchanges.bitget import BitgetAdapter
        keep = [o for o in self.open_orders if BitgetAdapter.is_protective(o)]
        for o in self.open_orders:
            if not BitgetAdapter.is_protective(o):
                self.cancelled.append(str(o.get("orderId")))
        self.open_orders = keep
        return True

    async def get_open_orders(self, instrument: Instrument | None = None) -> list[dict]:
        return list(self.open_orders)

    async def get_positions(self) -> list[Position]:
        return list(self.positions)

    async def get_fills(self, instrument: Instrument | None = None) -> list[Fill]:
        return list(self.fills)

    async def get_balance_usdt(self) -> float:
        return self.balance

    async def set_leverage(self, instrument: Instrument, leverage: int,
                           hold_side: str | None = None) -> None:
        self.leverage_calls.append((instrument.symbol, leverage))

    async def set_trading_stop(self, instrument: Instrument, sl: float | None,
                               tp: float | None, size: float | None = None) -> None:
        self.stops.append((instrument.symbol, sl, tp))

    # ------------------------------------------------------------- test helpers
    def seed_position(self, symbol: str = "BTCUSDT", side: Side = Side.BUY,
                      size: float = 0.01, entry: float = 60_000.0,
                      liq: float = 54_000.0) -> Position:
        pos = Position(instrument=Instrument.from_token(symbol), side=side, size=size,
                       entry_price=entry, leverage=10, liq_price=liq)
        self.positions.append(pos)
        return pos
