"""거시 연관성 분석 로직 검증 — 데이터가 없어도 코드는 맞아야 한다.

이 환경에서는 FRED 가 막혀 실제 나스닥·M2 를 못 받는다. 그래도 **분석 로직**은
합성 데이터로 검증할 수 있다. 답을 아는 데이터를 심어 두고, 도구가 그 답을
되찾는지 본다 — 심은 선행 개월, 심은 상관.
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import macro_correlation as mc  # noqa: E402


def month_grid(n: int, start=date(2013, 1, 31)) -> list[date]:
    out, d = [], start
    for _ in range(n):
        out.append(d)
        y, m = (d.year + (1 if d.month == 12 else 0)), (1 if d.month == 12 else d.month + 1)
        d = date(y, m, 28)
    return out


def test_pearson_and_spearman_basics():
    xs = [1, 2, 3, 4, 5]
    assert abs(mc.pearson(xs, xs) - 1.0) < 1e-9
    assert abs(mc.pearson(xs, [5, 4, 3, 2, 1]) + 1.0) < 1e-9
    assert abs(mc.spearman(xs, [1, 4, 9, 16, 25]) - 1.0) < 1e-9   # 단조 → 1
    assert mc.pearson([1, 1, 1], [1, 2, 3]) is None                # 분산 0


def test_yoy_growth():
    months = month_grid(14)
    series = {d: 100 * (1.01 ** i) for i, d in enumerate(months)}   # 매월 +1%
    g = mc.yoy(series)
    # 12개월 뒤 ≈ (1.01^12 − 1) = 12.7%
    assert g and all(abs(v - 12.68) < 0.2 for v in g.values())


def test_month_end_keeps_last_per_month():
    s = {date(2020, 1, 5): 1.0, date(2020, 1, 20): 2.0, date(2020, 2, 3): 3.0}
    me = mc.month_end(s)
    assert me[date(2020, 1, 20)] == 2.0 and date(2020, 1, 5) not in me
    assert me[date(2020, 2, 3)] == 3.0


def test_lead_lag_recovers_planted_lead():
    """M2 를 BTC 보다 정확히 3개월 앞서게 심고, 스캔이 3(±1)을 찾는지."""
    months = month_grid(140)
    m2_growth = [8 + 5 * math.sin(i / 9.0) for i in range(len(months))]
    m2, lvl = {}, 10000.0
    for i, d in enumerate(months):
        lvl *= (1 + m2_growth[i] / 100 / 12)
        m2[d] = lvl
    btc, price = {}, 100.0
    for i, d in enumerate(months):
        drive = m2_growth[i - 3] if i >= 3 else m2_growth[0]     # 3개월 선행
        price *= (1 + (drive * 8) / 100)                         # 잡음 없이 깨끗하게
        btc[d] = max(1.0, price)
    r = mc.m2_lead_lag(btc, m2, max_lag=6)
    assert r["best_lag"] in (2, 3, 4), r["by_lag"]
    assert r["best_corr"] > 0.5


def test_nasdaq_correlation_recovers_beta():
    """나스닥을 BTC 수익률의 0.5배 + 잡음으로 심으면 강한 양의 상관이 나와야."""
    import random
    rng = random.Random(7)
    days = [date(2018, 1, 1)]
    for _ in range(600):
        days.append(days[-1].fromordinal(days[-1].toordinal() + 3))   # 3일 간격
    btc, price = {}, 10000.0
    for d in days:
        price *= math.exp(rng.gauss(0, 0.03))
        btc[d] = price
    ndq, lvl = {}, 3000.0
    bd = sorted(btc)
    for i, d in enumerate(bd):
        if i:
            br = math.log(btc[d] / btc[bd[i - 1]])
            lvl *= math.exp(0.5 * br + rng.gauss(0, 0.01))
        ndq[d] = lvl
    r = mc.nasdaq_analysis(btc, ndq)
    assert r["full"] is not None and r["full"] > 0.6


def test_vix_recovers_negative_risk_asset_relation():
    """VIX 가 오를 때(공포↑) BTC 가 빠지게 심으면 음의 상관이 나와야."""
    import random
    rng = random.Random(11)
    days = [date(2015, 1, 1)]
    for _ in range(360):
        days.append(days[-1].fromordinal(days[-1].toordinal() + 5))
    vix, lvl = {}, 20.0
    for d in days:
        lvl = max(9.0, lvl + rng.gauss(0, 3))       # 평균회귀 없이 랜덤워크로 충분
        vix[d] = lvl
    # BTC 수익률 = -0.05 * ΔVIX(월) + 잡음  → 공포 커지면 하락
    btc, price = {}, 10000.0
    vs = sorted(vix)
    for i, d in enumerate(vs):
        dv = vix[d] - vix[vs[i - 1]] if i else 0.0
        price *= math.exp(-0.05 * dv + rng.gauss(0, 0.02))
        btc[d] = price
    r = mc.vix_analysis(btc, vix)
    assert r["full"] is not None and r["full"] < -0.3, r["full"]


def test_align_matches_nearest_within_tolerance():
    # 월말끼리 하루이틀 어긋난 격자 → 같은 달끼리 매칭돼야
    a = {date(2020, 1, 31): 1.0, date(2020, 2, 29): 2.0}
    b = {date(2020, 1, 30): 10.0, date(2020, 2, 28): 20.0}
    xs, ys = mc._align(a, b, tol_days=5)
    assert list(zip(xs, ys)) == [(1.0, 10.0), (2.0, 20.0)]
    # 허용범위 밖(20일↑)은 버린다
    far = {date(2020, 1, 31): 1.0}
    xs2, _ = mc._align(far, {date(2020, 3, 15): 9.0}, tol_days=5)
    assert xs2 == []


def test_self_test_passes():
    assert mc.self_test() == 0


def test_gold_analysis_detects_sign_and_null():
    """금 분석이 심은 양/음 상관은 잡고 무상관은 낮게 낸다 (docs/25 재현 로직).

    실제 금↔BTC 는 무상관(|ρ|<0.2)이지만, 그 판정을 내리는 로직 자체는 답을
    아는 합성 데이터로 검증할 수 있다 — 부호와 크기를 되찾는지 본다."""
    dates = month_grid(120)
    btc = {d: 1000.0 * math.exp(0.3 * math.sin(i / 3)) for i, d in enumerate(dates)}
    gold_pos = {d: 50.0 * btc[d] ** 0.5 for d in dates}                 # log 선형 → ρ≈+1
    gold_neg = {d: 5.0e6 / btc[d] for d in dates}                       # 역수 → ρ≈−1
    gold_null = {d: 1000.0 * math.exp(0.3 * math.cos(i / 7))
                 for i, d in enumerate(dates)}                          # 다른 주파수 → ρ≈0
    assert mc.gold_analysis(btc, gold_pos)["full"] > 0.9
    assert mc.gold_analysis(btc, gold_neg)["full"] < -0.9
    assert abs(mc.gold_analysis(btc, gold_null)["full"]) < 0.5


def test_merge_to_csv_preserves_columns_from_a_missing_source(tmp_path):
    """소스 하나가 이번 run 에 빠져도 그 열/값은 기존 파일에서 살아남는다 (H1).

    통째로 덮어쓰던 옛 방식은 chinadata 가 잠깐 막히면 m2_cn 열을 통째로 지워
    LRS 축이 조용히 사라졌다. 병합은 그 실패가 열을 지우지 못하게 한다."""
    import fetch_macro as fm

    p = tmp_path / "macro.csv"
    fm.merge_to_csv({
        "m2_cn": {date(2020, 1, 31): 100.0, date(2020, 2, 29): 101.0},
        "vix": {date(2020, 1, 31): 20.0},
    }, p)
    # 둘째 run: chinadata 가 막혀 m2_cn 없이 vix 만 새로 왔다
    fm.merge_to_csv({"vix": {date(2020, 2, 29): 22.0}}, p)

    got = fm._read_existing(p)
    assert "m2_cn" in got                                  # 지워지지 않았다
    assert got["m2_cn"][date(2020, 1, 31)] == 100.0
    assert got["m2_cn"][date(2020, 2, 29)] == 101.0
    assert got["vix"][date(2020, 2, 29)] == 22.0           # 새 run 값이 이긴다
    assert got["vix"][date(2020, 1, 31)] == 20.0           # 기존 값 보존
