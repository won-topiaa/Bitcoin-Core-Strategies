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


def test_a_step_rearms_after_a_deep_enough_pullback(cfg):
    """45 를 밟은 뒤 37(=45-8) 이하로 되돌았다면 다시 밟을 수 있다."""
    st = ExecutionState()
    st.steps.append(ExecutedStep("distribute", 45, TODAY - timedelta(days=60), 46.0, 10.0))
    for i, v in enumerate([46.0, 30.0, 30.0, 48.0, 49.0, 50.0]):
        st.record_bcs(TODAY - timedelta(days=5 - i), v)
    action = next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY)
    assert action.trigger == 45


def test_a_shallow_pullback_does_not_rearm(cfg):
    """45 를 밟고 42 까지만 되돌았다면(45−8=37 미달) 그 계단은 죽어 있다."""
    st = ExecutionState()
    st.steps.append(ExecutedStep("distribute", 45, TODAY - timedelta(days=60), 46.0, 10.0))
    for i, v in enumerate([46.0, 42.0, 42.0, 48.0, 49.0, 50.0]):   # 42 > 45-8
        st.record_bcs(TODAY - timedelta(days=5 - i), v)
    # 45 는 재무장 안 됐고 55 는 아직 도달 전 → 밟을 계단이 없다
    assert next_ladder_step(cfg, "distribute", 50.0, None, st, TODAY) is None


def test_the_next_rung_still_fires_while_a_lower_one_stays_disarmed(cfg):
    st = ExecutionState()
    st.steps.append(ExecutedStep("distribute", 45, TODAY - timedelta(days=60), 46.0, 10.0))
    for i, v in enumerate([46.0, 42.0, 56.0, 57.0, 58.0]):
        st.record_bcs(TODAY - timedelta(days=4 - i), v)
    action = next_ladder_step(cfg, "distribute", 58.0, None, st, TODAY)
    assert action.trigger == 55


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
