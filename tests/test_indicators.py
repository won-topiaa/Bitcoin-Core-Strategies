"""지표 계산 — 손으로 검산 가능한 경우 위주로."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import fixtures
from btc_core import indicators as ind
from btc_core.series import Series


def const(value, days=1500, start=date(2018, 1, 1)):
    return Series.from_pairs(
        [(start + timedelta(days=i), value) for i in range(days)], name="c"
    )


# --- 가격 계열 -------------------------------------------------------------

def test_pi_cycle_is_one_half_on_a_flat_price():
    """가격이 평평하면 111일선 = 350일선 → 비율은 정확히 0.5."""
    iv = ind.pi_cycle_ratio(const(50_000.0))
    assert iv.value == pytest.approx(0.5)
    assert iv.detail["crossed"] is False


def test_pi_cycle_flags_the_cross():
    price = fixtures.price_series()
    iv = ind.pi_cycle_ratio(price)
    assert iv.detail["crossed"] == (iv.value >= 1.0)


def test_pi_cycle_needs_enough_history():
    iv = ind.pi_cycle_ratio(const(50_000.0, days=100))
    assert iv.value is None
    assert "부족" in iv.note


def test_multiples_are_one_on_a_flat_price():
    p = const(50_000.0, days=1600)
    assert ind.golden_ratio_multiple(p).value == pytest.approx(1.0)
    assert ind.ma200w_multiple(p).value == pytest.approx(1.0)
    assert ind.investor_tool_multiple(p).value == pytest.approx(1.0)


def test_golden_ratio_reports_the_next_fibonacci_band():
    p = const(50_000.0, days=1600)
    detail = ind.golden_ratio_multiple(p).detail
    assert detail["next_band"] == 1.6
    assert detail["next_band_price"] == pytest.approx(80_000.0)
    assert detail["bands"]["2"] == pytest.approx(100_000.0)


def test_investor_tool_exposes_both_lines():
    p = const(20_000.0, days=1600)
    detail = ind.investor_tool_multiple(p).detail
    assert detail["buy_line"] == pytest.approx(20_000.0)
    assert detail["sell_line"] == pytest.approx(100_000.0)


def test_ma200w_flat_price_gives_zero_slope_and_blue_heatmap():
    detail = ind.ma200w_multiple(const(50_000.0, days=1600)).detail
    assert detail["monthly_slope_pct"] == pytest.approx(0.0)
    assert "파랑" in detail["heatmap_hint"]


def test_ma200w_rising_price_gives_hot_heatmap():
    rising = Series.from_pairs(
        [(date(2018, 1, 1) + timedelta(days=i), 1000.0 * 1.0025**i) for i in range(1600)]
    )
    detail = ind.ma200w_multiple(rising).detail
    assert detail["monthly_slope_pct"] > 3.0
    assert "빨강" in detail["heatmap_hint"]


# --- 밸류에이션 ------------------------------------------------------------

def test_nupl_formula():
    m = const(1000.0, days=40)
    r = const(250.0, days=40)
    assert ind.nupl(m, r).value == pytest.approx(0.75)


def test_nupl_is_negative_when_realized_cap_exceeds_market_cap():
    iv = ind.nupl(const(800.0, days=40), const(1000.0, days=40))
    assert iv.value == pytest.approx(-0.25)
    assert "항복" in iv.detail["phase"]


@pytest.mark.parametrize(
    "value, expected",
    [(0.80, "도취"), (0.60, "확신"), (0.30, "낙관"), (0.10, "희망"), (-0.10, "항복")],
)
def test_nupl_phase_boundaries(value, expected):
    assert expected in ind.nupl_phase(value)


def test_mvrv_z_matches_the_manual_computation():
    import statistics

    caps = [100.0 + 10.0 * i for i in range(60)]
    m = Series.from_pairs(
        [(date(2020, 1, 1) + timedelta(days=i), c) for i, c in enumerate(caps)]
    )
    r = const(100.0, days=60, start=date(2020, 1, 1))
    expected = (caps[-1] - 100.0) / statistics.pstdev(caps)
    assert ind.mvrv_zscore(m, r).value == pytest.approx(expected)


def test_mvrv_z_reports_date_misalignment_rather_than_failing_silently():
    m = const(1000.0, days=60, start=date(2020, 1, 1))
    r = const(500.0, days=60, start=date(2010, 1, 1))
    iv = ind.mvrv_zscore(m, r)
    assert iv.value is None
    assert "날짜가 겹치지 않습니다" in iv.note


def test_valuation_indicators_are_missing_without_realized_cap():
    assert ind.mvrv_zscore(const(1.0), None).value is None
    assert ind.nupl(None, const(1.0)).value is None


# --- 공급·채굴 -------------------------------------------------------------

def test_puell_is_one_when_issuance_revenue_is_constant():
    p = const(30_000.0, days=500)
    iss = const(450.0, days=500)
    assert ind.puell_multiple(p, iss).value == pytest.approx(1.0)


def test_puell_falls_back_to_supply_differences():
    days = 500
    supply = Series.from_pairs(
        [(date(2018, 1, 1) + timedelta(days=i), 18_000_000.0 + 450.0 * i) for i in range(days)]
    )
    iv = ind.puell_multiple(const(30_000.0, days=days), None, supply)
    assert iv.value == pytest.approx(1.0)


def test_puell_spikes_when_price_doubles():
    days = 500
    prices = [30_000.0] * (days - 1) + [60_000.0]
    p = Series.from_pairs(
        [(date(2018, 1, 1) + timedelta(days=i), v) for i, v in enumerate(prices)]
    )
    iv = ind.puell_multiple(p, const(450.0, days=days))
    assert iv.value == pytest.approx(2.0, rel=0.01)


def test_puell_missing_without_issuance_data():
    assert ind.puell_multiple(const(1.0), None, None).value is None


# --- Hash Ribbons ----------------------------------------------------------

def hashrate_from(values, start=date(2020, 1, 1)):
    return Series.from_pairs(
        [(start + timedelta(days=i), v) for i, v in enumerate(values)], name="h"
    )


def test_hash_ribbons_normal_when_flat():
    assert ind.hash_ribbons(hashrate_from([100.0] * 200)).value == "normal"


def test_hash_ribbons_detects_capitulation():
    # 꾸준히 오르다 급락 → 30일선이 60일선 아래로
    values = [100.0 + i for i in range(150)] + [200.0 - 4.0 * i for i in range(40)]
    assert ind.hash_ribbons(hashrate_from(values)).value == "capitulation"


def test_hash_ribbons_detects_recovery_after_capitulation():
    values = (
        [100.0 + i for i in range(150)]        # 상승
        + [250.0 - 4.0 * i for i in range(40)]  # 급락 → 항복
        + [90.0 + 7.0 * i for i in range(45)]   # 반등 → 재돌파
    )
    assert ind.hash_ribbons(hashrate_from(values)).value == "recovery"


def test_hash_ribbons_expansion_on_sustained_acceleration():
    values = [100.0 * 1.012**i for i in range(220)]
    iv = ind.hash_ribbons(hashrate_from(values))
    assert iv.value == "expansion"
    assert iv.detail["ratio"] >= ind.HASH_EXPANSION_RATIO


def test_hash_ribbons_missing_without_enough_data():
    assert ind.hash_ribbons(hashrate_from([1.0] * 10)).value is None
    assert ind.hash_ribbons(None).value is None


# --- 통합 ------------------------------------------------------------------

def test_compute_all_returns_every_auto_indicator():
    out = ind.compute_all(fixtures.market_data())
    assert set(out) == {
        "pi_cycle", "grm", "ma200w_mult", "ma2y_mult",
        "mvrv_z", "nupl", "puell", "hash_ribbons",
    }
    assert all(v.value is not None for v in out.values())


def test_compute_all_degrades_gracefully_with_price_only():
    md = ind.MarketData(price=fixtures.price_series())
    out = ind.compute_all(md)
    assert out["pi_cycle"].value is not None
    assert out["mvrv_z"].value is None
    assert out["puell"].value is None
    assert out["hash_ribbons"].value is None


def test_synthetic_cycle_sweeps_from_cheap_to_hot_and_back():
    """합성 데이터가 실제로 사이클 모양인지 — 지표 테스트의 전제 확인."""
    md = fixtures.market_data()
    z = [
        ind.compute_all(fixtures.truncated(md, d))["mvrv_z"].value
        for d in (1500, 2000, 2600)
    ]
    assert z[0] < 0 < z[1], "저평가 → 과열 구간이 나와야 한다"
    assert z[2] < z[1], "과열 이후 다시 내려와야 한다"


def test_ma200w_price_is_the_moving_average_itself():
    p = const(50_000.0, days=1600)
    assert ind.ma200w_price(p) == pytest.approx(50_000.0)
