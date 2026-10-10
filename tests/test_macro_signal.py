"""중국 M2 임펄스 자동 계산(datasources/macro.py) 검증.

핵심으로 지키는 것: (1) reference 미래 데이터를 절대 보지 않는다(과거 스냅샷
정직성), (2) 이력이 모자라면 None, (3) 가속/감속 부호가 맞다.
"""

from __future__ import annotations

import csv
from datetime import date

from btc_core.datasources.macro import (
    _yoy, china_m2_impulse, load_macro_signals,
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


def test_yoy_uses_calendar_month_not_index(tmp_path):
    """한 달이 빠져도 (연,월)−12 로 정확히 비교한다 — 위치로 12칸 뒤를 잡으면 어긋난다."""
    s = {}
    for m in range(1, 13):
        if m != 6:                                  # 2015-06 을 일부러 뺀다
            s[date(2015, m, 1)] = 100.0
    for m in range(1, 13):
        s[date(2016, m, 1)] = 110.0                 # 2016 은 10% 높은 수준
    g = _yoy(s)
    assert date(2016, 6, 1) not in g                # 2015-06 결측 → 건너뜀
    assert abs(g[date(2016, 7, 1)] - 10.0) < 1e-6   # 2015-07 대비 +10%, 위치라면 틀렸을 값


def test_impulse_requires_full_24_month_window(tmp_path):
    """24개월치가 다 있어야 임펄스를 낸다 — 짧은 스냅샷의 들쭉날쭉한 창을 막는다."""
    p = tmp_path / "m.csv"
    _write(p, _monthly(37))          # 37개월 → YoY 25점 → 직전 24개월 확보 → 계산됨
    assert china_m2_impulse(p) is not None
    _write(p, _monthly(36))          # 36개월 → YoY 24점 → 직전 23개월뿐 → None
    assert china_m2_impulse(p) is None


def test_impulse_needs_contiguous_calendar_window(tmp_path):
    """창 안에 달력상 한 달이라도 비면 임펄스를 내지 않는다 — 위치로 세면 창이
    조용히 25~26개월로 늘어나 스냅샷끼리 비교 불가능해진다 (M3)."""
    p = tmp_path / "m.csv"
    rows = _monthly(50)                         # 50개월 연속 → 임펄스 나온다
    _write(p, rows)
    assert china_m2_impulse(p) is not None
    # 마지막 YoY(50번째 달) 직전 24개월 창 안(40번째 달)을 하나 뺀다
    gapped = [r for i, r in enumerate(rows) if i != 39]
    _write(p, gapped)
    assert china_m2_impulse(p) is None


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


# --------------------------------------------------------------------------
# 멈춘 원본의 낡은 값을 '지금'이라 부르지 않는다 (2026-10 감사)
# --------------------------------------------------------------------------
def _until(last_ym, n=60):
    """last_ym 에서 끝나는 월간 n개."""
    y, m = last_ym
    for _ in range(n - 1):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return _monthly(n, start=(y, m), growth=0.006)


def test_a_stalled_china_m2_is_not_called_now(tmp_path):
    """chinadata.live 가 2026-05 에서 멈춘 채, 10월 화면이 그 값을 '지금 임펄스'라
    부르고 유동성 레짐을 매겼다. 원본이 멈춘 뒤로 그 값은 움직일 수가 없었다."""
    from btc_core.datasources.macro import M2_MAX_AGE_DAYS, china_m2_last
    p = tmp_path / "m.csv"
    _write(p, _until((2026, 5)))
    assert china_m2_last(p) == date(2026, 5, 1)
    # 정상 지연(다음 달 중순 발표) — 7월 말에 5월 값은 아직 '지금'이다
    assert china_m2_impulse(p, reference=date(2026, 7, 31)) is not None
    # 넉 달 넘게 멈춤 — 10월에 5월 값은 '지금'이 아니다
    assert china_m2_impulse(p, reference=date(2026, 10, 9)) is None
    assert load_macro_signals(p, reference=date(2026, 10, 9)) == {}
    # 경계: 정확히 M2_MAX_AGE_DAYS 일은 아직 쓴다, 하루 넘으면 안 쓴다
    from datetime import timedelta
    edge = date(2026, 5, 1) + timedelta(days=M2_MAX_AGE_DAYS)
    assert china_m2_impulse(p, reference=edge) is not None
    assert china_m2_impulse(p, reference=edge + timedelta(days=1)) is None
    # 정상 발표 지연(최대 ~75일)을 덮을 만큼 여유가 있어야 한다
    assert 80 <= M2_MAX_AGE_DAYS <= 120


def test_the_cli_judges_now_against_today(monkeypatch):
    """CLI 의 '지금' 계산이 reference=None 으로 부르면 나이를 잴 수 없다."""
    import inspect
    from btc_core import cli
    src = inspect.getsource(cli)
    assert "load_macro_signals(args.macro, reference=as_of)" not in src
    assert "load_macro_signals(args.macro, reference=as_of or date.today())" in src


def test_the_liquidity_payload_says_which_month_and_whether_it_is_stale(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import export_viz
    from btc_core.config import load_config

    p = tmp_path / "m.csv"
    _write(p, _until((2026, 5)))
    rows = [{"d": "2025-01-01", "price": 90000.0}, {"d": "2026-10-09", "price": 82000.0}]
    stale = export_viz.macro_lead(load_config(), str(p), rows, date(2026, 10, 9))
    assert stale["m2Month"] == "2026-05" and stale["m2Stale"] is True
    assert stale["impulse"] is None and stale["lrs"] is None

    fresh = export_viz.macro_lead(load_config(), str(p), rows, date(2026, 7, 31))
    assert fresh["m2Month"] == "2026-05" and fresh["m2Stale"] is False
    assert fresh["impulse"] is not None
