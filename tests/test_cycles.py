"""사이클 전환점 — 고른 날짜가 아니라 잰 날짜인지 지킨다.

전수 점검에서 나온 문제가 이 파일의 이유다. 전환점 날짜가 도구 다섯 곳에
각각 하드코딩돼 있었고 하나가 나머지와 달랐으며, **손으로 고른 마지막 저점이
잰 저점보다 결과를 좋아 보이게 했다** (저점 σ 8.7 vs 10.8, 빈 공간 124 vs 118).

그래서 날짜를 데이터에서 뽑기로 했고, 이 테스트가 그 약속을 지킨다.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from btc_core.cycles import (
    MEASURED_BOTTOMS,
    MEASURED_TOPS,
    bottoms,
    find_turns,
    tops,
)
from btc_core.series import Series

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "market.csv"


@pytest.fixture(scope="module")
def price():
    if not CSV.exists():
        pytest.skip("data/market.csv 없음")
    from btc_core.datasources.csv_source import load_csv_bundle

    return load_csv_bundle(CSV).market.price


def test_hardcoded_dates_match_what_the_data_says(price):
    """적어 둔 날짜와 잰 날짜가 같아야 한다.

    데이터를 갱신했는데 이 테스트가 깨지면, 새 극값이 생겼다는 뜻이므로
    cycles.py 의 MEASURED_* 를 갱신해야 한다. **조용히 어긋나면 안 된다** —
    전환점은 이 시스템의 성적표 그 자체다.
    """
    turns = find_turns(price)
    assert tuple(t.when for t in tops(turns)) == MEASURED_TOPS
    assert tuple(t.when for t in bottoms(turns)) == MEASURED_BOTTOMS


def test_each_turn_really_is_the_extreme_close(price):
    """뽑힌 날이 자기 구간에서 진짜 최고/최저인지 직접 확인한다."""
    px = {d: v for d, v in zip(price.dates, price.values) if v}
    turns = find_turns(price)
    for a, b in zip(turns, turns[1:]):
        span = {d: v for d, v in px.items() if a.when <= d <= b.when}
        if a.phase == "top":
            assert px[a.when] == max(span.values()), f"{a.when} 이 최고가 아니다"
        else:
            assert px[a.when] == min(span.values()), f"{a.when} 이 최저가 아니다"


@pytest.mark.parametrize("window", [600, 700, 800, 900, 1000])
def test_the_search_window_does_not_pick_the_answer(price, window):
    """창을 넓혀도 고점 날짜가 안 바뀌어야 한다.

    창이 답을 만들면 그것도 사람이 고른 것이나 마찬가지다. 실측 고점은
    반감기 이후 367~548일에 나왔으므로 600일 이상이면 어느 창을 써도 같아야
    한다.
    """
    got = tuple(t.when for t in tops(find_turns(price, top_window=window)))
    assert got == MEASURED_TOPS


def test_the_current_cycle_bottom_is_marked_provisional(price):
    """사이클이 안 끝났으면 그 저점은 확정이 아니다."""
    last = bottoms(find_turns(price))[-1]
    assert last.provisional, "진행 중인 사이클의 저점은 잠정이어야 한다"


def test_tops_and_bottoms_alternate(price):
    turns = find_turns(price)
    phases = [t.phase for t in turns]
    assert phases == ["top", "bottom"] * (len(phases) // 2)


def test_empty_price_gives_no_turns():
    assert find_turns(Series((), (), "price")) == []


def test_short_series_does_not_invent_turns():
    """반감기 창을 못 덮는 짧은 데이터에서 억지로 극값을 뽑지 않는다."""
    start = date(2024, 4, 20)
    days = tuple(start + timedelta(days=i) for i in range(30))
    s = Series(days, tuple(50_000.0 + i for i in range(30)), "price")
    turns = find_turns(s)
    # 뽑히더라도 전부 잠정이어야 한다 — 창이 데이터 끝을 넘기 때문이다.
    assert all(t.provisional for t in turns)


def test_no_tool_hardcodes_its_own_turning_points():
    """도구가 자기만의 전환점 표를 다시 만들지 않는다.

    다섯 곳에 흩어져 있던 것이 서로 어긋나서 이 모듈이 생겼다. 되돌아가지
    않게 막는다.
    """
    import re

    offenders = []
    for f in sorted((ROOT / "tools").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        if "from btc_core.cycles import" in src or "btc_core.cycles" in src:
            continue
        # 알려진 전환점 연도의 날짜 리터럴이 2개 이상이면 자체 표를 든 것이다
        hits = re.findall(r"date\((2013|2015|2017|2018|2021|2022|2025|2026),\s*\d+,\s*\d+\)",
                          src)
        if len(hits) >= 4:
            offenders.append(f.name)
    assert not offenders, (
        f"전환점을 자체 하드코딩한 도구: {offenders} — btc_core.cycles 를 쓰세요"
    )
