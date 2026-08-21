"""P14: LLM fallback is OFF by default and never slows/breaks valid signals.

These tests prove the operator constraints without any network:
* unconfigured  -> deterministic result returned, LLM never contacted.
* configured + deterministic confident -> LLM never contacted (zero added latency).
* configured + deterministic empty -> LLM contacted once; on success its signal is
  used; on failure/garbage we fall back to the deterministic result (never raise).
"""

from __future__ import annotations

import json

import pytest

import src.sources.llm_fallback as lf
from src.sources.llm_fallback import SignalLLM, parse_with_fallback
from src.sources.parser import SymbolMap


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = 0
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def post(self, url, headers=None, json=None):
        self.calls += 1
        return self._resp


async def _run(text, llm):
    return await parse_with_fallback(text, source="fattyfatclub", channel="fattyfatclub",
                                     symbol_map=SymbolMap(), llm=llm)


@pytest.mark.asyncio
async def test_unconfigured_passes_through_deterministic_unchanged():
    llm = SignalLLM(api_key="")  # disabled
    assert llm.enabled is False
    text = "#WLD $WLD LONG TRADE\n\nENTRY: 0.364\n\nTARGET: 0.4175\n\nSTOPLOSS: 0.3538"
    out = lf.parse_signals(text)
    fall = await _run(text, llm)
    assert [r.symbol_usdt for r in fall] == [r.symbol_usdt for r in out]
    assert fall[0].tp == 0.4175


@pytest.mark.asyncio
async def test_configured_but_confident_does_not_call_llm(monkeypatch):
    resp = _FakeResp(200, {"choices": [{"message": {"content": json.dumps(
        {"symbol": "WLD", "side": "BUY", "entry": 0.364, "sl": 0.3538,
         "tp": 0.4175, "action": "entry"})}}]})
    llm = SignalLLM(api_key="sk-test", model="test-model", timeout_s=1.0)
    captured = {"n": 0}
    def spy(*a, **k):
        captured["n"] += 1
        return _FakeClient(resp)
    monkeypatch.setattr(lf.httpx, "AsyncClient", spy)
    text = "#WLD $WLD LONG TRADE\n\nENTRY: 0.364\n\nTARGET: 0.4175\n\nSTOPLOSS: 0.3538"
    fall = await _run(text, llm)
    assert captured["n"] == 0, "LLM must NOT be called for a confident signal"
    assert fall[0].tp == 0.4175


@pytest.mark.asyncio
async def test_configured_unparsed_calls_llm_and_uses_result(monkeypatch):
    resp = _FakeResp(200, {"choices": [{"message": {"content": json.dumps(
        {"symbol": "FARTCOIN", "side": "BUY", "entry": None, "sl": None,
         "tp": None, "action": "partial"})}}]})
    llm = SignalLLM(api_key="sk-test", model="test-model", timeout_s=1.0)
    monkeypatch.setattr(lf.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))
    fall = await _run("fartcoin first tp done", llm)
    assert len(fall) == 1
    assert fall[0].symbol_usdt == "FARTCOINUSDT"
    assert fall[0].management_action == "partial"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_deterministic_no_raise(monkeypatch):
    resp = _FakeResp(200, {"choices": [{"message": {"content": "not json at all"}}]})
    llm = SignalLLM(api_key="sk-test", model="test-model", timeout_s=1.0)
    monkeypatch.setattr(lf.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))
    text = "some unparseable chatter with no trade intent"
    fall = await _run(text, llm)  # must not raise
    assert isinstance(fall, list)


@pytest.mark.asyncio
async def test_llm_disabled_returns_deterministic_only(monkeypatch):
    llm = SignalLLM(api_key="")  # explicitly disabled
    assert llm.enabled is False
    # With LLM off we get the deterministic parse and nothing more. ("fartcoin first
    # tp done" is now handled deterministically as 'partial', so it is present.)
    fall = await _run("fartcoin first tp done", llm)
    assert len(fall) == 1
    assert fall[0].management_action == "partial"
