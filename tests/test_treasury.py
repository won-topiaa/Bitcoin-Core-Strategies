"""트레저리 이벤트 검정 로직 검증 — 답을 심어 두고 도구가 되찾는지 본다.

전부 합성 데이터·고정 시드다(오프라인·결정론). 실데이터 수치는 docs/34 에 있고,
여기서는 **기계가 맞는지**만 본다: 심은 효과의 회수, 효과 없는 널의 큰 p,
이벤트 목록의 형식, 표본 밖 날짜의 조용한 건너뛰기, 창 산술의 정확성.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import treasury_study as ts

D0 = date(2020, 1, 1)


def daily_walk(n: int, seed: int = 3, sigma: float = 0.01) -> dict[date, float]:
    """기하 랜덤워크 n일. sigma 는 일간 로그수익률 표준편차."""
    rng = random.Random(seed)
    out, lvl = {}, 100.0
    for k in range(n):
        lvl *= math.exp(rng.gauss(0, sigma))
        out[D0 + timedelta(days=k)] = lvl
    return out


def plant(price: dict[date, float], days: list[date], jump: float) -> dict[date, float]:
    """지정한 날 **이후의 모든 가격**을 e^jump 배 — 그 날 하루의 수익률만 커진다."""
    out = dict(price)
    for d in days:
        for k in sorted(out):
            if k >= d:
                out[k] *= math.exp(jump)
    return out


def make_events(days: list[date], kind: str = "surprise"):
    return [(d.isoformat(), f"합성 이벤트 {i}", "company", kind, "테스트")
            for i, d in enumerate(days)]


# ---------------------------------------------------------------------------
# 이벤트 목록의 형식 — 실측 목록 자체가 계약이다
# ---------------------------------------------------------------------------
def test_event_list_shape_and_kinds():
    assert 15 <= len(ts.EVENTS) <= 25
    for dstr, name, cls, kind, note in ts.EVENTS:
        d = date.fromisoformat(dstr)                 # 날짜가 전부 파싱돼야 한다
        assert date(2020, 1, 1) <= d <= date(2026, 12, 31)
        assert kind in ts.KINDS
        assert cls in ("company", "country")
        assert name and note
    # 두 kind 모두 실제로 존재해야 검정이 성립한다 (분리가 이 검정의 핵심)
    kinds = {e[3] for e in ts.EVENTS}
    assert kinds == set(ts.KINDS)
    # 국가 이벤트(엘살바도르)가 별도 class 로 표시돼 있어야 한다
    assert any(e[2] == "country" for e in ts.EVENTS)


# ---------------------------------------------------------------------------
# 창 산술 — 매일 정확히 +1% 인 가격이면 창 값은 자명하다
# ---------------------------------------------------------------------------
def test_window_arithmetic_exact():
    price = {D0 + timedelta(days=k): 100.0 * math.exp(0.01 * k) for k in range(200)}
    ev = make_events([D0 + timedelta(days=100)])
    res = ts.analyse(price, None, events=ev, start=D0)
    (r,) = res["rows"]
    d0, w11, w05 = r["raw"]
    assert abs(d0 - 1.0) < 1e-9          # 당일 [0] = 1%
    assert abs(w11 - 3.0) < 1e-9         # [-1..+1] = 3일 = 3%
    assert abs(w05 - 6.0) < 1e-9         # [0..+5] = 6일 = 6%
    assert abs(r["pre60"] - 60.0) < 1e-9  # 전 60일 = 60%


def test_excess_subtracts_nasdaq():
    """BTC 와 나스닥이 같은 날 같은 만큼 뛰면 초과수익은 0 이어야 한다."""
    days = [D0 + timedelta(days=k) for k in range(200)]
    flat = {d: 100.0 for d in days}
    t = days[100]
    btc = plant(flat, [t], 0.05)
    ndq = plant({d: 500.0 for d in days}, [t], 0.05)
    res = ts.analyse(btc, ndq, events=make_events([t]), start=D0)
    (r,) = res["rows"]
    assert abs(r["raw"][0] - 5.0) < 1e-9
    assert abs(r["exc"][0]) < 1e-9


# ---------------------------------------------------------------------------
# 심은 효과의 회수 / 효과 없는 널
# ---------------------------------------------------------------------------
def _perm_key(sub: str, wi: int = 0, fam: str = "raw"):
    return (sub, ts.WINDOWS[wi][0], fam)


def test_planted_effect_recovered():
    walk = daily_walk(1100, seed=3)
    ev_days = [D0 + timedelta(days=90 + 80 * i) for i in range(12)]
    price = plant(walk, ev_days, 0.08)               # 이벤트일마다 +8%p 를 심는다
    res = ts.analyse(price, None, events=make_events(ev_days), start=D0)
    pt = ts.perm_test(res, n=400, seed=11)
    obs = pt["obs"][_perm_key("all")]
    assert 6.0 < obs < 10.0                          # 심은 8%p 근처를 회수
    assert pt["p"][_perm_key("all")] < 0.05          # 원형 이동 널에서 유의


def test_null_has_large_p():
    walk = daily_walk(1100, seed=5)
    ev_days = [D0 + timedelta(days=90 + 80 * i) for i in range(12)]
    res = ts.analyse(walk, None, events=make_events(ev_days), start=D0)
    pt = ts.perm_test(res, n=400, seed=11)
    assert abs(pt["obs"][_perm_key("all")]) < 1.0    # 효과 없음 → 평균 ~0
    assert pt["p"][_perm_key("all")] > 0.15          # 유의하지 않아야 한다


# ---------------------------------------------------------------------------
# 표본 밖 날짜 — 조용히 건너뛴다 (죽거나 지어내지 않는다)
# ---------------------------------------------------------------------------
def test_out_of_sample_dates_skipped():
    price = daily_walk(400, seed=7)
    inside = D0 + timedelta(days=200)
    ev = make_events([inside]) + [
        ("1999-01-01", "표본 이전", "company", "surprise", "건너뛰어야 함"),
        ("2035-01-01", "표본 이후", "company", "flagged", "건너뛰어야 함"),
    ]
    res = ts.analyse(price, None, events=ev, start=D0)
    assert len(res["rows"]) == 1
    assert res["rows"][0]["date"] == inside


# ---------------------------------------------------------------------------
# 요일 검정 — 시대에만 심은 월요일 효과를 시대에서만 찾아야 한다
# ---------------------------------------------------------------------------
def test_weekday_effect_only_in_era():
    days = [D0 + timedelta(days=k) for k in range(900)]
    era_start = D0 + timedelta(days=450)
    rng = random.Random(9)
    out, lvl = {}, 100.0
    for d in days:
        r = rng.gauss(0, 0.004)
        if d.weekday() == 0 and d >= era_start:      # 시대의 월요일에만 +2%p
            r += 0.02
        lvl *= math.exp(r)
        out[d] = lvl
    wd = ts.weekday_study(out, era_start=era_start, era_end=days[-1], n=400, seed=13)
    assert wd["era"] is not None and wd["pre"] is not None
    assert wd["era"]["diff"] > 1.5                   # 심은 2%p 근처
    assert wd["era"]["p"] < 0.05                     # 시대에서는 유의
    assert abs(wd["pre"]["diff"]) < 0.5              # 직전 구간엔 효과 없음
    assert wd["pre"]["p"] > 0.15
