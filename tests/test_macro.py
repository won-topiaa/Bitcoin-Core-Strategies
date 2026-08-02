"""거시 연관성 분석 로직 검증 — 데이터가 없어도 코드는 맞아야 한다.

이 환경에서는 FRED 가 막혀 실제 나스닥·M2 를 못 받는다. 그래도 **분석 로직**은
합성 데이터로 검증할 수 있다. 답을 아는 데이터를 심어 두고, 도구가 그 답을
되찾는지 본다 — 심은 선행 개월, 심은 상관.
"""

from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

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


def test_gold_analysis_recovers_a_planted_lead():
    """선행/후행 스캔이 심어 둔 선행(개월)을 되찾는지 — best_lag 를 검증한다.
    gold 수익률[t] = btc 수익률[t+L] 로 만들면 금이 BTC 를 L개월 앞선다(k>0=금 선행)."""
    L = 3
    dates = month_grid(140)
    r = [0.3 * math.sin(i / 2.0) + 0.1 * math.cos(i / 5.0)
         for i in range(len(dates) + L + 2)]

    def cumexp(seq):                       # price[i] = exp(누적합) → 로그수익률[i] = seq[i]
        out, s = [], 0.0
        for x in seq:
            s += x
            out.append(math.exp(s))
        return out

    btc = {d: v for d, v in zip(dates, cumexp(r[:len(dates)]))}
    gold = {d: v for d, v in zip(dates, cumexp([r[i + L] for i in range(len(dates))]))}

    res = mc.gold_analysis(btc, gold, max_lag=6)
    assert res["best_lag"] == L, f"best_lag={res['best_lag']} (기대 {L})"
    assert res["best_corr"] is not None and res["best_corr"] > 0.9


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


def test_build_global_scales_us_to_the_same_units_as_foreign_legs():
    """전 세계 합계는 미국분(M2SL=십억$)을 외국분(원 달러)에 맞춰 환산해 더한다.

    m2_us 를 MABMM301(달러) → M2SL(십억$) 로 바꿨을 때 build_global 을 안 고쳐서,
    미국분이 1e9 배 작아져 합계에서 사실상 증발하던 실제 버그를 못 박는다. 이런
    '단위 바뀜' 은 조용히 틀리므로(에러가 안 난다) 테스트가 유일한 방어선이다."""
    import fetch_macro as fm

    d = date(2020, 1, 31)
    cols = {
        "m2_us": {d: 20_000.0},        # 십억 달러 = $20조
        "m2_jp": {d: 1_000_000.0},     # 엔 (원 단위)
        "fx_jp": {d: 100.0},           # 엔/달러 → $10,000
    }
    out = fm.build_global(cols)
    assert out and d in out
    # 미국분이 제대로 환산됐다면 합계는 $20조 + $1만 ≈ 2e13 이다.
    assert out[d] > 1e13, f"미국분이 증발했다(단위 불일치): {out[d]:.3e}"
    assert abs(out[d] - (20_000.0 * fm.M2_US_TO_USD + 10_000.0)) < 1.0


def test_gold_and_m2_columns_never_carry_non_finite_values():
    """받아 둔 실데이터에 NaN/inf 가 섞이면 임펄스·payload 로 번져 사이트가 백지가
    된다. 파일 자체를 검문한다 — 나중에 손으로 고치다 넣는 사고까지 잡힌다."""
    import math
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "data" / "macro.csv"
    if not p.exists():
        pytest.skip("data/macro.csv 없음")
    import csv as _csv
    bad = []
    with p.open(encoding="utf-8", newline="") as fh:
        for row in _csv.DictReader(fh):
            for col, v in row.items():
                if col == "date" or not (v or "").strip():
                    continue
                try:
                    f = float(v)
                except ValueError:
                    bad.append((row.get("date"), col, v))
                    continue
                if not math.isfinite(f):
                    bad.append((row.get("date"), col, v))
    assert not bad, f"비유한/비수치 셀: {bad[:5]}"


def test_m2_us_stays_on_one_scale():
    """m2_us 를 M2SL(십억$)로 재구성했는데 옛 MABMM301(달러, ~1e13) 값이 한 줄이라도
    남으면, 그 지점에서 전년비가 통째로 깨진다(수준이 1e9 배 튄다)."""
    from pathlib import Path
    import csv as _csv

    p = Path(__file__).resolve().parents[1] / "data" / "macro.csv"
    if not p.exists():
        pytest.skip("data/macro.csv 없음")
    vals = []
    with p.open(encoding="utf-8", newline="") as fh:
        for row in _csv.DictReader(fh):
            v = (row.get("m2_us") or "").strip()
            if v:
                vals.append((row["date"], float(v)))
    if not vals:
        pytest.skip("m2_us 열 비어 있음")
    hi = max(v for _, v in vals)
    assert hi < 1e7, f"달러 스케일 잔재로 보이는 값: 최대 {hi:.3e}"


def test_carry_analysis_detects_sign_and_partials_out_a_common_driver():
    """엔캐리 분석이 (1) 심은 부호를 잡고 (2) 공통 원인(공포)을 통제하면
    가짜 상관이 사라지는지 — 실측 판정(docs/26)의 핵심 논리를 검증한다."""
    import random
    rng = random.Random(5)
    days = [date(2016, 1, 4)]
    for _ in range(700):
        days.append(days[-1].fromordinal(days[-1].toordinal() + 3))

    # (1) 진짜 캐리: USDJPY 수익률이 BTC 를 양(+)으로 끌면 ρ>0 이어야
    jpy, btc, lj, lb = {}, {}, 100.0, 10000.0
    for d in days:
        jr = rng.gauss(0, 0.01)
        lj *= math.exp(jr); lb *= math.exp(1.5 * jr + rng.gauss(0, 0.005))
        jpy[d], btc[d] = lj, lb
    r = mc.carry_analysis(btc, jpy)
    assert r["full"] is not None and r["full"] > 0.5, r["full"]

    # (2) 가짜 상관: 공포(VIX)가 둘 다 흔드는 공통 원인일 때, 통제하면 사라진다.
    # 함수는 **ΔVIX(주간 변화)** 로 통제하므로, 충격이 '변화'에 담기도록 누적한다
    # (레벨에만 담으면 차분과 어긋나 통제가 성립하지 않는다 — 실제로 그렇게 짰다가 잡혔다).
    # 시작 수준을 넉넉히 둔다 — 낮게 잡으면 랜덤워크가 하한(9)에 자주 눌려 ΔVIX 가
    # 잘리고, 그 구간에서 통제가 성립하지 않아 테스트가 함수를 억울하게 신고한다.
    jpy2, btc2, vix2, lj, lb, lv = {}, {}, {}, 100.0, 10000.0, 100.0
    for d in days:
        shock = rng.gauss(0, 1.0)                 # 공통 원인
        lv = max(9.0, lv + shock * 3)             # 누적 → ΔVIX ≈ shock*3
        lj *= math.exp(-0.01 * shock + rng.gauss(0, 0.004))   # 공포↑ → 엔 강세
        lb *= math.exp(-0.02 * shock + rng.gauss(0, 0.004))   # 공포↑ → BTC 하락
        jpy2[d], btc2[d], vix2[d] = lj, lb, lv
    r2 = mc.carry_analysis(btc2, jpy2, vix2)
    assert r2["full"] > 0.3, f"공통 원인이면 단순 상관은 양수로 크게 뜬다: {r2['full']}"
    assert r2["partial_vix"] is not None
    assert abs(r2["partial_vix"]) < 0.2, (
        f"공통 원인을 통제하면 남는 게 없어야 한다: {r2['partial_vix']}")


def test_change_uses_log_for_prices_and_diff_for_rates():
    """_change: 가격류(양수)는 로그수익률, 수준 금리(음수 가능)는 차분(Δ).

    실질금리처럼 음수가 되는 계열에 로그를 쓰면 값이 통째로 사라져(도메인 밖)
    검정이 조용히 비어버린다. 후보 검정이 그 함정을 피하는지 못 박는다."""
    price = {date(2020, 1, 1): 100.0, date(2020, 1, 2): 110.0}
    assert abs(mc._change(price, True)[date(2020, 1, 2)] - math.log(1.1)) < 1e-9
    # 음수를 넘나드는 금리: 로그는 값을 못 만들고, 차분은 정확히 −0.3 을 준다
    rate = {date(2020, 1, 1): 0.2, date(2020, 1, 2): -0.1}
    assert mc._change(rate, True) == {}
    assert abs(mc._change(rate, False)[date(2020, 1, 2)] - (-0.3)) < 1e-9


def test_candidate_analysis_detects_sign_and_partials_out_a_common_driver():
    """거시 후보 검정이 (1) 심은 부호를 잡고 (2) 공통 원인(공포)을 통제하면 가짜
    상관이 사라지는지 — 실측 3종 기각(docs/27)의 핵심 논리를 검증한다."""
    import random
    rng = random.Random(11)
    days = [date(2016, 1, 4)]
    for _ in range(760):
        days.append(days[-1].fromordinal(days[-1].toordinal() + 3))

    # (1) 진짜 결합: driver 로그수익률이 BTC 를 양(+)으로 끌면 주간 ρ>0
    drv, btc, ld, lb = {}, {}, 100.0, 10000.0
    for d in days:
        dr = rng.gauss(0, 0.01)
        ld *= math.exp(dr); lb *= math.exp(1.3 * dr + rng.gauss(0, 0.005))
        drv[d], btc[d] = ld, lb
    r = mc.candidate_analysis(btc, drv, use_log=True)
    assert r["full_wk"] is not None and r["full_wk"] > 0.5, r["full_wk"]

    # (2) 가짜 상관: 공포(VIX)가 driver·BTC 를 동시에 흔드는 공통 원인일 때,
    # ΔVIX 를 통제하면 남는 게 없어야 한다. 충격을 '변화'에 담기도록 누적한다.
    drv2, btc2, vix2, ld, lb, lv = {}, {}, {}, 100.0, 10000.0, 100.0
    for d in days:
        shock = rng.gauss(0, 1.0)
        lv = max(9.0, lv + shock * 3)
        ld *= math.exp(0.01 * shock + rng.gauss(0, 0.004))    # 공포↑ → driver↑
        lb *= math.exp(0.02 * shock + rng.gauss(0, 0.004))    # 공포↑ → BTC↑ (같은 원인)
        drv2[d], btc2[d], vix2[d] = ld, lb, lv
    r2 = mc.candidate_analysis(btc2, drv2, vix2, use_log=True)
    assert r2["full_wk"] > 0.3, f"공통 원인이면 단순 상관은 크게 뜬다: {r2['full_wk']}"
    assert r2["partial_vix"] is not None and abs(r2["partial_vix"]) < 0.2, (
        f"공통 원인을 통제하면 남는 게 없어야 한다: {r2['partial_vix']}")


def test_report_never_names_a_candidate_it_did_not_measure():
    """리포트는 **실제로 잰 후보만** 언급해야 한다. 결론 문장에 후보 이름을 하드코딩해
    두면, 그 열이 없는 데이터로 돌렸을 때 '재지 않은 것을 잰 것처럼' 읽힌다 — 이
    프로젝트가 가장 경계하는 종류의 거짓말이라 문자열 수준에서 막는다."""
    dates = month_grid(150)
    btc = {d: 1000.0 * math.exp(0.3 * math.sin(i / 4)) for i, d in enumerate(dates)}
    base = {d: 100.0 * math.exp(0.2 * math.cos(i / 5)) for i, d in enumerate(dates)}

    # 코스피만 있고 나머지 후보는 없는 데이터
    only_kospi = mc.report(btc, {"kospi": base, "nasdaq": base})
    assert "코스피" in only_kospi
    for absent in ("DXY", "실질금리", "순유동성"):
        assert absent not in only_kospi, f"없는 후보를 언급했다: {absent}"

    # 후보가 하나도 없으면 [1e] 섹션 자체가 나오지 않는다
    none_present = mc.report(btc, {"nasdaq": base})
    assert "[1e]" not in none_present


def test_candidate_analysis_measures_a_monthly_driver_on_the_monthly_grid():
    """월간으로만 나오는 후보(코스피=OECD 월간 지수)를 주간 격자에 물리면 '1개월
    수익률 vs 1주 수익률'을 비교하게 돼 값이 통째로 틀린다. 빈도를 후보에 맞추는지,
    그리고 **부분상관도 같은 격자**에서 재는지 못 박는다.

    심는 값: 월간 driver 수익률 = BTC 월수익률 × 1 (완전 동행). 격자가 맞으면 ρ≈+1,
    주간 격자에 잘못 물리면 짝이 어긋나 ρ 가 크게 떨어진다."""
    import random
    rng = random.Random(3)
    days = [date(2014, 1, 6)]
    for _ in range(1500):                       # 일간 BTC (주간 격자가 촘촘하도록)
        days.append(days[-1].fromordinal(days[-1].toordinal() + 3))

    btc, lb = {}, 10000.0
    for d in days:
        lb *= math.exp(rng.gauss(0, 0.02))
        btc[d] = lb

    # 월간 driver: 그 달 BTC 월말값에 비례 → 월 로그수익률이 정확히 같다
    bm = mc.month_end(btc)
    driver = {d: v * 0.01 for d, v in bm.items()}

    r = mc.candidate_analysis(btc, driver, use_log=True)
    assert r["monthly_only"] is True, "월간 후보를 월간으로 인식해야 한다"
    assert r["primary"] == "월간"
    assert r["full_wk"] is None and r["n_wk"] == 0, "주간 프레임은 계산하지 않는다"
    assert r["full_mo"] is not None and r["full_mo"] > 0.99, (
        f"같은 격자면 심은 완전 동행을 되찾아야 한다: {r['full_mo']}")

    # 부분상관도 월간 격자여야 한다 — 무관한 통제를 넣어도 값이 살아남는다
    ndq = {d: 100.0 * math.exp(rng.gauss(0, 0.01)) for d in days}
    r2 = mc.candidate_analysis(btc, driver, nasdaq=ndq, use_log=True)
    assert r2["partial_ndq"] is not None, "월간 격자에서 부분상관이 계산돼야 한다"
    assert r2["partial_ndq"] > 0.9, (
        f"무관한 통제는 완전 동행을 못 지운다(격자 어긋남 의심): {r2['partial_ndq']}")


def test_build_net_liquidity_aligns_rrp_to_the_other_legs():
    """순유동성 = WALCL − TGA − RRP. WALCL·TGA 는 백만$, RRP(RRPONTSYD)는 십억$ 라
    RRP 를 ×1000 해야 맞다. 안 맞추면 RRP 가 1000배 작아져 사실상 빠진다 — build_global
    의 m2_us 단위 버그와 같은 부류의 '조용히 틀림'이라, 테스트가 유일한 방어선이다."""
    import fetch_macro as fm

    d = date(2022, 6, 1)
    cols = {
        "_walcl": {d: 8_900_000.0},   # 백만$ = $8.9조
        "_tga":   {d: 700_000.0},     # 백만$ = $0.7조
        "_rrp":   {d: 2_200.0},       # 십억$ = $2.2조  (×1000 = 2_200_000 백만$)
    }
    out = fm.build_net_liquidity(cols)
    assert out and d in out
    # 정렬이 맞으면 8.9 − 0.7 − 2.2 = $6.0조 = 6_000_000 백만$
    assert abs(out[d] - (8_900_000.0 - 700_000.0 - 2_200.0 * fm.RRP_B_TO_M)) < 1e-6
    assert abs(out[d] - 6_000_000.0) < 1.0
    # ×1000 을 빠뜨렸다면 값이 ~$8.2조로 크게 부풀어 오른다 — 그 회귀를 배제한다.
    assert out[d] < 7_000_000.0, "RRP 단위 정렬 누락으로 보이는 값"


def test_build_net_liquidity_survives_a_missing_leg():
    """TGA·RRP 가 아직 0 이던 초기(또는 소스가 잠깐 빠진) 구간에서도 죽지 않고
    있는 항으로만 계산한다 — WALCL 만 있으면 net_liq = WALCL 로 이어진다."""
    import fetch_macro as fm

    d = date(2011, 1, 5)
    out = fm.build_net_liquidity({"_walcl": {d: 2_400_000.0}})
    assert out and abs(out[d] - 2_400_000.0) < 1e-6


# ---------------------------------------------------------------------------
# 부분상관 — 통제가 완전한 예측자일 때
# ---------------------------------------------------------------------------
def test_pearson_never_returns_a_value_outside_minus_one_to_one():
    """두 계열이 사실상 같으면 부동소수 오차로 1.0000000000000002 가 나온다.

    그 값이 부분상관의 sqrt(1 − r²) 에 들어가면 **음수의 제곱근**이 되어
    ValueError 로 죽는다. 사이트 빌드가 이 경로를 지나므로 실제 사고가 된다.
    """
    xs = [i * 0.7 - 3 for i in range(200)]
    for ys in ([x * 0.8 for x in xs], [-x * 1.3 for x in xs], xs):
        r = mc.pearson(xs, ys)
        assert r is not None and -1.0 <= r <= 1.0, r


def test_partial_correlation_returns_none_instead_of_crashing_on_a_perfect_control():
    """통제가 후보와 BTC 를 **완전히** 설명하면 부분상관은 정의되지 않는다.
    정의되지 않는 것을 None 으로 돌려주는 것과 죽는 것은 다르다."""
    n = 200
    base = [math.sin(i / 5.0) + i * 0.01 for i in range(n)]
    dates = [date(2013, 1, 7) + timedelta(days=7 * i) for i in range(n)]
    keys = [d.isocalendar()[:2] for d in dates]
    dk = dict(zip(keys, [0.6 * v for v in base]))
    bk = dict(zip(keys, [0.8 * v for v in base]))
    ctrl = dict(zip(dates, base))
    assert mc._partial(dk, bk, ctrl, False) is None


def test_partial_from_r_matches_the_textbook_formula():
    """공용 헬퍼로 모으면서 식이 바뀌지 않았는지 — 세 도구가 이 함수를 쓴다."""
    xy, xz, yz = 0.5, 0.4, 0.3
    want = (xy - xz * yz) / math.sqrt((1 - xz ** 2) * (1 - yz ** 2))
    assert abs(mc.partial_from_r(xy, xz, yz) - want) < 1e-12
    assert mc.partial_from_r(None, xz, yz) is None
    assert mc.partial_from_r(xy, 1.0, yz) is None          # 완전 통제 → 정의 불가
