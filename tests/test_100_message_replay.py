"""Repeatable 100-message network-free composition-root replay."""

from __future__ import annotations

import pytest

from src.config.db import InMemoryConfigStore
from src.eval.shadow_ledger import ShadowLedger
from src.exchanges.fake import FakeExchangeAdapter
from src.notifier.golive import GateConditions, GoLiveGate
from src.notifier.telegram import TelegramNotifier
from src.services.orchestrator import Orchestrator
from src.services.risk import RiskLimits
from src.sources.parser import SymbolMap
from src.sources.telegram import FakeSignalSource
from src.store.ledger import TradeLedger


@pytest.mark.asyncio
async def test_100_message_network_free_composition_root_replay():
    whitelist = {"BTCUSDT", "ETHUSDT"}
    fake = FakeExchangeAdapter(mark=60_000.0)
    ledger = TradeLedger(":memory:")
    source = FakeSignalSource(symbol_map=SymbolMap(whitelist), trace_store=ledger)
    gate = GoLiveGate(conditions=GateConditions(), config_store=InMemoryConfigStore())
    gate.paper = True
    orch = Orchestrator(
        source, fake, gate, ledger, notifier=TelegramNotifier(token="", chat_id=""),
        shadow=ShadowLedger(min_trades=2),
        limits=RiskLimits(max_concurrent=2, equity_usd=100.0),
        symbol_map=SymbolMap(whitelist), whitelist=whitelist,
    )
    await source.start()

    texts = []
    texts += ["BTCUSDT LONG entry 60000 sl 57000 tp 66000"] * 20
    texts += ["ETHUSDT SHORT entry 2500 sl 2600 tp 2200"] * 10
    texts += ["$BTW hold above 0.38 invalidation below 0.35"] * 15
    texts += ["gm frens no setup"] * 15
    texts += ["sl to entry"] * 10
    texts += ["$BTC LONG and SHORT entry 60000 sl 57000"] * 10
    texts += ["$WIF pullback to 0.174 sl 0.165"] * 10
    texts += ["$SOL breakout below 150 invalidation above 155"] * 10
    assert len(texts) == 100

    for index, text in enumerate(texts, start=1):
        # One repeated Telegram message ID exercises receipt/order idempotency.
        message_id = 7 if index == 8 else index
        await source.push(text, message_id=message_id)
    handled = await orch.run_once()

    received = ledger.get_trace("fake:7")
    all_received = [event for mid in range(1, 101)
                    for event in ledger.get_trace(f"fake:{mid}")
                    if event["stage"] == "RECEIVED"]
    assert handled == 100
    assert len(all_received) == 100
    assert any(e["status"] == "DUPLICATE" for e in received)
    assert len(fake.placed) == 0, "paper replay must not place exchange orders"
    assert any(r["reason"] == "PENDING_SETUP" for r in ledger.get_trace("fake:31"))
    assert any(e["stage"] == "MANAGEMENT" for mid in range(1, 101)
               for e in ledger.get_trace(f"fake:{mid}"))
    assert any(e["reason"] == "DIRECTION_CONFLICT" for mid in range(1, 101)
               for e in ledger.get_trace(f"fake:{mid}"))
    assert any(e["stage"] == "TERMINAL" for mid in range(1, 101)
               for e in ledger.get_trace(f"fake:{mid}"))
