"""Read-only verification of BUG A (real fee) and BUG B (real liq price).

Uses the bot's own signed BitgetAdapter. No orders are ever placed.
Reads: real fills for WLDUSDT, real positions, real USDT balance.
"""
import asyncio
import os
import sys

sys.path.insert(0, "/opt/bots/bitget-listener")
from dotenv import dotenv_values

os.environ.update({k: v for k, v in dotenv_values(
    "/opt/bots/bitget-listener/.env").items() if v})

from src.exchanges.bitget import BitgetAdapter  # noqa: E402
from src.domain.model import Instrument  # noqa: E402


async def main():
    ad = BitgetAdapter(
        key=os.environ.get("BITGET_API_KEY", ""),
        secret=os.environ.get("BITGET_API_SECRET", ""),
        passphrase=os.environ.get("BITGET_PASSPHRASE", ""),
        product_type="USDT-FUTURES",
    )

    print("=== BUG A: real fills for WLDUSDT ===")
    wld = Instrument.from_token("WLDUSDT")
    fills = await ad.get_fills(instrument=wld)
    print(f"fill count (WLDUSDT): {len(fills)}")
    real_fee_count = 0
    for f in fills:
        print(f"  fill price={f.price} qty={f.qty} side={f.side} "
              f"fee={f.fee} fee_asset={f.fee_asset} ts_ms={f.ts_ms}")
        if f.fee is not None and f.fee > 0:
            real_fee_count += 1
    print(f"fills with REAL fee>0: {real_fee_count}/{len(fills)}")

    print("=== BUG B: real positions (liq price) ===")
    positions = await ad.get_positions()
    print(f"open positions: {len(positions)}")
    for p in positions:
        print(f"  {p.instrument} side={p.side} size={p.size} "
              f"entry={p.entry_price} liq_price={p.liq_price} "
              f"margin_mode={p.margin_mode}")
        if p.liq_price is None:
            print("    liq_price=None (REAL — not coerced to 0.00) OK")
        elif p.liq_price > 0:
            print("    liq_price>0 (REAL) OK")
        else:
            print("    !! liq_price<=0 — POSSIBLE BUG")

    print("=== REAL balance (audit, not internal net_usd) ===")
    bal = await ad.get_balance_usdt()
    print(f"USDT-FUTURES available balance = {bal}")


asyncio.run(main())
