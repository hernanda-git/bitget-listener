"""Prove the wired TelegramNotifier really delivers using the process env only."""
import asyncio, os, sys
sys.path.insert(0, "/opt/bots/bitget-listener")
from dotenv import load_dotenv
load_dotenv("/opt/bots/bitget-listener/.env")
from src.notifier.telegram import TelegramNotifier, CommandRouter

n = TelegramNotifier()
print("token_set:", bool(n.token), "chat_id:", n.chat_id, "operator:", os.getenv("TG_OPERATOR_ID"))
ok = asyncio.run(n.send("<b>bitget-listener</b>\nnotifier wired + verified on sera (PAPER mode)"))
print("send_returned:", ok)

r = CommandRouter(gate=None, position_manager=None, shadow_ledger=None)
print("authorized(operator):", r.authorized(os.getenv("TG_OPERATOR_ID")))
print("authorized(stranger):", r.authorized("111111"))
