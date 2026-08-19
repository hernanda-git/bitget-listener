"""Read-only probe: effective min notional per coin on REAL USDT-FUTURES.

Public endpoints only (contracts + ticker). No keys, no orders.
"""
import asyncio
import sys

sys.path.insert(0, "/opt/bots/bitget-listener")

from src.domain.model import Instrument          # noqa: E402
from src.exchanges.bitget import BitgetAdapter   # noqa: E402

TOKENS = ("BTC", "ETH", "SOL", "BNB", "LTC", "XRP", "ADA", "DOGE", "TRX", "NEAR")
BALANCE = 8.879943


async def main():
    ad = BitgetAdapter(product_type="USDT-FUTURES")
    for tok in TOKENS:
        try:
            s = await ad.get_spec(Instrument.from_token(tok))
            verdict = "TRADABLE" if s.min_notional <= BALANCE else "TOO BIG"
            pct = s.min_notional / BALANCE * 100
            print(f"{tok:5s} minTradeNum={s.min_qty:<12g} "
                  f"eff_min_notional=${s.min_notional:>8.2f}  "
                  f"{pct:>6.1f}% of ${BALANCE:.2f}  {verdict}")
        except Exception as e:
            print(f"{tok:5s} ERR {type(e).__name__}: {e}")


asyncio.run(main())
