"""정규화 계층 — 경계와 클램프."""

from __future__ import annotations

import pytest

from btc_core.normalize import (
    NormalizationError,
    blend,
    categorical,
    clamp,
    percentile_rank,
    piecewise,
)

ANCHORS = [[0.0, -1.0], [1.0, 0.0], [2.0, 1.0]]


def test_piecewise_hits_anchor_points_exactly():
    assert piecewise(0.0, ANCHORS) == pytest.approx(-1.0)
    assert piecewise(1.0, ANCHORS) == pytest.approx(0.0)
    assert piecewise(2.0, ANCHORS) == pytest.approx(1.0)


def test_piecewise_interpolates_linearly_within_a_segment():
    assert piecewise(0.5, ANCHORS) == pytest.approx(-0.5)
    assert piecewise(1.75, ANCHORS) == pytest.approx(0.75)


def test_piecewise_clamps_outside_the_anchor_range():
    assert piecewise(-99.0, ANCHORS) == pytest.approx(-1.0)
    assert piecewise(99.0, ANCHORS) == pytest.approx(1.0)


def test_piecewise_handles_uneven_segment_widths():
    # 앵커 간격이 다르면 기울기도 달라야 한다
    anchors = [[0.0, -1.0], [1.0, 0.0], [11.0, 1.0]]
    assert piecewise(0.5, anchors) == pytest.approx(-0.5)
    assert piecewise(6.0, anchors) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "bad, reason",
    [
        ([[1.0, 0.0]], "앵커 1개"),
        ([[1.0, 0.0], [0.0, 1.0]], "원시값 역순"),
        ([[0.0, 1.0], [1.0, -1.0]], "정규화값 감소"),
        ([[0.0, -2.0], [1.0, 1.0]], "범위 밖"),
        ([[0.0, 0.0, 0.0], [1.0, 1.0]], "쌍이 아님"),
    ],
)
def test_piecewise_rejects_malformed_anchors(bad, reason):
    with pytest.raises(NormalizationError):
        piecewise(0.5, bad, name=reason)


def test_categorical_is_case_and_space_insensitive():
    states = {"recovery": -1.0, "normal": 0.0}
    assert categorical("Recovery", states) == pytest.approx(-1.0)
    assert categorical("  normal ", states) == pytest.approx(0.0)


def test_categorical_rejects_unknown_state():
    with pytest.raises(NormalizationError):
        categorical("bogus", {"normal": 0.0})


def test_percentile_rank_maps_median_to_zero():
    history = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile_rank(5.5, history) == pytest.approx(0.0, abs=0.05)
    assert percentile_rank(0.0, history) == pytest.approx(-1.0, abs=0.11)
    assert percentile_rank(99.0, history) == pytest.approx(1.0, abs=0.01)


def test_percentile_rank_splits_ties_evenly():
    # 같은 값이 여러 개면 중간 순위로 센다
    assert percentile_rank(5, [5, 5, 5, 5]) == pytest.approx(0.0)


def test_percentile_rank_needs_history():
    with pytest.raises(NormalizationError):
        percentile_rank(1.0, [1.0])


def test_blend_respects_weight():
    assert blend(1.0, -1.0, 0.0) == pytest.approx(1.0)
    assert blend(1.0, -1.0, 1.0) == pytest.approx(-1.0)
    assert blend(1.0, -1.0, 0.5) == pytest.approx(0.0)


def test_blend_output_stays_in_range():
    assert -1.0 <= blend(1.0, 1.0, 0.35) <= 1.0
    assert clamp(5.0) == 1.0
    assert clamp(-5.0) == -1.0
