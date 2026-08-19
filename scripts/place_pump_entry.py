"""Place ONE manual entry through the bot's real OrderService.

Uses the same code path a signal would: sizer -> validator -> repair ->
set_leverage -> place_order, with SL and TP attached to the order itself.

Requires --confirm to place. Without it, prints the sizing and exits.
"""
import asyncio
import os
import re
import subprocess
import sys

sys.path.insert(0, "/opt/bots/bitget-listener")

from dotenv import dotenv_values  # noqa: E402

os.environ.update({k: v for k, v in dotenv_values(
    "/opt/bots/bitget-listener/.env").items() if v})
_unit = subprocess.run(["systemctl", "cat", "bitget-listener.service"],
                       capture_output=True, text=True).stdout
for _m in re.finditer(r"^Environment=([A-Z_]+)=(.*)$", _unit, re.M):
    os.environ.setdefault(_m.group(1), _m.group(2).strip())

from src.config.db import PostgresConfigStore          # noqa: E402
from src.domain.model import Instrument, Side, Signal  # noqa: E402
from src.exchanges.bitget import BitgetAdapter         # noqa: E402
from src.services.order import OrderService            # noqa: E402
from src.services.risk import FixedRiskSizer, RiskLimits  # noqa: E402

SYMBOL, SIDE = "PUMPUSDT", Side.BUY
SL, TP = 0.002882, 0.00337


async def main():
    confirm = "--confirm" in sys.argv
    params = PostgresConfigStore().load().params
    print("params:", {k: params.get(k) for k in
                      ("risk_per_trade_usd", "max_position_usd",
                       "min_notional_usd", "max_leverage")})

    adapter = BitgetAdapter(
        key=os.environ["BITGET_API_KEY"],
        secret=os.environ["BITGET_API_SECRET"],
        passphrase=os.environ["BITGET_PASSPHRASE"],
        product_type=os.environ.get("BITGET_PRODUCT_TYPE", "USDT-FUTURES"),
        paper=False,
    )
    inst = Instrument.from_token("PUMP")
    mark = await adapter.get_mark_price(inst)
    spec = await adapter.get_spec(inst)
    print(f"mark={mark}  min_notional=${spec.min_notional:.2f}  "
          f"step={spec.qty_step}  max_lev={spec.max_leverage}")

    limits = RiskLimits(
        risk_per_trade_usd=float(params.get("risk_per_trade_usd", 0.18)),
        max_position_usd=float(params.get("max_position_usd", 6.0)),
        min_notional_usd=float(params.get("min_notional_usd", 5.0)),
        max_leverage=int(params.get("max_leverage", 20)),
    )
    sizer = FixedRiskSizer(limits)
    sized = sizer.size_for_signal(mark, SL, spec, 1)
    print(f"SIZED ok={sized.ok} qty={sized.qty:.0f} "
          f"notional=${sized.notional_usd:.2f} lev={sized.leverage} "
          f"fees=${sized.open_fee + sized.close_fee:.4f} "
          f"reason={sized.reason or '-'}")
    if not sized.ok:
        print("sizer refused; not placing")
        return

    if not confirm:
        print("\nDRY RUN — pass --confirm to place this order")
        return

    svc = OrderService(adapter, sizer=sizer, paper=False)
    sig = Signal(instrument=inst, side=SIDE, entry=mark, sl=SL, tp=TP,
                 notional_usd=sized.notional_usd, leverage=1,
                 source="manual-pump-signal", raw={"manual": True})
    res = await svc.open_from_signal(sig, mark_price=mark)
    print("\nRESULT")
    for k, v in res.items():
        if k in ("order", "sizing"):
            continue
        print(f"  {k}: {v}")
    order = res.get("order")
    if order is not None:
        print(f"  qty={order.qty} sl={order.sl} tp={order.tp} "
              f"lev={order.leverage}")


asyncio.run(main())
