"""실행 전략 — 사다리, 히스테리시스, LRS 조정, 바닥선."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from btc_core.config import load_config
from btc_core.models import FamilyScore
from btc_core.strategy import (
    ExecutedStep,
    ExecutionState,
    build_floors,
    build_plan,
    commit_action,
    lrs_multiplier,
    next_ladder_step,
)

TODAY = date(2026, 7, 28)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def state_with_history(values, end=TODAY):
    """마지막 날이 end 가 되도록 연속 일자로 BCS 이력을 채운다."""
    st = ExecutionState()
    for i, v in enumerate(values):
        st.record_bcs(end - timedelta(days=len(values) - 1 - i), v)
    return st


def steady(value, days=10, end=TODAY):
    return state_with_history([value] * days, end)


# --- 밴드 ------------------------------------------------------------------

@pytest.mark.parametrize(
    "bcs, expected",
    [
        (95, "euphoria"), (70, "euphoria"),
        (69.9, "distribution"), (45, "distribution"),
        (44.9, "upper_neutral"), (20, "upper_neutral"),
        (19.9, "neutral"), (0, "neutral"), (-19.9, "neutral"),
        (-20, "accumulation"), (-44.9, "accumulation"),
        (-45, "deep_value"), (-69.9, "deep_value"),
        (-70, "capitulation"), (-100, "capitulation"),
    ],
)
def test_band_boundaries_are_contiguous(cfg, bcs, expected):
    assert cfg.band_for(bcs)["key"] == expected


def test_dca_multiplier_rises_as_the_market_gets_cheaper(cfg):
    dca = cfg.ladders["dca_multiplier"]
    order = ["euphoria", "distribution", "upper_neutral", "neutral",
             "accumulation", "deep_value", "capitulation"]
    vals = [dca[k] for k in order]
    assert vals == sorted(vals), "밴드가 싸질수록 적립 배수가 커져야 한다"
    assert dca["euphoria"] == 0.0


# --- 사다리 ----------------------------------------------------------------

def test_first_distribute_step_fires_at_its_trigger(cfg):
    st = steady(50.0)
    action = next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY)
    assert action is not None
    assert action.trigger == 45
    assert action.size_pct == pytest.approx(10.0)
    assert action.executable


def test_no_step_below_the_first_trigger(cfg):
    assert next_ladder_step(cfg, "distribute", 44.0, None, steady(44.0), TODAY) is None


def test_ladder_climbs_one_rung_at_a_time(cfg):
    """BCS 가 90 으로 튀어도 아직 안 밟은 가장 낮은 계단부터."""
    action = next_ladder_step(cfg, "distribute", 90.0, None, steady(90.0), TODAY)
    assert action.trigger == 45


def test_executed_steps_are_skipped(cfg):
    st = steady(60.0)
    st.steps.append(
        ExecutedStep("distribute", 45, TODAY - timedelta(days=30), 46.0, 10.0)
    )
    action = next_ladder_step(cfg, "distribute", 60.0, None, st, TODAY)
    assert action.trigger == 55


def test_accumulate_ladder_runs_downward(cfg):
    action = next_ladder_step(cfg, "accumulate", -50.0, None, steady(-50.0), TODAY)
    assert action.trigger == -30
    assert action.size_pct == pytest.approx(15.0)


def test_accumulate_does_not_fire_above_its_first_trigger(cfg):
    assert next_ladder_step(cfg, "accumulate", -29.0, None, steady(-29.0), TODAY) is None


def test_ladder_exhausts_after_the_last_rung(cfg):
    st = steady(90.0)
    for s in cfg.ladders["distribute"]["steps"]:
        st.steps.append(
            ExecutedStep("distribute", float(s["trigger_bcs"]),
                         TODAY - timedelta(days=200), 90.0, float(s["pct_of_holdings"]))
        )
    assert next_ladder_step(cfg, "distribute", 90.0, None, st, TODAY) is None


# --- 히스테리시스 ----------------------------------------------------------

def test_confirm_days_blocks_a_one_day_spike(cfg):
    """단 하루 임계를 넘은 것은 실행 근거가 못 된다."""
    st = state_with_history([10.0, 12.0, 50.0])
    action = next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY)
    assert not action.executable
    assert "확정 대기" in action.blocked_by


def test_three_consecutive_days_above_the_trigger_confirms(cfg):
    st = state_with_history([10.0, 46.0, 47.0, 48.0])
    action = next_ladder_step(cfg, "distribute", 48.0, None, st, TODAY)
    assert action.executable


def test_min_days_between_steps_is_enforced(cfg):
    st = steady(60.0)
    st.steps.append(ExecutedStep("distribute", 45, TODAY - timedelta(days=3), 46.0, 10.0))
    action = next_ladder_step(cfg, "distribute", 60.0, None, st, TODAY)
    assert action.trigger == 55
    assert not action.executable
    assert "간격 미충족" in action.blocked_by


def test_a_step_rearms_only_after_visiting_the_opposite_band(cfg):
    """분배 계단은 BCS 가 -20 이하까지 내려갔다 와야 다시 살아난다."""
    st = ExecutionState()
    st.steps.append(ExecutedStep("distribute", 45, TODAY - timedelta(days=200), 46.0, 10.0))
    for i, v in enumerate([46.0, -30.0, -30.0, 48.0, 49.0, 50.0]):
        st.record_bcs(TODAY - timedelta(days=5 - i), v)
    action = next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY)
    assert action.trigger == 45


def test_a_pullback_that_stays_positive_does_not_rearm(cfg):
    """이게 16년 실측이 잡아낸 결함이다.

    임계에서 몇 점 되돌린 것을 재무장으로 인정하면, BCS 의 일상적 변동만으로
    같은 계단이 2주마다 무한히 재발동한다.
    """
    st = ExecutionState()
    st.steps.append(ExecutedStep("distribute", 45, TODAY - timedelta(days=200), 46.0, 10.0))
    for i, v in enumerate([46.0, 30.0, 30.0, 48.0, 49.0, 50.0]):   # 양수 구간에 머묾
        st.record_bcs(TODAY - timedelta(days=5 - i), v)
    assert next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY) is None


def test_accumulate_rearms_only_after_visiting_the_upper_band(cfg):
    st = ExecutionState()
    st.steps.append(ExecutedStep("accumulate", -30, TODAY - timedelta(days=200), -32.0, 15.0))
    for i, v in enumerate([-32.0, -25.0, -28.0, -35.0]):           # 음수 구간에 머묾
        st.record_bcs(TODAY - timedelta(days=3 - i), v)
    assert next_ladder_step(cfg, "accumulate", -35.0, None, st, TODAY) is None

    for i, v in enumerate([25.0, 25.0, -35.0, -36.0, -37.0]):      # +20 이상 방문
        st.record_bcs(TODAY - timedelta(days=10 - i), v)
    action = next_ladder_step(cfg, "accumulate", -37.0, None, st, TODAY)
    assert action is not None and action.trigger == -30


def test_the_next_rung_still_fires_while_a_lower_one_stays_disarmed(cfg):
    st = ExecutionState()
    st.steps.append(ExecutedStep("distribute", 45, TODAY - timedelta(days=200), 46.0, 10.0))
    for i, v in enumerate([46.0, 42.0, 56.0, 57.0, 58.0]):
        st.record_bcs(TODAY - timedelta(days=4 - i), v)
    action = next_ladder_step(cfg, "distribute", 58.0, None, st, TODAY)
    assert action.trigger == 55


# --- 사이클 예산 한도 ------------------------------------------------------

def test_distribution_stops_at_the_core_holding(cfg):
    """설정 검증만으로는 부족하다 — 재무장이 겹치면 실행 합계가 한도를 넘는다.

    16년 실측에서 2017 사이클 분배 누적이 70%까지 가서 코어 40% 를 침범했다.
    """
    core = float(cfg.ladders["distribute"]["core_hold_pct"])
    cap = 100.0 - core
    st = steady(90.0, days=400)
    # 이번 사이클에 이미 한도만큼 팔았다고 기록 (반대편 방문 없음)
    st.steps.append(ExecutedStep("distribute", 85, TODAY - timedelta(days=30), 90.0, cap))

    action = next_ladder_step(cfg, "distribute", 90.0, None, st, TODAY)
    assert action is not None
    assert not action.executable
    assert "한도 소진" in action.blocked_by
    assert "코어 지분 보호" in action.blocked_by


def test_the_budget_resets_after_the_opposite_band(cfg):
    cap = 100.0 - float(cfg.ladders["distribute"]["core_hold_pct"])
    st = ExecutionState()
    st.steps.append(ExecutedStep("distribute", 85, TODAY - timedelta(days=200), 90.0, cap))
    # 그 뒤로 BCS 가 저평가 구간까지 내려갔다 돌아왔다 → 새 사이클
    for i, v in enumerate([-40.0, -40.0, 88.0, 89.0, 90.0]):
        st.record_bcs(TODAY - timedelta(days=4 - i), v)
    action = next_ladder_step(cfg, "distribute", 90.0, None, st, TODAY)
    assert action is not None and action.executable


def test_accumulation_stops_at_a_full_reserve(cfg):
    st = steady(-90.0, days=400)
    st.steps.append(ExecutedStep("accumulate", -82, TODAY - timedelta(days=30), -90.0, 100.0))
    action = next_ladder_step(cfg, "accumulate", -90.0, None, st, TODAY)
    assert action is not None
    assert not action.executable
    assert "한도 소진" in action.blocked_by


# --- LRS 조정 --------------------------------------------------------------

def test_easing_liquidity_slows_distribution(cfg):
    assert lrs_multiplier(cfg, "distribute", 60.0) == pytest.approx(0.75)


def test_tightening_liquidity_accelerates_distribution(cfg):
    assert lrs_multiplier(cfg, "distribute", -60.0) == pytest.approx(1.25)


def test_easing_liquidity_accelerates_accumulation(cfg):
    assert lrs_multiplier(cfg, "accumulate", 60.0) == pytest.approx(1.25)


def test_moderate_liquidity_leaves_size_untouched(cfg):
    assert lrs_multiplier(cfg, "distribute", 10.0) == pytest.approx(1.0)
    assert lrs_multiplier(cfg, "accumulate", None) == pytest.approx(1.0)


def test_lrs_scales_tranche_size_but_never_flips_direction(cfg):
    st = steady(50.0)
    base = next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY)
    eased = next_ladder_step(cfg, "distribute", 50.0, 60.0, st, TODAY)
    assert eased.size_pct == pytest.approx(base.size_pct * 0.75)
    assert eased.kind == base.kind == "distribute"
    assert eased.size_pct > 0


# --- 바닥선 ----------------------------------------------------------------

def test_reference_floor_is_the_weighted_average_of_the_lines(cfg):
    levels, ref, zones = build_floors(
        cfg, 100_000.0, {"ma200w": 50_000, "lth_rp": 60_000, "cvdd": 40_000}
    )
    assert ref == pytest.approx(0.35 * 50_000 + 0.35 * 60_000 + 0.30 * 40_000)
    assert len(levels) == 3
    assert len(zones) == 5


def test_floor_weights_renormalize_when_a_line_is_missing(cfg):
    _, ref, _ = build_floors(cfg, 100_000.0, {"ma200w": 50_000, "lth_rp": None, "cvdd": None})
    assert ref == pytest.approx(50_000.0)


def test_no_reference_floor_without_any_line(cfg):
    levels, ref, zones = build_floors(cfg, 100_000.0, {})
    assert ref is None
    assert zones == ()
    assert all(l.price is None for l in levels)


def test_buy_zones_report_distance_and_reached_state(cfg):
    _, ref, zones = build_floors(cfg, 60_000.0, {"ma200w": 50_000, "lth_rp": 50_000, "cvdd": 50_000})
    assert ref == pytest.approx(50_000.0)
    first = zones[0]                       # 기준 바닥 +30% = 65,000
    assert first.price == pytest.approx(65_000.0)
    assert first.reached is True           # 현재가 60,000 이 이미 그 아래
    assert first.distance_pct == pytest.approx(8.3, abs=0.1)

    last = zones[-1]                       # 기준 바닥 -25% = 37,500
    assert last.reached is False
    assert last.distance_pct < 0


def test_buy_zone_allocations_sum_to_the_whole_reserve(cfg):
    total = sum(z["pct_of_reserve"] for z in cfg.floors["buy_zones"])
    assert total == pytest.approx(100.0)


# --- 계획 조립 -------------------------------------------------------------

def fam(key, label, weight, score):
    return FamilyScore(key, label, weight, weight, score)


def test_plan_blocks_the_ladder_when_consensus_fails(cfg):
    from btc_core.models import Consensus

    plan = build_plan(
        cfg, bcs=50.0, lrs=None, coverage=1.0,
        families=[fam("price", "가격", 25, 0.9)],
        consensus=Consensus(False, "overheated", ("price",), (), "가격 계열만 — 관망"),
        price=100_000.0, floor_prices={}, state=steady(50.0), as_of=TODAY,
    )
    ladder = [a for a in plan.actions if a.kind == "distribute"]
    assert ladder and not ladder[0].executable
    assert "가격 계열만" in ladder[0].blocked_by


def test_plan_blocks_the_ladder_on_low_coverage(cfg):
    from btc_core.models import Consensus

    plan = build_plan(
        cfg, bcs=50.0, lrs=None, coverage=0.25,
        families=[fam("price", "가격", 25, 0.9)],
        consensus=Consensus(True, "overheated", ("a", "b", "c"), ("a", "b"), "ok"),
        price=100_000.0, floor_prices={}, state=steady(50.0), as_of=TODAY,
    )
    ladder = [a for a in plan.actions if a.kind == "distribute"]
    assert ladder and not ladder[0].executable
    assert "커버리지" in ladder[0].blocked_by


def test_dca_survives_a_failed_consensus(cfg):
    """사다리는 추가 판단이고, DCA 는 판단을 미루는 장치다. 게이트와 무관하다."""
    from btc_core.models import Consensus

    plan = build_plan(
        cfg, bcs=-30.0, lrs=None, coverage=1.0,
        families=[fam("price", "가격", 25, -0.9)],
        consensus=Consensus(False, "undervalued", (), (), "합의 미달"),
        price=50_000.0, floor_prices={}, state=steady(-30.0), as_of=TODAY,
    )
    dca = [a for a in plan.actions if a.kind == "dca"]
    assert dca and dca[0].executable
    assert plan.dca_multiplier == pytest.approx(1.5)
    assert plan.band_key == "accumulation"


def test_plan_notes_a_conflict_between_families(cfg):
    plan = build_plan(
        cfg, bcs=5.0, lrs=None, coverage=1.0,
        families=[fam("valuation", "밸류에이션", 30, 0.8), fam("supply", "공급", 20, -0.8)],
        consensus=None, price=50_000.0, floor_prices={},
        state=steady(5.0), as_of=TODAY,
    )
    assert any("계열 충돌" in n for n in plan.notes)


def test_plan_without_a_score_refuses_to_act(cfg):
    plan = build_plan(
        cfg, bcs=None, lrs=None, coverage=0.0, families=[], consensus=None,
        price=None, floor_prices={}, state=ExecutionState(), as_of=TODAY,
    )
    assert plan.band_key == "unknown"
    assert all(not a.executable for a in plan.actions)


# --- 상태 저장 -------------------------------------------------------------

def test_commit_records_a_step_and_accumulates(cfg, tmp_path):
    st = steady(50.0)
    action = next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY)
    commit_action(st, action, 50.0, on=TODAY, note="테스트")
    assert st.cumulative("distribute") == pytest.approx(10.0)
    assert st.last_execution_date() == TODAY


def test_commit_refuses_a_blocked_action(cfg):
    st = state_with_history([10.0, 12.0, 50.0])
    action = next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY)
    with pytest.raises(ValueError):
        commit_action(st, action, 50.0, on=TODAY)


def test_commit_refuses_a_non_ladder_action(cfg):
    from btc_core.models import Action

    with pytest.raises(ValueError):
        commit_action(ExecutionState(), Action("dca", "DCA", 100.0, None), 0.0)


def test_state_survives_a_save_load_round_trip(cfg, tmp_path):
    st = steady(50.0)
    action = next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY)
    commit_action(st, action, 50.0, on=TODAY, note="왕복 검증")
    path = tmp_path / "state.json"
    st.save(path)

    back = ExecutionState.load(path)
    assert back.cumulative("distribute") == pytest.approx(10.0)
    assert back.steps[0].note == "왕복 검증"
    assert back.steps[0].executed_on == TODAY
    assert len(back.bcs_history) == len(st.bcs_history)


def test_loading_a_missing_state_file_gives_an_empty_state(tmp_path):
    st = ExecutionState.load(tmp_path / "nope.json")
    assert st.steps == []
    assert st.bcs_history == []


def test_recording_the_same_day_twice_overwrites(cfg):
    st = ExecutionState()
    st.record_bcs(TODAY, 10.0)
    st.record_bcs(TODAY, 20.0)
    assert st.bcs_history == [(TODAY, 20.0)]


# --- 저점 프로파일 ---------------------------------------------------------

def bottom_values(**over):
    """2018-12-15 저점의 실측값. 모든 조건을 만족한다."""
    base = dict(drawdown_pct=-83.8, days_since_ath=364.0, mvrv=0.69,
                nupl=-0.448, puell=0.39, ma200w_mult=1.00)
    base.update(over)
    return base


def test_bottom_profile_has_no_mathematically_equivalent_conditions(cfg):
    """MVRV < 1 과 NUPL < 0 은 완전히 같은 조건이다 (NUPL = 1 - 1/MVRV).

    둘 다 넣으면 하나의 사실이 두 표를 행사한다. 이 저장소의 최상위 원칙과
    정면으로 충돌하므로 하나만 남아 있어야 한다.
    """
    keys = {c["key"] for c in cfg.bottom_profile["conditions"]}
    assert not ({"mvrv", "nupl"} <= keys), "MVRV<1 과 NUPL<0 이 함께 들어 있습니다"


def test_historical_bottoms_match_every_condition(cfg):
    """2015/2018/2022 저점 실측값이 6/6 이어야 한다.

    하나라도 안 맞으면 그 조건은 저점의 공통점이 아니므로 조건 자체가 틀린 것이다.
    """
    from btc_core.strategy import build_bottom_profile

    observed = [
        dict(drawdown_pct=-84.5, days_since_ath=406.0, mvrv=0.56,
             nupl=-0.774, puell=0.31, ma200w_mult=0.91),   # 2015-01-14
        dict(drawdown_pct=-83.8, days_since_ath=364.0, mvrv=0.69,
             nupl=-0.448, puell=0.39, ma200w_mult=1.00),   # 2018-12-15
        dict(drawdown_pct=-76.6, days_since_ath=378.0, mvrv=0.78,
             nupl=-0.286, puell=0.43, ma200w_mult=0.66),   # 2022-11-21
    ]
    for vals in observed:
        profile = build_bottom_profile(cfg, vals)
        assert profile.hits == profile.total, vals
        assert "바닥권" in profile.label


def test_a_bull_market_matches_almost_nothing(cfg):
    from btc_core.strategy import build_bottom_profile

    # 2021-11-08 고점 부근의 값
    profile = build_bottom_profile(
        cfg,
        dict(drawdown_pct=-0.5, days_since_ath=2.0, mvrv=2.6,
             nupl=0.650, puell=1.47, ma200w_mult=3.97),
    )
    assert profile.hits == 0


def test_partial_match_is_labelled_accordingly(cfg):
    from btc_core.strategy import build_bottom_profile

    profile = build_bottom_profile(cfg, bottom_values(drawdown_pct=-40.0, puell=0.9))
    assert profile.hits == profile.total - 2
    assert profile.label and "바닥권" not in profile.label


def test_missing_inputs_leave_the_profile_unevaluable(cfg):
    from btc_core.strategy import build_bottom_profile

    profile = build_bottom_profile(cfg, {})
    assert not profile.evaluable
    assert profile.hits == 0


def test_the_profile_never_touches_the_score(cfg):
    """저점 프로파일은 표시 전용이다. BCS 도 사다리도 바꾸면 안 된다."""
    from btc_core.models import Consensus

    args = dict(
        cfg=cfg, bcs=-50.0, lrs=None, coverage=1.0,
        families=[fam("valuation", "밸류에이션", 30, -0.9)],
        consensus=Consensus(True, "undervalued", ("a", "b", "c"), ("a", "b"), "ok"),
        price=50_000.0, floor_prices={}, state=steady(-50.0), as_of=TODAY,
    )
    without = build_plan(**args)
    with_profile = build_plan(**args, bottom_inputs=bottom_values())

    assert with_profile.bottom_profile.hits == with_profile.bottom_profile.total
    assert without.bottom_profile.hits == 0
    # 실행 계획은 동일해야 한다
    assert [a.as_dict() for a in without.actions] == [a.as_dict() for a in with_profile.actions]
    assert without.dca_multiplier == with_profile.dca_multiplier
    assert without.band_key == with_profile.band_key


def test_profile_note_appears_only_when_cheap(cfg):
    """비쌀 때 저점 프로파일을 메모로 띄우는 건 소음이다."""
    args = dict(
        cfg=cfg, lrs=None, coverage=1.0, families=[], consensus=None,
        price=50_000.0, floor_prices={}, as_of=TODAY,
        bottom_inputs=bottom_values(),
    )
    cheap = build_plan(bcs=-50.0, state=steady(-50.0), **args)
    rich = build_plan(bcs=50.0, state=steady(50.0), **args)
    assert any("저점 프로파일" in n for n in cheap.notes)
    assert not any("저점 프로파일" in n for n in rich.notes)


# --- 정독 2회차로 찾은 결함들 ----------------------------------------------

def test_core_holding_note_uses_the_same_cycle_boundary_as_the_budget(cfg):
    """한도는 사이클 기준인데 화면 숫자만 전 기간 누적이면 약속이 깨진 것처럼 읽힌다.

    이전 사이클에서 60% 를 다 팔았어도, BCS 가 반대편 구간을 다녀왔으면
    이번 사이클 분배는 0% 다. 그런데 메모는 "누적 분배 60%" 라고 찍었다.
    """
    st = ExecutionState()
    old = TODAY - timedelta(days=900)
    # 지난 사이클에 한도를 전부 소진
    st.steps.append(ExecutedStep("distribute", 45.0, old, 46.0, 60.0))
    # 그 뒤 BCS 가 반대편(-20 이하)까지 다녀왔다 → 새 사이클
    st.record_bcs(old, 46.0)
    for i, v in enumerate([-55.0] * 5):
        st.record_bcs(TODAY - timedelta(days=400 - i), v)
    for i in range(10):
        st.record_bcs(TODAY - timedelta(days=9 - i), 50.0)

    plan = build_plan(
        cfg=cfg, bcs=50.0, lrs=None, coverage=1.0, families=[], consensus=None,
        price=100_000.0, floor_prices={}, state=st, as_of=TODAY,
    )
    core_note = next(n for n in plan.notes if "코어 보유" in n)
    assert "이번 사이클" in core_note
    assert "분배 0%" in core_note

    # 한도도 같은 경계를 쓰므로 새 계단이 실제로 열려야 한다
    step = next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY)
    assert step is not None
    assert "한도 소진" not in (step.blocked_by or "")


def test_buy_zone_distance_is_unknown_rather_than_zero_without_a_price(cfg):
    """현재가를 모를 때 거리 0.0% 는 '지금 가격과 같다'로 읽힌다 — 정반대 의미다."""
    floors = {"ma200w": 60_000.0, "lth_rp": 55_000.0, "cvdd": 40_000.0}
    _, ref, zones = build_floors(cfg, None, floors)
    assert ref is not None and zones
    assert all(z.distance_pct is None for z in zones)
    assert all(not z.reached for z in zones)

    _, _, priced = build_floors(cfg, 100_000.0, floors)
    assert all(z.distance_pct is not None and z.distance_pct < 0 for z in priced)


def test_band_selection_refuses_a_nan_score(cfg):
    """조용히 첫 밴드를 돌려주면 그건 '항복'(전량 매수) 밴드다 — 최악의 오답."""
    from btc_core.config import ConfigError

    with pytest.raises(ConfigError):
        cfg.band_for(float("nan"))
