"""파생 시계열 — 무료 데이터 의존을 price + realized_cap 둘로 줄이는 장치.

이 모듈이 지키는 것은 하나다. **계산으로 대체할 수 있는 것은 대체하고,
대체할 수 없는 것은 절대 만들어내지 않는다.** 실현시총을 그럴듯한 값으로
채우는 순간 지표 전체가 거짓이 되므로, 그 경계를 테스트로 못 박는다.
"""

from __future__ import annotations

from datetime import date, timedelta

import fixtures
import pytest

from btc_core.datasources.base import DataBundle
from btc_core.datasources.derive import (
    BLOCKS_PER_ERA,
    GENESIS,
    HALVING_SCHEDULE,
    backfill,
    backfill_bundle,
    derive_daily_issuance,
    derive_supply,
    issuance_series,
    supply_series,
)
from btc_core.indicators import HALVINGS, MarketData
from btc_core.series import Series


# --- 발행 스케줄 자체 -------------------------------------------------------

def test_schedule_agrees_with_the_halving_dates_used_for_cycle_progress():
    """유통량과 사이클 진행도가 같은 달력을 봐야 한다.

    어긋나면 저점·고점 프로파일의 '반감기 이후 N일' 조건과 밸류에이션 계열이
    서로 다른 사이클을 이야기한다. import 시점에도 검사하지만 여기서도 본다.
    """
    assert tuple(d for d, _ in HALVING_SCHEDULE[1:]) == tuple(HALVINGS)


def test_supply_is_exact_at_every_halving_boundary():
    """구간 경계에서는 근사가 아니라 정확해야 한다 — 210,000 × 보상의 누적이다."""
    expected = 0.0
    for i, (when, reward) in enumerate(HALVING_SCHEDULE):
        assert derive_supply(when) == pytest.approx(expected, rel=1e-9), when
        expected += BLOCKS_PER_ERA * reward


def test_supply_never_decreases():
    d = GENESIS
    prev = 0.0
    while d < date(2030, 1, 1):
        cur = derive_supply(d)
        assert cur >= prev, d
        prev = cur
        d += timedelta(days=37)


def test_supply_converges_below_twenty_one_million():
    assert derive_supply(date(2040, 1, 1)) < 21_000_000
    assert derive_supply(date(2040, 1, 1)) > 20_600_000


def test_supply_is_zero_before_genesis():
    assert derive_supply(GENESIS - timedelta(days=1)) == 0.0
    assert derive_supply(date(1990, 1, 1)) == 0.0


def test_daily_issuance_matches_the_block_subsidy_in_each_era():
    """일간 발행량 ≈ 하루치 블록(약 144개) × 보상."""
    for probe, reward in ((date(2018, 6, 1), 12.5),
                          (date(2022, 6, 1), 6.25),
                          (date(2025, 6, 1), 3.125)):
        got = derive_daily_issuance(probe)
        assert got == pytest.approx(144 * reward, rel=0.05), probe


def test_daily_issuance_is_never_negative():
    """반감기 당일에 음수가 나오면 Puell 이 뒤집힌다."""
    for when, _ in HALVING_SCHEDULE:
        for offset in (-1, 0, 1):
            assert derive_daily_issuance(when + timedelta(days=offset)) >= 0.0


def test_issuance_sums_back_to_supply():
    dates = tuple(date(2021, 1, 1) + timedelta(days=i) for i in range(400))
    s = supply_series(dates)
    iss = issuance_series(dates)
    grew = s.values[-1] - s.values[0]
    summed = sum(v for v in iss.values[1:] if v is not None)
    assert summed == pytest.approx(grew, rel=1e-6)


# --- backfill 의 경계 -------------------------------------------------------

def test_backfill_leaves_measured_values_untouched():
    """실측이 있는 날은 값이 바뀌지 않는다 — 파생이 실측을 덮으면 안 된다."""
    md = fixtures.market_data()
    filled, notes = backfill(md)
    for name in ("supply", "market_cap"):
        before = dict(getattr(md, name))
        after = dict(getattr(filled, name))
        for d, v in before.items():
            if v is not None:
                assert after[d] == v, f"{name} {d} 가 파생값으로 덮였다"
    # 픽스처에 issuance_usd 가 없으므로 그것만 채워진다
    assert any("발행액" in n for n in notes)
    assert not any("유통량" in n for n in notes)


def test_backfill_patches_a_trailing_gap():
    """가장 흔한 실제 상황 — 최신 하루가 늦어 마지막 행만 비어 있다.

    CoinMetrics 커뮤니티 티어가 정확히 이렇다. 시계열 전체가 없을 때만 채우면
    이 하루를 놓치고, 오늘 값을 보려고 만든 지표에서 **오늘의 커버리지가**
    떨어진다.
    """
    md = fixtures.market_data()
    trimmed = MarketData(
        price=md.price,
        market_cap=Series(md.market_cap.dates[:-1], md.market_cap.values[:-1], "market_cap"),
        realized_cap=md.realized_cap,
        supply=Series(md.supply.dates[:-1], md.supply.values[:-1], "supply"),
        issuance_btc=md.issuance_btc,
        hashrate=md.hashrate,
    )
    filled, notes = backfill(trimmed)
    last = md.price.dates[-1]
    assert filled.supply.value_on(last) is not None
    assert filled.supply.dates[-1] == last
    assert filled.market_cap.dates[-1] == last
    assert any("1일" in n and "유통량" in n for n in notes), notes


def test_backfill_fills_supply_market_cap_and_issuance_from_price_alone():
    md = MarketData(price=fixtures.price_series())
    filled, notes = backfill(md)
    for name in ("supply", "market_cap", "issuance_btc", "issuance_usd"):
        s = getattr(filled, name)
        assert s is not None and s.last is not None, name
    assert len(notes) >= 4


def test_derived_market_cap_is_price_times_supply():
    md = MarketData(price=fixtures.price_series())
    filled, _ = backfill(md)
    d = filled.market_cap.dates[-1]
    assert filled.market_cap.last == pytest.approx(
        filled.price.last * filled.supply.value_on(d)
    )


def test_backfill_never_invents_realized_cap():
    """실현시총은 체인 전체를 훑어야 나오는 값이다. 만들어내면 지표가 거짓이 된다."""
    md = MarketData(price=fixtures.price_series())
    filled, notes = backfill(md)
    assert filled.realized_cap is None
    assert any("실현시총" in n for n in notes)


def test_backfill_never_invents_hashrate_or_active_addresses():
    md = MarketData(price=fixtures.price_series())
    filled, _ = backfill(md)
    assert filled.hashrate is None
    assert filled.active_addresses is None


def test_backfill_without_price_does_nothing():
    md = MarketData(price=Series((), (), "price"))
    filled, notes = backfill(md)
    assert filled is md
    assert notes and "가격" in notes[0]


def test_backfill_replaces_an_all_missing_series():
    """열이 존재하지만 값이 전부 None 인 CSV — 결측과 똑같이 다뤄야 한다."""
    price = fixtures.price_series()
    empty = Series(price.dates, tuple(None for _ in price.dates), "supply")
    filled, notes = backfill(MarketData(price=price, supply=empty))
    assert filled.supply.last is not None
    assert any("유통량" in n for n in notes)


# --- 번들 통합 --------------------------------------------------------------

def test_backfill_bundle_surfaces_the_derivation_in_the_warnings():
    """파생값을 쓰고 있다는 사실이 리포트에서 사라지면 안 된다."""
    bundle = DataBundle(market=MarketData(price=fixtures.price_series()), origin="테스트")
    out = backfill_bundle(bundle)
    assert out is not bundle
    assert any("반감기 스케줄" in w for w in out.warnings)
    assert "파생" in out.origin


def test_backfill_bundle_is_a_no_op_when_nothing_is_missing():
    market, _ = backfill(fixtures.market_data())
    bundle = DataBundle(market=market, origin="테스트")
    assert backfill_bundle(bundle) is bundle


# --- 실측 대비 정확도 (data/market.csv 가 있을 때만) -------------------------

def test_derived_supply_tracks_the_measured_series_since_2015():
    """2015년 이후 평균 절대오차가 0.1% 를 넘으면 파생을 믿을 수 없다.

    측정값은 평균 0.079% / 최대 0.989% 다. 초기(2009~2010)는 해시레이트가
    거의 없어 블록이 10분보다 훨씬 느리게 나왔고, 이 모델은 구간 안에서 블록이
    균일하다고 가정하므로 그 시절 오차가 크다 — 대신 그 시절은 가격 데이터도
    없어서 지표 계산에 들어오지 않는다.
    """
    from pathlib import Path

    csv = Path(__file__).resolve().parents[1] / "data" / "market.csv"
    if not csv.exists():
        pytest.skip("data/market.csv 없음 — 오프라인 검증 생략")

    from btc_core.datasources.csv_source import load_csv_bundle

    measured = load_csv_bundle(csv).market.supply
    if measured is None:
        pytest.skip("CSV 에 유통량 열이 없음")

    errs = [
        abs(derive_supply(d) - v) / v
        for d, v in zip(measured.dates, measured.values)
        if v and d >= date(2015, 1, 1)
    ]
    assert errs, "2015년 이후 데이터가 없다"
    assert sum(errs) / len(errs) < 0.001
    assert max(errs) < 0.02
