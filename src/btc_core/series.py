"""시계열 도우미. 표준 라이브러리만 쓴다 (pandas 의존 없음)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Iterator, Optional, Sequence


@dataclass(frozen=True)
class Series:
    """날짜 오름차순으로 정렬된 (date, value) 시계열.

    결측은 ``None`` 으로 들어올 수 있고, 이동평균 계산에서 제외된다.
    """

    dates: tuple[date, ...]
    values: tuple[Optional[float], ...]
    name: str = ""

    def __post_init__(self) -> None:
        if len(self.dates) != len(self.values):
            raise ValueError(f"{self.name}: dates 와 values 길이가 다릅니다.")
        if any(b <= a for a, b in zip(self.dates, self.dates[1:])):
            raise ValueError(f"{self.name}: 날짜가 오름차순 유일값이어야 합니다.")

    def __len__(self) -> int:
        return len(self.dates)

    def __iter__(self) -> Iterator[tuple[date, Optional[float]]]:
        return iter(zip(self.dates, self.values))

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[date, Optional[float]]], name: str = "") -> "Series":
        items = sorted(((d, v) for d, v in pairs), key=lambda p: p[0])
        deduped: list[tuple[date, Optional[float]]] = []
        for d, v in items:
            if deduped and deduped[-1][0] == d:
                deduped[-1] = (d, v)   # 같은 날짜는 마지막 값으로 덮어쓴다
            else:
                deduped.append((d, v))
        return cls(
            dates=tuple(d for d, _ in deduped),
            values=tuple(v for _, v in deduped),
            name=name,
        )

    @property
    def last_date(self) -> Optional[date]:
        return self.dates[-1] if self.dates else None

    @property
    def last(self) -> Optional[float]:
        for v in reversed(self.values):
            if v is not None:
                return v
        return None

    def value_on(self, target: date) -> Optional[float]:
        """해당 날짜의 값. 없으면 그 이전 가장 가까운 값(forward fill)."""
        best: Optional[float] = None
        for d, v in zip(self.dates, self.values):
            if d > target:
                break
            if v is not None:
                best = v
        return best

    def tail(self, n: int) -> "Series":
        if n <= 0:
            return Series((), (), self.name)
        return Series(self.dates[-n:], self.values[-n:], self.name)

    def slice_from(self, start: date) -> "Series":
        idx = 0
        for i, d in enumerate(self.dates):
            if d >= start:
                idx = i
                break
        else:
            idx = len(self.dates)
        return Series(self.dates[idx:], self.values[idx:], self.name)

    def slice_to(self, end: date) -> "Series":
        """end 이후를 잘라낸다. 특정 시점 기준으로 되돌려 계산할 때 쓴다."""
        idx = len(self.dates)
        for i, d in enumerate(self.dates):
            if d > end:
                idx = i
                break
        return Series(self.dates[:idx], self.values[:idx], self.name)

    def map_with(self, other: "Series", fn) -> "Series":
        """두 시계열을 날짜로 정렬해 결합한다. 한쪽이 없으면 그 날짜는 None."""
        lookup = dict(zip(other.dates, other.values))
        out: list[Optional[float]] = []
        for d, v in zip(self.dates, self.values):
            w = lookup.get(d)
            out.append(None if v is None or w is None else fn(v, w))
        return Series(self.dates, tuple(out), f"{self.name}~{other.name}")

    def diff(self) -> "Series":
        out: list[Optional[float]] = [None]
        for prev, cur in zip(self.values, self.values[1:]):
            out.append(None if prev is None or cur is None else cur - prev)
        return Series(self.dates, tuple(out), f"diff({self.name})")

    def gaps(self) -> list[tuple[date, int]]:
        """하루보다 큰 간격이 생긴 지점들. 데이터 품질 경고용."""
        found = []
        for a, b in zip(self.dates, self.dates[1:]):
            delta = (b - a).days
            if delta > 1:
                found.append((a, delta))
        return found


def sma(series: Series, window: int, *, min_ratio: float = 0.9) -> Series:
    """단순이동평균.

    창 안의 유효값이 ``window * min_ratio`` 에 못 미치면 그 날은 None.
    거래소 데이터에 며칠 구멍이 나도 조용히 왜곡되지 않게 하려는 장치다.
    """
    if window <= 0:
        raise ValueError("window 는 1 이상이어야 합니다.")
    need = max(1, math.ceil(window * min_ratio))
    out: list[Optional[float]] = []
    total = 0.0
    count = 0
    vals = series.values
    for i, v in enumerate(vals):
        if v is not None:
            total += v
            count += 1
        if i >= window:
            dropped = vals[i - window]
            if dropped is not None:
                total -= dropped
                count -= 1
        out.append(total / count if i >= window - 1 and count >= need else None)
    return Series(series.dates, tuple(out), f"sma{window}({series.name})")


def expanding_stdev(series: Series, *, min_points: int = 30) -> Series:
    """확장 창 모표준편차. MVRV Z-Score 의 분모에 쓴다.

    Welford 온라인 알고리즘 — 큰 값(시가총액 10^12 규모)에서도 수치적으로 안정적이다.
    """
    out: list[Optional[float]] = []
    n = 0
    mean = 0.0
    m2 = 0.0
    for v in series.values:
        if v is not None:
            n += 1
            delta = v - mean
            mean += delta / n
            m2 += delta * (v - mean)
        out.append(math.sqrt(m2 / n) if n >= min_points and m2 > 0 else None)
    return Series(series.dates, tuple(out), f"stdev({series.name})")


def ratio(numerator: Series, denominator: Series) -> Series:
    def _div(a: float, b: float) -> Optional[float]:
        return a / b if b else None

    lookup = dict(zip(denominator.dates, denominator.values))
    out: list[Optional[float]] = []
    for d, v in zip(numerator.dates, numerator.values):
        w = lookup.get(d)
        out.append(None if v is None or w in (None, 0) else _div(v, w))
    return Series(numerator.dates, tuple(out), f"{numerator.name}/{denominator.name}")


def resample_daily(series: Series) -> Series:
    """빠진 날짜를 직전 값으로 채워 일간 연속 시계열로 만든다.

    주간 데이터(200주 MA 계산용)나 결측이 있는 온체인 데이터를 이동평균에
    넣기 전에 통과시킨다.
    """
    if not series.dates:
        return series
    out_dates: list[date] = []
    out_values: list[Optional[float]] = []
    lookup = dict(zip(series.dates, series.values))
    cur = series.dates[0]
    end = series.dates[-1]
    carry: Optional[float] = None
    while cur <= end:
        v = lookup.get(cur)
        if v is not None:
            carry = v
        out_dates.append(cur)
        out_values.append(carry)
        cur += timedelta(days=1)
    return Series(tuple(out_dates), tuple(out_values), series.name)


def last_pair(series: Series) -> tuple[Optional[float], Optional[date]]:
    """마지막 비결측값과 **그 값이 실제로 속한 날짜**.

    ``Series.last`` 는 값만 주고 ``last_date`` 는 마지막 날짜를 주는데, 꼬리에
    결측이 있으면 그 둘이 서로 다른 날을 가리킨다. 두 시계열을 각각 ``.last``
    로 집어 나누면 **다른 날의 값이 섞이고**, 그런데도 as_of 는 오늘로 찍힌다.
    날짜를 값과 함께 돌려주어 그 어긋남을 없앤다.
    """
    for d, v in zip(reversed(series.dates), reversed(series.values)):
        if v is not None:
            return v, d
    return None, None


def history_of(series: Series, *, years: float = 4.0) -> list[float]:
    """최근 N년치 유효값 목록. 퍼센타일 정규화용."""
    if not series.dates:
        return []
    cutoff = series.dates[-1] - timedelta(days=int(years * 365.25))
    return [v for d, v in zip(series.dates, series.values) if v is not None and d >= cutoff]


def percent_change(series: Series, periods: int) -> Optional[float]:
    """마지막 값의 N기간 전 대비 변화율(%)."""
    vals: Sequence[Optional[float]] = series.values
    if len(vals) <= periods:
        return None
    cur = vals[-1]
    prev = vals[-1 - periods]
    if cur is None or prev in (None, 0):
        return None
    return (cur / prev - 1.0) * 100.0
