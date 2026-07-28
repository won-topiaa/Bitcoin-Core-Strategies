"""스코어 엔진 — 계열 집계, 가중치 재정규화, 합의 게이트."""

from __future__ import annotations

import pytest

from btc_core.config import load_config
from btc_core.models import Reading
from btc_core.score import compute_bcs, compute_lrs, evaluate_consensus, score_indicator


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def reading(key, score, family, source="auto"):
    return Reading(key, key, family, None, score, source)


def build(cfg, **scores):
    """지표 키 → 점수 로 Reading 묶음을 만든다. 빠진 지표는 결측."""
    out = {}
    for key, spec in cfg.indicators.items():
        out[key] = reading(key, scores.get(key), spec["family"], spec.get("source", "auto"))
    return out


# --- 지표 점수화 -----------------------------------------------------------

def test_score_indicator_uses_the_configured_anchors(cfg):
    r = score_indicator(cfg, "nupl", 0.75)
    assert r.score == pytest.approx(0.90)
    assert r.available


def test_score_indicator_marks_missing_input(cfg):
    r = score_indicator(cfg, "nupl", None)
    assert not r.available
    assert r.note == "결측"


def test_score_indicator_rejects_non_numeric(cfg):
    r = score_indicator(cfg, "nupl", "제법 높음")
    assert not r.available
    assert "숫자" in r.note


def test_categorical_indicator_scores_by_state(cfg):
    assert score_indicator(cfg, "hash_ribbons", "recovery").score == pytest.approx(-1.0)
    assert score_indicator(cfg, "hash_ribbons", "capitulation").score == pytest.approx(-0.55)
    assert not score_indicator(cfg, "hash_ribbons", "무슨상태").available


def test_dxy_is_inverted_because_a_weak_dollar_helps_bitcoin(cfg):
    spec = cfg.lrs_components["dxy_trend"]
    falling = score_indicator(cfg, "dxy_trend", -3.0, spec=spec)   # 달러 약세
    rising = score_indicator(cfg, "dxy_trend", 3.0, spec=spec)     # 달러 강세
    assert falling.score > 0 > rising.score


def test_percentile_blending_pulls_toward_the_historical_distribution(cfg):
    history = [0.0] * 200            # 과거는 전부 0 → 지금 값은 최상위
    plain = score_indicator(cfg, "mvrv_z", 2.0)
    blended = score_indicator(cfg, "mvrv_z", 2.0, history=history)
    assert blended.score > plain.score
    assert "갈립니다" in blended.note


def test_short_history_does_not_trigger_blending(cfg):
    r = score_indicator(cfg, "mvrv_z", 2.0, history=[0.0] * 10)
    assert r.score == pytest.approx(score_indicator(cfg, "mvrv_z", 2.0).score)


# --- 계열 집계 -------------------------------------------------------------

def test_valuation_uses_max_abs_so_correlated_pairs_are_not_double_counted(cfg):
    """MVRV Z 와 NUPL 은 계산 재료가 같다. 평균이면 신호가 희석된다."""
    readings = build(cfg, mvrv_z=0.9, nupl=0.3)
    _, families, _ = compute_bcs(cfg, readings)
    valuation = next(f for f in families if f.key == "valuation")
    assert valuation.score == pytest.approx(0.9)      # 평균 0.6 이 아니라 극단값


def test_max_abs_picks_the_extreme_even_when_negative(cfg):
    readings = build(cfg, mvrv_z=-0.8, nupl=0.2)
    _, families, _ = compute_bcs(cfg, readings)
    assert next(f for f in families if f.key == "valuation").score == pytest.approx(-0.8)


def test_price_family_averages_its_four_moving_average_variants(cfg):
    readings = build(cfg, pi_cycle=1.0, grm=0.0, ma200w_mult=0.0, ma2y_mult=0.0)
    _, families, _ = compute_bcs(cfg, readings)
    assert next(f for f in families if f.key == "price").score == pytest.approx(0.25)


def test_bcs_is_the_weighted_sum_of_family_scores(cfg):
    readings = build(
        cfg, mvrv_z=1.0, nupl=1.0,
        rhodl=1.0, reserve_risk=1.0, lth_mvrv=1.0,
        pi_cycle=1.0, grm=1.0, ma200w_mult=1.0, ma2y_mult=1.0,
        puell=1.0, hash_ribbons=1.0,
    )
    bcs, _, coverage = compute_bcs(cfg, readings)
    assert bcs == pytest.approx(100.0)
    assert coverage == pytest.approx(1.0)


def test_bcs_bottoms_out_at_minus_one_hundred(cfg):
    readings = build(cfg, **{k: -1.0 for k in cfg.indicators})
    bcs, _, _ = compute_bcs(cfg, readings)
    assert bcs == pytest.approx(-100.0)


def test_all_neutral_gives_zero(cfg):
    readings = build(cfg, **{k: 0.0 for k in cfg.indicators})
    bcs, _, _ = compute_bcs(cfg, readings)
    assert bcs == pytest.approx(0.0)


# --- 결측과 재정규화 -------------------------------------------------------

def test_missing_family_redistributes_its_weight(cfg):
    """밸류에이션(30) 이 빠지면 나머지 70 이 100 으로 늘어난다."""
    readings = build(
        cfg,
        rhodl=1.0, reserve_risk=1.0, lth_mvrv=1.0,
        pi_cycle=1.0, grm=1.0, ma200w_mult=1.0, ma2y_mult=1.0,
        puell=1.0, hash_ribbons=1.0,
    )
    bcs, families, coverage = compute_bcs(cfg, readings)
    assert coverage == pytest.approx(0.70)
    assert bcs == pytest.approx(100.0)     # 남은 계열이 모두 +1 이므로 여전히 100

    valuation = next(f for f in families if f.key == "valuation")
    assert not valuation.available
    assert valuation.effective_weight == 0.0

    live = [f for f in families if f.available]
    assert sum(f.effective_weight for f in live) == pytest.approx(100.0)
    holder = next(f for f in live if f.key == "holder")
    assert holder.effective_weight == pytest.approx(25 / 70 * 100)


def test_a_family_survives_on_a_single_member(cfg):
    readings = build(cfg, reserve_risk=-0.8)
    _, families, _ = compute_bcs(cfg, readings)
    holder = next(f for f in families if f.key == "holder")
    assert holder.available
    assert holder.score == pytest.approx(-0.8)


def test_everything_missing_yields_no_score(cfg):
    bcs, families, coverage = compute_bcs(cfg, build(cfg))
    assert bcs is None
    assert coverage == pytest.approx(0.0)
    assert all(not f.available for f in families)


# --- 합의 게이트 -----------------------------------------------------------

def test_consensus_passes_when_three_families_including_two_non_price_agree(cfg):
    readings = build(
        cfg, mvrv_z=0.8, reserve_risk=0.5, rhodl=0.5, lth_mvrv=0.5,
        pi_cycle=0.6, grm=0.6, ma200w_mult=0.6, ma2y_mult=0.6,
    )
    bcs, families, _ = compute_bcs(cfg, readings)
    c = evaluate_consensus(cfg, families, bcs)
    assert c.passed
    assert c.direction == "overheated"
    assert len(c.non_price_agreeing) >= 2


def test_consensus_blocks_a_price_only_signal(cfg):
    """가격 이동평균 4종만 빨개진 상황 — 가장 흔한 착시를 걸러내야 한다."""
    readings = build(
        cfg, pi_cycle=0.9, grm=0.9, ma200w_mult=0.9, ma2y_mult=0.9,
        mvrv_z=0.05, nupl=0.05, reserve_risk=0.05, rhodl=0.05, lth_mvrv=0.05,
        puell=0.05, hash_ribbons=0.0,
    )
    bcs, families, _ = compute_bcs(cfg, readings)
    c = evaluate_consensus(cfg, families, bcs)
    assert bcs > 0
    assert not c.passed
    assert c.agreeing == ("price",)


def test_consensus_blocks_when_only_two_families_agree(cfg):
    readings = build(
        cfg, mvrv_z=0.9, reserve_risk=0.9, rhodl=0.9, lth_mvrv=0.9,
        pi_cycle=0.0, grm=0.0, ma200w_mult=0.0, ma2y_mult=0.0,
        puell=0.0, hash_ribbons=0.0,
    )
    bcs, families, _ = compute_bcs(cfg, readings)
    c = evaluate_consensus(cfg, families, bcs)
    assert not c.passed
    assert "합의 미달" in c.reason


def test_weak_families_below_threshold_do_not_count_as_agreement(cfg):
    readings = build(
        cfg, mvrv_z=0.9, reserve_risk=0.2, rhodl=0.2, lth_mvrv=0.2, puell=0.2,
        hash_ribbons=0.2, pi_cycle=0.2, grm=0.2, ma200w_mult=0.2, ma2y_mult=0.2,
    )
    _, families, _ = compute_bcs(cfg, readings)
    bcs, _, _ = compute_bcs(cfg, readings)
    c = evaluate_consensus(cfg, families, bcs)
    assert c.agreeing == ("valuation",)
    assert not c.passed


def test_consensus_needs_a_direction(cfg):
    readings = build(cfg, **{k: 0.0 for k in cfg.indicators})
    bcs, families, _ = compute_bcs(cfg, readings)
    c = evaluate_consensus(cfg, families, bcs)
    assert not c.passed
    assert c.direction == "neutral"


def test_consensus_handles_a_missing_bcs(cfg):
    c = evaluate_consensus(cfg, (), None)
    assert not c.passed
    assert "데이터 부족" in c.reason


def test_consensus_works_in_the_undervalued_direction(cfg):
    readings = build(
        cfg, mvrv_z=-0.8, reserve_risk=-0.6, rhodl=-0.6, lth_mvrv=-0.6,
        puell=-0.7, hash_ribbons=-1.0,
        pi_cycle=-0.5, grm=-0.5, ma200w_mult=-0.5, ma2y_mult=-0.5,
    )
    bcs, families, _ = compute_bcs(cfg, readings)
    c = evaluate_consensus(cfg, families, bcs)
    assert c.passed
    assert c.direction == "undervalued"
    assert bcs < 0


# --- LRS -------------------------------------------------------------------

def test_lrs_is_a_weighted_average_of_its_components(cfg):
    lrs, readings, band = compute_lrs(
        cfg,
        {"m2_impulse": 12.0, "dxy_trend": -6.0, "fed_stance": "cutting_qe", "etf_flow": 12.0},
    )
    assert lrs == pytest.approx(100.0)
    assert band == "easing"
    assert all(r.available for r in readings)


def test_lrs_bottoms_out_under_tightening(cfg):
    lrs, _, band = compute_lrs(
        cfg,
        {"m2_impulse": -2.0, "dxy_trend": 6.0, "fed_stance": "qt_hiking", "etf_flow": -6.0},
    )
    assert lrs == pytest.approx(-100.0)
    assert band == "tightening"


def test_lrs_renormalizes_over_available_components(cfg):
    lrs, _, _ = compute_lrs(cfg, {"m2_impulse": 12.0})
    assert lrs == pytest.approx(100.0)


def test_lrs_is_none_without_any_input(cfg):
    lrs, readings, band = compute_lrs(cfg, {})
    assert lrs is None
    assert band == "unknown"
    assert all(not r.available for r in readings)
