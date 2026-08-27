from src.domain.model import InstrumentSpec
from src.services.risk import FixedRiskSizer, RiskLimits


def spec(symbol, max_leverage=75, min_notional=5.0):
    return InstrumentSpec(
        symbol=symbol, price_step=0.01, qty_step=0.1, min_qty=0.1,
        size_multiplier=0.1, min_notional=min_notional,
        max_leverage=max_leverage,
    )


def test_normal_profile_is_unchanged():
    result = FixedRiskSizer(RiskLimits(risk_per_trade_usd=0.01)).size_for_signal(
        0.20, 0.19, spec("FARTCOINUSDT"), requested_leverage=10)
    assert result.leverage == 10
    assert result.notional_usd < 90


def test_high_profile_any_large_coin_uses_venue_max_and_minimum_notional():
    limits = RiskLimits(high_risk=True, max_leverage=30, risk_per_trade_usd=0.01)
    result = FixedRiskSizer(limits).size_for_signal(
        150, 1.0, spec("SOLUSDT", max_leverage=100, min_notional=12.0),
        requested_leverage=10)
    assert result.leverage == 100
    assert result.notional_usd >= 12.0
    assert result.notional_usd < 90.0


def test_high_profile_regular_coin_has_30x_and_three_dollar_margin_floor():
    limits = RiskLimits(high_risk=True, max_leverage=30, risk_per_trade_usd=0.01)
    result = FixedRiskSizer(limits).size_for_signal(
        0.20, 0.19, spec("FARTCOINUSDT"), requested_leverage=10)
    assert result.leverage == 30
    assert result.notional_usd >= 90.0
    assert result.notional_usd / result.leverage >= 3.0


def test_high_profile_btc_uses_venue_max_and_effective_minimum_notional():
    limits = RiskLimits(high_risk=True, max_leverage=30, risk_per_trade_usd=0.01)
    result = FixedRiskSizer(limits).size_for_signal(
        70_000, 1.0, spec("BTCUSDT", max_leverage=125, min_notional=8.0),
        requested_leverage=10)
    assert result.leverage == 125
    assert result.notional_usd >= 8.0
    assert result.notional_usd < 90.0


def test_high_profile_inj_minimum_is_not_blocked_by_normal_position_cap():
    limits = RiskLimits(high_risk=True, max_position_usd=10.0,
                        max_leverage=30, risk_per_trade_usd=0.01)
    result = FixedRiskSizer(limits).size_for_signal(
        5.5, 5.345, spec("INJUSDT", max_leverage=30, min_notional=5.0),
        requested_leverage=10)
    assert result.ok
    assert result.leverage == 30
    assert result.notional_usd == 90.0
