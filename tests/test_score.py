"""스코어 엔진 — 계열 집계, 가중치 재정규화, 합의 게이트."""

from __future__ import annotations

import pytest

from btc_core.config import load_config
from btc_core.models import FamilyScore, Reading
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
    # 앵커에 [0.70, 0.92] 와 [0.80, 1.00] 이 있으므로 0.75 는 그 중간
    r = score_indicator(cfg, "nupl", 0.75)
    assert r.score == pytest.approx(0.96)
    assert r.available


def test_score_indicator_hits_every_anchor_exactly(cfg):
    """설정의 앵커 좌표가 그대로 점수로 나오는지 — 전 지표 전 좌표.

    퍼센타일 전용 지표(requires_history)는 앵커를 쓰지 않으므로 제외한다.
    """
    for key, spec in cfg.indicators.items():
        if spec.get("input_mode") == "categorical" or spec.get("requires_history"):
            continue
        for raw, expected in spec["anchors"]:
            got = score_indicator(cfg, key, raw, spec=spec).score
            want = -expected if spec.get("invert") else expected
            assert got == pytest.approx(want), f"{key} @ {raw}"


# --- 퍼센타일 전용 지표 ----------------------------------------------------

def test_history_only_indicator_is_missing_without_history(cfg):
    """서멀캡은 절대 수준에 구조적 추세가 있어서 앵커 단독 사용을 막아야 한다."""
    r = score_indicator(cfg, "thermocap", 20.0)
    assert not r.available
    assert "이력 부족" in r.note


def test_history_only_indicator_ignores_its_anchors(cfg):
    """같은 원시값이라도 과거 분포에 따라 점수가 완전히 달라져야 한다."""
    low = score_indicator(cfg, "thermocap", 20.0, history=[50.0] * 200)
    high = score_indicator(cfg, "thermocap", 20.0, history=[5.0] * 200)
    assert low.score == pytest.approx(-1.0)
    assert high.score == pytest.approx(1.0)


def test_per_indicator_adaptive_weight_overrides_the_global(cfg):
    """지표가 지정한 혼합 비율이 호출 인자보다 우선한다."""
    spec = cfg.indicators["thermocap"]
    assert spec["adaptive_weight"] == 1.0
    # 전역 인자를 0 으로 줘도 퍼센타일 100% 가 유지된다
    r = score_indicator(cfg, "thermocap", 20.0, history=[5.0] * 200, adaptive_weight=0.0)
    assert r.score == pytest.approx(1.0)


def test_thermocap_does_not_weaken_the_valuation_family_at_bottoms(cfg):
    """max_abs 라서 바닥에서는 더 극단인 MVRV 가 선택된다.

    서멀캡은 고점 탐지에 강하고 바닥 탐지에 약한데(4년 퍼센타일 기준 저점
    평균 -0.56), 집계 방식이 이 약점을 무해하게 만든다.
    """
    readings = build(cfg, mvrv_z=-0.99, nupl=-0.95, thermocap=-0.12)
    _, families, _ = compute_bcs(cfg, readings)
    valuation = next(f for f in families if f.key == "valuation")
    assert valuation.score == pytest.approx(-0.99)


def test_thermocap_can_lift_the_valuation_family_at_tops(cfg):
    readings = build(cfg, mvrv_z=0.71, nupl=0.63, thermocap=0.91)
    _, families, _ = compute_bcs(cfg, readings)
    valuation = next(f for f in families if f.key == "valuation")
    assert valuation.score == pytest.approx(0.91)


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


# --- 비유한값 방어 ---------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_input_becomes_missing_not_a_crash(cfg, bad):
    """NaN 은 꺾은선 보간을 IndexError 로 터뜨렸고 ±inf 는 조용히 ±1.00 이 됐다.

    CSV 의 'nan' 문자열과 YAML 의 .nan/.inf 이 float() 를 통과해 여기까지 온다.
    """
    r = score_indicator(cfg, "mvrv_z", bad)
    assert not r.available
    assert "유한한 숫자가 아닙니다" in r.note


def test_non_finite_history_does_not_poison_the_percentile(cfg):
    r = score_indicator(cfg, "mvrv_z", 2.0, history=[float("nan")] * 5 + [0.0] * 200)
    assert r.available


# --- 합의 게이트의 안전 속성 -----------------------------------------------

def price_only_families(score=0.9):
    """가격 계열만 살아 있고 나머지는 전부 결측인 상태."""
    return [
        FamilyScore("price", "가격", 25, 100, score),
        FamilyScore("valuation", "밸류에이션", 30, 0, None),
        FamilyScore("holder", "보유자", 25, 0, None),
        FamilyScore("supply", "공급", 20, 0, None),
    ]


def test_price_family_alone_can_never_pass_the_gate(cfg):
    """이 게이트의 존재 이유가 정확히 이 상황을 막는 것이다.

    결측 비례 완화가 비가격 요구까지 0으로 낮춰서, 가격 계열 단독으로
    게이트를 통과하는 구멍이 있었다. 커버리지 하한이 가려주고 있었을 뿐
    안전 속성이 코드에 보장돼 있지 않았다.
    """
    for direction in (0.9, -0.9):
        c = evaluate_consensus(cfg, price_only_families(direction), 22.5 * (1 if direction > 0 else -1))
        assert not c.passed
        assert "가격 이동평균에만 의존" in c.reason


def test_requirements_never_ask_for_more_non_price_than_exist(cfg):
    """가격 계열이 없는 조합이 오히려 더 엄격해지는 역전이 있었다."""
    import itertools

    from btc_core.score import _requirements

    keys = list(cfg.bcs_families)
    for r in range(1, len(keys) + 1):
        for combo in itertools.combinations(keys, r):
            avail = [FamilyScore(k, k, cfg.bcs_families[k]["weight"], 0, 0.5) for k in combo]
            need, need_np, _ = _requirements(cfg, cfg.consensus, avail)
            n_np = sum(1 for k in combo if k != "price")
            assert need <= len(combo), combo
            if "price" not in combo:
                assert need_np == 0, f"{combo}: 가격 계열이 없는데 비가격을 요구한다"
            elif n_np == 0:
                assert need_np >= 1, f"{combo}: 가격 단독인데 통과 가능하다"
            else:
                assert 1 <= need_np <= n_np, combo


def test_relaxation_never_makes_the_gate_stricter(cfg):
    """결측이 늘어날수록 기준이 엄해지면 방향이 거꾸로다."""
    import itertools

    from btc_core.score import _requirements

    keys = list(cfg.bcs_families)
    full = [FamilyScore(k, k, cfg.bcs_families[k]["weight"], 0, 0.5) for k in keys]
    base_total, base_np, _ = _requirements(cfg, cfg.consensus, full)
    for r in range(1, len(keys)):
        for combo in itertools.combinations(keys, r):
            avail = [FamilyScore(k, k, cfg.bcs_families[k]["weight"], 0, 0.5) for k in combo]
            need, need_np, _ = _requirements(cfg, cfg.consensus, avail)
            assert need <= base_total, combo
            assert need_np <= base_np, combo
