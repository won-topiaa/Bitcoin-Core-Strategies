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
    # 기준선이 1460일이라 그만큼 이력이 필요하다 — 반감기를 하나 포함해야
    # 발행량이 약분되지 않는다(docs/13).
    days = ind.PUELL_WINDOW + 200
    p = const(30_000.0, days=days)
    iss = const(450.0, days=days)
    assert ind.puell_multiple(p, iss).value == pytest.approx(1.0)


def test_puell_falls_back_to_supply_differences():
    days = ind.PUELL_WINDOW + 200
    supply = Series.from_pairs(
        [(date(2018, 1, 1) + timedelta(days=i), 18_000_000.0 + 450.0 * i) for i in range(days)]
    )
    iv = ind.puell_multiple(const(30_000.0, days=days), None, supply)
    assert iv.value == pytest.approx(1.0)


def test_puell_spikes_when_price_doubles():
    days = ind.PUELL_WINDOW + 200
    prices = [30_000.0] * (days - 1) + [60_000.0]
    p = Series.from_pairs(
        [(date(2018, 1, 1) + timedelta(days=i), v) for i, v in enumerate(prices)]
    )
    iv = ind.puell_multiple(p, const(450.0, days=days))
    assert iv.value == pytest.approx(2.0, rel=0.01)


def test_puell_missing_without_issuance_data():
    assert ind.puell_multiple(const(1.0), None, None).value is None


def test_puell_baseline_spans_a_halving_so_issuance_does_not_cancel():
    """365일 기준선이면 발행량이 약분돼 가격 지표가 된다 — 실측 ρ +0.89.

    1460일은 반감기 간격(약 1458일)을 넘으므로 창 안에서 발행량이 반으로
    줄고, 약분이 일어나지 않는다. 이 상수를 다시 365 로 되돌리면 공급 계열이
    가격 계열의 복사본이 되어 합의 게이트의 전제가 깨진다(docs/13).
    """
    HALVING_INTERVAL_DAYS = 1458
    assert ind.PUELL_WINDOW > HALVING_INTERVAL_DAYS


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
    # 서멀캡은 창세부터의 누적 채굴수익이 있어야 한다. 기본 픽스처는 2018 시작이라
    # 일부러 결측이 되고, 창세부터 깔아주면 계산된다.
    out = ind.compute_all(fixtures.market_data(start=date(2010, 1, 1)))
    assert set(out) == {
        "pi_cycle", "grm", "ma200w_mult", "ma2y_mult",
        "mvrv_z", "nupl", "thermocap", "mcap_per_active", "puell", "hash_ribbons",
    }
    # 활성주소는 픽스처에 없다 — 나머지는 전부 값이 나와야 한다
    missing = [k for k, v in out.items() if v.value is None]
    assert missing == ["mcap_per_active"], missing


def test_thermocap_refuses_a_window_that_does_not_reach_genesis():
    """분모가 창세부터의 누적이 아니면 배수가 부풀려진다.

    실측(2026-07-28, 실제 CoinMetrics 데이터): 2018년부터 자른 8년 창은 배수를
    +10.1%, 4년 창은 +84.4% 과대평가했다. 밸류에이션 계열은 max_abs 라
    부풀려진 값 하나가 30% 지분을 통째로 가져간다. 그럴듯한 숫자를 조용히
    내주느니 결측이 낫다.
    """
    late = ind.compute_all(fixtures.market_data(start=date(2018, 1, 1)))["thermocap"]
    assert late.value is None
    assert "창세" in late.note

    early = ind.compute_all(fixtures.market_data(start=date(2010, 1, 1)))["thermocap"]
    assert early.value is not None


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


def test_puell_falls_back_when_the_usd_series_is_too_short():
    """달러 시계열을 우선한다고 해서 그것만 쓰라는 뜻은 아니다.

    최근 몇 달치뿐인 달러 시계열 때문에 온전한 BTC 시계열까지 못 쓰게 되면
    있는 데이터를 버리는 셈이다.
    """
    md = fixtures.market_data()
    short = md.issuance_usd
    assert short is None                       # 픽스처는 BTC 만 갖고 있다

    full_usd = fixtures.issuance_usd_series()
    stub = Series(full_usd.dates[-60:], full_usd.values[-60:], "issuance_usd")

    both = ind.MarketData(price=md.price, issuance_btc=md.issuance_btc,
                          issuance_usd=stub)
    usd_only = ind.MarketData(price=md.price, issuance_usd=stub)

    assert ind.compute_all(usd_only)["puell"].value is None      # 1460일 평균 불가
    assert ind.compute_all(both)["puell"].value is not None      # BTC 경로로 넘어간다


# --- 고점 프로파일 입력 ----------------------------------------------------

def test_days_since_halving_is_measured_from_the_previous_halving():
    assert ind.days_since_halving(date(2024, 4, 20)) == 0.0
    assert ind.days_since_halving(date(2025, 10, 6)) == 534.0    # 2025 고점
    assert ind.days_since_halving(date(2021, 11, 10)) == 548.0   # 2021 고점
    assert ind.days_since_halving(date(2009, 1, 3)) is None      # 첫 반감기 이전


def test_top_profile_inputs_are_all_percentiles_or_calendar():
    """레벨 기반 조건은 사이클마다 붕괴한다 — 그래서 넣지 않았다.

    2년 MA 배수는 고점에서 17.18 → 9.63 → 2.48 → 1.63,
    Puell 은 9.14 → 6.62 → 1.55 → 1.07 로 무너졌다.
    """
    vals = ind.top_profile_inputs(fixtures.market_data(start=date(2010, 1, 1)))
    assert set(vals) == {"mcap_per_active_pct", "thermocap_pct", "mvrv_z_pct",
                         "days_since_halving", "days_since_ath"}
    for key in ("thermocap_pct", "mvrv_z_pct"):
        assert vals[key] is None or -1.0 <= vals[key] <= 1.0


def test_mcap_per_active_needs_active_addresses():
    md = fixtures.market_data()
    assert md.active_addresses is None
    assert ind.mcap_per_active_address(md.market_cap, None).value is None


def test_every_numeric_indicator_exposes_a_percentile_history():
    """퍼센타일 경로가 모든 수치 지표에 열려 있어야 한다.

    고정 앵커는 마지막 사이클에 맞춰 조정된 것이라 사이클을 넘어 일반화되지
    않는다 — 네 고점의 BCS 표준편차가 앵커 전용 23.6 vs 퍼센타일 전용 16.2,
    그리고 앵커 전용은 2025 고점에서 +28.9 로 분배 첫 계단(+45)에 못 닿는다
    (docs/14, tools/robustness.py).

    이력이 없으면 --adaptive-weight 1.0 (앵커 미사용 모드)이 그 지표에는
    적용되지 않고 조용히 앵커로 되돌아간다. 그걸 막는다.
    """
    out = ind.compute_all(fixtures.market_data(start=date(2010, 1, 1)))
    missing = [
        k for k, v in out.items()
        if not isinstance(v.value, str)              # 범주형은 대상이 아니다
        and v.value is not None
        and not v.detail.get("history_4y")
    ]
    assert not missing, f"history_4y 가 없는 수치 지표: {missing}"
