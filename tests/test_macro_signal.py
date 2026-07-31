"""중국 M2 임펄스 자동 계산(datasources/macro.py) 검증.

핵심으로 지키는 것: (1) reference 미래 데이터를 절대 보지 않는다(과거 스냅샷
정직성), (2) 이력이 모자라면 None, (3) 가속/감속 부호가 맞다.
"""

from __future__ import annotations

import csv
from datetime import date

from btc_core.datasources.macro import (
    china_m2_impulse, load_macro_signals,
)


def _write(path, rows, header=("date", "m2_cn")):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _monthly(n, start=(2013, 1), growth=0.01, base=1000.0):
    """월 growth 비율로 자라는 월간 시계열 n개."""
    y, m = start
    lvl = base
    out = []
    for _ in range(n):
        out.append((date(y, m, 1).isoformat(), round(lvl, 4)))
        lvl *= (1 + growth)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def test_missing_file_or_column_returns_empty(tmp_path):
    assert load_macro_signals(tmp_path / "nope.csv") == {}
    p = tmp_path / "m.csv"
    _write(p, [("2020-01-01", "1")], header=("date", "sp500"))
    assert load_macro_signals(p) == {}


def test_insufficient_history_returns_none(tmp_path):
    # 24개월 미만이면 임펄스 계산 불가
    p = tmp_path / "m.csv"
    _write(p, _monthly(20))
    assert china_m2_impulse(p) is None


def test_impulse_is_zero_for_constant_growth(tmp_path):
    # 매월 같은 비율로 자라면 전년비가 일정 → 자기 평균과 같음 → 임펄스 ≈ 0
    p = tmp_path / "m.csv"
    _write(p, _monthly(60, growth=0.01))
    imp = china_m2_impulse(p)
    assert imp is not None and abs(imp) < 0.05


def test_acceleration_gives_positive_impulse(tmp_path):
    # 앞 절반은 완만, 뒤 절반은 급가속 → 최근 임펄스가 +
    p = tmp_path / "m.csv"
    slow = _monthly(40, start=(2013, 1), growth=0.005)
    y, m = 2016, 5
    lvl = float(slow[-1][1])
    fast = []
    for _ in range(20):
        lvl *= 1.02                     # 훨씬 빠른 성장
        fast.append((date(y, m, 1).isoformat(), round(lvl, 4)))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    _write(p, slow + fast)
    imp = china_m2_impulse(p)
    assert imp is not None and imp > 0.5


def test_no_future_reference(tmp_path):
    """reference 이후 데이터는 절대 안 본다 — 과거 스냅샷이 미래를 훔쳐보면 안 된다."""
    p = tmp_path / "m.csv"
    rows = _monthly(60, growth=0.005)
    # 마지막 12개월을 급가속으로 바꾼다 (날짜는 원래 격자 그대로, 값만 키운다)
    lvl = float(rows[47][1])
    for i in range(48, 60):
        lvl *= 1.05
        rows[i] = (rows[i][0], round(lvl, 4))
    _write(p, rows)
    # reference 를 급가속 전(2016-06)으로 잡으면, 그 뒤 급가속을 못 본다
    early = china_m2_impulse(p, reference=date(2016, 6, 30))
    late = china_m2_impulse(p, reference=date(2017, 12, 31))
    assert early is not None and late is not None
    assert late > early                 # 미래를 봤다면 early 도 커졌을 것
