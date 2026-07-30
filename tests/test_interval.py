"""BCS 구간 — 점 하나가 아니라 범위로 위치를 말하기.

이 구간이 지켜야 하는 성질은 세 가지다.

1. **파라미터를 추가하지 않는다.** 폭은 데이터가 정한다(잭나이프).
2. **점을 포함한다.** low ≤ point ≤ high 가 깨지면 구간이 거짓말을 한다.
3. **표시 전용이다.** 구간이 실행 규칙을 바꾸면 새 자유도가 생긴다.
"""

from __future__ import annotations

import fixtures
import pytest

from btc_core.config import load_config
from btc_core.models import Reading
from btc_core.report import range_gauge
from btc_core.score import compute_bcs, compute_bcs_interval


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def readings_from(cfg, scores: dict[str, float]) -> dict[str, Reading]:
    """지표 키 → 점수를 그대로 Reading 으로. 정규화 경로를 우회해 구간만 본다."""
    out: dict[str, Reading] = {}
    for key, spec in cfg.indicators.items():
        s = scores.get(key)
        out[key] = Reading(
            key, spec.get("label", key), spec.get("family", ""),
            None if s is None else 1.0, s, spec.get("source", "manual"),
            note="" if s is not None else "결측",
        )
    return out


ALL_KEYS = ("mvrv_z", "nupl", "thermocap", "pi_cycle", "grm",
            "ma200w_mult", "ma2y_mult", "puell", "hash_ribbons")


# --- 기본 성질 --------------------------------------------------------------

def test_interval_contains_the_point(cfg):
    r = readings_from(cfg, {k: -0.6 for k in ALL_KEYS})
    iv = compute_bcs_interval(cfg, r)
    assert iv.low <= iv.point <= iv.high


def test_interval_is_a_point_when_every_indicator_agrees(cfg):
    """전부 같은 점수면 하나를 빼도 결과가 같다 — 폭이 0 이어야 한다."""
    r = readings_from(cfg, {k: -1.0 for k in ALL_KEYS})
    iv = compute_bcs_interval(cfg, r)
    assert iv.width == pytest.approx(0.0, abs=0.01)
    assert iv.stable


def test_interval_widens_when_one_indicator_disagrees(cfg):
    """한 지표만 반대 방향이면 그것을 빼는 순간 점수가 움직인다."""
    scores = {k: -0.8 for k in ALL_KEYS}
    scores["mvrv_z"] = +0.9
    iv = compute_bcs_interval(cfg, readings_from(cfg, scores))
    assert iv.width > 20


def test_max_abs_family_shows_up_as_interval_width(cfg):
    """밸류에이션이 max_abs 라 그 안의 극단값 하나가 40% 를 끌고 간다.

    그 지표를 빼면 차상위가 올라온다. 구간이 그 사실을 드러내야 한다 —
    이것이 구간을 만든 첫 번째 이유다.
    """
    scores = {k: 0.0 for k in ALL_KEYS}
    scores["mvrv_z"] = -1.0        # 혼자 극단
    scores["nupl"] = -0.1
    scores["thermocap"] = -0.1
    iv = compute_bcs_interval(cfg, readings_from(cfg, scores))
    assert iv.dropped_high == cfg.indicator("mvrv_z")["label"]
    assert iv.high > iv.point + 20


def test_dropping_labels_are_reported(cfg):
    scores = {k: -0.5 for k in ALL_KEYS}
    scores["puell"] = -1.0
    scores["hash_ribbons"] = +0.2
    iv = compute_bcs_interval(cfg, readings_from(cfg, scores))
    assert iv.dropped_low and iv.dropped_high
    assert iv.dropped_low != iv.dropped_high


def test_bands_of_both_ends_are_reported(cfg):
    scores = {k: 0.0 for k in ALL_KEYS}
    scores["mvrv_z"] = -1.0
    iv = compute_bcs_interval(cfg, readings_from(cfg, scores))
    assert iv.band_low == cfg.band_for(iv.low)["key"]
    assert iv.band_high == cfg.band_for(iv.high)["key"]
    assert iv.stable == (iv.band_low == iv.band_high)


def test_no_interval_when_bcs_cannot_be_computed(cfg):
    assert compute_bcs_interval(cfg, readings_from(cfg, {})) is None


def test_single_surviving_indicator_gives_a_zero_width_interval(cfg):
    """뺄 것이 없으면 구간이 아니라 점이다. 그걸 정직하게 표시한다."""
    iv = compute_bcs_interval(cfg, readings_from(cfg, {"puell": -0.7}))
    assert iv is not None
    assert iv.width == 0.0
    assert iv.n_variants == 0


def test_n_variants_counts_only_available_indicators(cfg):
    scores = {k: -0.4 for k in ALL_KEYS[:5]}
    iv = compute_bcs_interval(cfg, readings_from(cfg, scores))
    assert iv.n_variants == 5


def test_interval_matches_a_hand_rolled_jackknife(cfg):
    """구현이 정의와 같은지 — 손으로 돌린 잭나이프와 비교한다."""
    scores = {k: v for k, v in zip(ALL_KEYS, (-0.9, -0.2, -0.5, +0.3, -0.7,
                                              -0.6, -0.4, -1.0, +0.1))}
    r = readings_from(cfg, scores)
    iv = compute_bcs_interval(cfg, r)

    alts = []
    for key in ALL_KEYS:
        trimmed = dict(r)
        old = trimmed[key]
        trimmed[key] = Reading(old.key, old.label, old.family, None, None, old.source, "결측")
        alt, _, _ = compute_bcs(cfg, trimmed)
        alts.append(alt)
    point, _, _ = compute_bcs(cfg, r)
    assert iv.low == pytest.approx(round(min(alts + [point]), 2))
    assert iv.high == pytest.approx(round(max(alts + [point]), 2))


# --- 표시 전용이라는 약속 ---------------------------------------------------

def test_the_interval_never_changes_the_plan(cfg):
    """구간은 실행에 개입하지 않는다.

    개입하게 두면 "구간이 걸치면 보류" 같은 새 규칙이 생기고 그건 자유도를
    늘리는 일이다. 밴드·사다리·DCA 는 점수 하나로만 결정된다.
    """
    from btc_core.engine import evaluate
    from btc_core.datasources.base import DataBundle
    from btc_core.strategy import ExecutionState, build_plan

    bundle = DataBundle(market=fixtures.market_data(), origin="테스트")
    snap, _ = evaluate(cfg, bundle=bundle, record=False)
    assert snap.bcs_interval is not None

    rebuilt = build_plan(
        cfg, bcs=snap.bcs, lrs=snap.lrs, coverage=snap.coverage,
        families=snap.families, consensus=snap.consensus, price=snap.price,
        floor_prices={}, state=ExecutionState(), as_of=snap.as_of,
    )
    assert rebuilt.band_key == snap.plan.band_key
    assert rebuilt.dca_multiplier == snap.plan.dca_multiplier


def test_a_straddling_interval_is_surfaced_as_a_warning(cfg):
    """리포트를 안 보고 JSON 만 쓰는 경우에도 경고로 남아야 한다."""
    from btc_core.datasources.base import DataBundle
    from btc_core.engine import evaluate

    bundle = DataBundle(market=fixtures.market_data(), origin="테스트")
    snap, _ = evaluate(cfg, bundle=bundle, record=False)
    iv = snap.bcs_interval
    straddles = any("밴드 경계를 걸칩니다" in w for w in snap.warnings)
    assert straddles == (not iv.stable)


def test_snapshot_json_carries_the_interval(cfg):
    from btc_core.datasources.base import DataBundle
    from btc_core.engine import evaluate

    bundle = DataBundle(market=fixtures.market_data(), origin="테스트")
    snap, _ = evaluate(cfg, bundle=bundle, record=False)
    d = snap.as_dict()["bcs_interval"]
    assert d["low"] <= d["point"] <= d["high"]
    assert set(d) >= {"low", "high", "point", "width", "stable",
                      "band_low", "band_high", "n_variants"}


# --- 막대 그리기 ------------------------------------------------------------

def test_range_gauge_marks_both_ends_and_the_point():
    bar = range_gauge(-0.8, -0.4, -0.6)
    assert "├" in bar and "┤" in bar and "●" in bar
    assert bar.index("├") < bar.index("●") < bar.index("┤")


def test_range_gauge_keeps_the_zero_tick_when_the_interval_is_far_away():
    assert "┼" in range_gauge(-0.9, -0.7, -0.8)


def test_range_gauge_handles_a_reversed_interval():
    """low > high 로 들어와도 터지지 않는다."""
    assert "├" in range_gauge(0.5, 0.1, 0.3)


def test_range_gauge_is_all_dots_when_missing():
    assert set(range_gauge(None, None, None)) == {"·"}


def test_range_gauge_width_is_stable():
    from btc_core.report import BAR_WIDTH

    for lo, hi in ((-1.0, 1.0), (-0.05, 0.05), (0.99, 1.0), (-1.0, -0.99)):
        assert len(range_gauge(lo, hi, (lo + hi) / 2)) == BAR_WIDTH
