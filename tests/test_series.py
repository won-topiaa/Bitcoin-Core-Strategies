"""시계열 도우미 — 이동평균과 결측 처리."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from btc_core.series import (
    Series,
    expanding_stdev,
    percent_change,
    ratio,
    resample_daily,
    sma,
)


def mkseries(values, start=date(2020, 1, 1)):
    return Series.from_pairs(
        [(start + timedelta(days=i), v) for i, v in enumerate(values)], name="t"
    )


def test_series_rejects_unsorted_or_duplicate_dates():
    with pytest.raises(ValueError):
        Series(dates=(date(2020, 1, 2), date(2020, 1, 1)), values=(1.0, 2.0))
    with pytest.raises(ValueError):
        Series(dates=(date(2020, 1, 1), date(2020, 1, 1)), values=(1.0, 2.0))


def test_from_pairs_sorts_and_keeps_last_duplicate():
    s = Series.from_pairs(
        [(date(2020, 1, 2), 2.0), (date(2020, 1, 1), 1.0), (date(2020, 1, 2), 9.0)]
    )
    assert s.dates == (date(2020, 1, 1), date(2020, 1, 2))
    assert s.values == (1.0, 9.0)


def test_sma_is_none_until_the_window_fills():
    s = mkseries([1, 2, 3, 4, 5])
    out = sma(s, 3)
    assert out.values[:2] == (None, None)
    assert out.values[2] == pytest.approx(2.0)
    assert out.values[4] == pytest.approx(4.0)


def test_sma_slides_correctly_over_a_long_run():
    s = mkseries(list(range(100)))
    out = sma(s, 10)
    # 마지막 10개는 90..99, 평균 94.5
    assert out.values[-1] == pytest.approx(94.5)
    assert out.values[50] == pytest.approx(sum(range(41, 51)) / 10)


def test_sma_of_a_constant_series_is_that_constant():
    s = mkseries([7.0] * 50)
    assert sma(s, 20).last == pytest.approx(7.0)


def test_sma_returns_none_when_too_many_values_are_missing():
    # 창 10 중 유효값 5개 → min_ratio 0.9 를 못 채운다
    s = mkseries([1, None, 2, None, 3, None, 4, None, 5, None])
    assert sma(s, 10).last is None


def test_sma_tolerates_a_small_gap():
    vals = [1.0] * 20
    vals[7] = None
    s = mkseries(vals)
    assert sma(s, 10).last == pytest.approx(1.0)


def test_sma_rejects_nonpositive_window():
    with pytest.raises(ValueError):
        sma(mkseries([1, 2, 3]), 0)


def test_expanding_stdev_matches_population_formula():
    import statistics

    vals = [float(v) for v in range(1, 61)]
    out = expanding_stdev(mkseries(vals), min_points=2)
    assert out.last == pytest.approx(statistics.pstdev(vals))


def test_expanding_stdev_waits_for_min_points():
    out = expanding_stdev(mkseries([1, 2, 3]), min_points=30)
    assert out.last is None


def test_ratio_guards_against_zero_denominator():
    a = mkseries([10.0, 20.0])
    b = mkseries([2.0, 0.0])
    out = ratio(a, b)
    assert out.values[0] == pytest.approx(5.0)
    assert out.values[1] is None


def test_resample_daily_forward_fills_gaps():
    s = Series.from_pairs([(date(2020, 1, 1), 1.0), (date(2020, 1, 5), 5.0)])
    out = resample_daily(s)
    assert len(out) == 5
    assert out.values == (1.0, 1.0, 1.0, 1.0, 5.0)


def test_gaps_reports_missing_days():
    s = Series.from_pairs([(date(2020, 1, 1), 1.0), (date(2020, 1, 6), 5.0)])
    assert s.gaps() == [(date(2020, 1, 1), 5)]


def test_value_on_forward_fills_from_the_past():
    s = Series.from_pairs([(date(2020, 1, 1), 1.0), (date(2020, 1, 5), 5.0)])
    assert s.value_on(date(2020, 1, 3)) == pytest.approx(1.0)
    assert s.value_on(date(2019, 1, 1)) is None


def test_percent_change():
    s = mkseries([100.0, 110.0, 121.0])
    assert percent_change(s, 1) == pytest.approx(10.0)
    assert percent_change(s, 2) == pytest.approx(21.0)
    assert percent_change(s, 5) is None


def test_diff_produces_first_differences():
    s = mkseries([10.0, 12.0, 15.0])
    assert s.diff().values == (None, pytest.approx(2.0), pytest.approx(3.0))
