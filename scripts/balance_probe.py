"""Read-only balance probe: REAL USDT-FUTURES account. Signed GET only, no orders."""
import asyncio
import os
import sys

sys.path.insert(0, "/opt/bots/bitget-listener")

from dotenv import dotenv_values  # noqa: E402

os.environ.update({k: v for k, v in dotenv_values(
    "/opt/bots/bitget-listener/.env").items() if v})

from src.exchanges.bitget import BitgetAdapter  # noqa: E402


async def main():
    for pt in ("USDT-FUTURES", "SUSDT-FUTURES"):
        ad = BitgetAdapter(
            key=os.environ.get("BITGET_API_KEY", ""),
            secret=os.environ.get("BITGET_API_SECRET", ""),
            passphrase=os.environ.get("BITGET_PASSPHRASE", ""),
            product_type=pt,
        )
        try:
            bal = await ad.get_balance_usdt()
            print(f"{pt:15s} margin_coin={ad.margin_coin:6s} available={bal}")
        except Exception as e:
            print(f"{pt:15s} ERR {type(e).__name__}: {e}")


asyncio.run(main())
