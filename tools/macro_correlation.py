#!/usr/bin/env python3
"""비트코인 ↔ 거시(나스닥·M2) 연관성 분석.

무료 데이터만 쓴다. 거시 시계열은 data/macro.csv 에서 읽는다(오프라인 우선) —
받는 것은 tools/fetch_macro.py 가 맡고, 이 파일은 **분석만** 한다.

    python3 tools/macro_correlation.py --macro data/macro.csv
    python3 tools/macro_correlation.py --self-test     # 합성 데이터로 로직 검증

data/macro.csv 형식(첫 열 date, 나머지는 있는 것만):

    date,nasdaq,m2_us,m2_global,m2_cn,m2_eu,m2_jp
    2014-01-31,4103.88,11007.2,,,,
    ...

## 무엇을 재는가 — 그리고 무엇을 조심하는가

두 시계열의 **수준(레벨)** 상관은 의미가 없다. 나스닥·M2·비트코인은 전부
장기 우상향이라, 레벨끼리 상관을 재면 "같이 올랐다"만 나온다(허위 상관).
그래서 전부 **변화율**로 잰다.

  나스닥 — 주간 로그수익률의 이동상관. "요즘 얼마나 같이 움직이나."
  M2     — 전년비 증가율의 **선행/후행** 스캔. "M2 가 비트코인을 몇 주 앞서나."

M2 분석의 핵심은 선행·후행이다. 통화량이 늘면 위험자산이 나중에 따라 오른다는
가설을, BTC 수익률을 M2 증가율보다 k개월 **뒤로** 놓고 상관이 가장 커지는 k를
찾아 검정한다. 전 세계 M2 와 개별국 M2 를 같은 방식으로 재서 어느 쪽이 더 잘
설명하는지 비교한다.

**조심할 것.** 통화 사이클 표본이 적고(2013~ 두세 번), 2020~2021 유동성 급증
한 번이 상관을 통째로 끌어올릴 수 있다. 그래서 표본 외 안정성은 이 분석이
답하지 못한다 — LRS 축을 실행 *방향*이 아니라 *크기*로만 쓰는 이유다.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------
# 통계 (표준 라이브러리만)
# ---------------------------------------------------------------------------
def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 3:
        return None

    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    return pearson(rank(xs), rank(ys))


# ---------------------------------------------------------------------------
# 시계열 헬퍼 — 날짜→값 딕셔너리로 다룬다
# ---------------------------------------------------------------------------
def load_btc(csv_path: str) -> dict[date, float]:
    out: dict[date, float] = {}
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            v = row.get("price")
            if not v:
                continue
            try:
                out[date.fromisoformat(row["date"][:10])] = float(v)
            except (ValueError, KeyError):
                continue
    return out


def load_macro(csv_path: str) -> dict[str, dict[date, float]]:
    p = Path(csv_path)
    if not p.exists():
        raise SystemExit(
            f"거시 데이터가 없습니다: {p}\n"
            "  tools/fetch_macro.py 로 받거나, 직접 CSV 를 만들어 주세요.\n"
            "  형식: date,nasdaq,m2_us,m2_global,...")
    cols: dict[str, dict[date, float]] = {}
    with p.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        names = [c for c in (reader.fieldnames or []) if c != "date"]
        for c in names:
            cols[c] = {}
        for row in reader:
            try:
                d = date.fromisoformat((row.get("date") or "")[:10])
            except ValueError:
                continue
            for c in names:
                v = (row.get(c) or "").strip()
                if v:
                    try:
                        cols[c][d] = float(v)
                    except ValueError:
                        pass
    return {c: s for c, s in cols.items() if s}


def month_end(series: dict[date, float]) -> dict[date, float]:
    """각 월의 마지막 관측만 남긴다. (year, month) → 값."""
    by_month: dict[tuple[int, int], tuple[date, float]] = {}
    for d in sorted(series):
        key = (d.year, d.month)
        by_month[key] = (d, series[d])
    return {d: v for d, v in by_month.values()}


def weekly(series: dict[date, float]) -> dict[date, float]:
    """각 ISO 주의 마지막 관측만."""
    by_week: dict[tuple[int, int], tuple[date, float]] = {}
    for d in sorted(series):
        iso = d.isocalendar()
        by_week[(iso[0], iso[1])] = (d, series[d])
    return {d: v for d, v in by_week.values()}


def log_returns(series: dict[date, float]) -> dict[date, float]:
    ds = sorted(series)
    out: dict[date, float] = {}
    for a, b in zip(ds, ds[1:]):
        if series[a] > 0 and series[b] > 0:
            out[b] = math.log(series[b] / series[a])
    return out


def yoy(series: dict[date, float], months: int = 12) -> dict[date, float]:
    """전년비 증가율(%). 월말 시계열에 쓴다."""
    out: dict[date, float] = {}
    ds = sorted(series)
    for i, d in enumerate(ds):
        if i >= months:
            prev = series[ds[i - months]]
            if prev:
                out[d] = (series[d] / prev - 1.0) * 100.0
    return out


def _align(a: dict[date, float], b: dict[date, float],
           tol_days: int = 20) -> tuple[list[float], list[float]]:
    """두 시계열을 공통 시점으로 맞춘다. b 는 a 의 각 날짜에 tol 안에서 가장
    가까운 값을 쓴다(월간·주간 격자가 살짝 어긋나도 되도록)."""
    bs = sorted(b)
    xs, ys = [], []
    for d in sorted(a):
        best, gap = None, tol_days + 1
        for db in bs:
            g = abs((db - d).days)
            if g < gap:
                best, gap = db, g
            if db > d + timedelta(days=tol_days):
                break
        if best is not None and gap <= tol_days:
            xs.append(a[d])
            ys.append(b[best])
    return xs, ys


# ---------------------------------------------------------------------------
# 분석 1 — 나스닥 ↔ 비트코인 (수익률 이동상관)
# ---------------------------------------------------------------------------
def _is_monthly(series: dict[date, float]) -> bool:
    """관측 간격이 대체로 20일 이상이면 월간으로 본다."""
    ds = sorted(series)
    if len(ds) < 4:
        return False
    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    gaps.sort()
    return gaps[len(gaps) // 2] > 20     # 중위 간격


def nasdaq_analysis(btc: dict[date, float], ndq: dict[date, float]) -> dict:
    # 주가 시계열의 빈도에 맞춘다. 월간 지수(S&P500 datahub 등)를 주간 BTC 와
    # 비교하면 1주 수익률 vs 1개월 수익률이 되어 어긋난다. 둘 다 같은 빈도로.
    monthly = _is_monthly(ndq)
    freq = month_end if monthly else weekly
    bw, nw = freq(btc), freq(ndq)
    br, nr = log_returns(bw), log_returns(nw)

    # **달력 구간**으로 맞춘다. 월간 지수는 1일, BTC 월말은 30일이라 정확한
    # 날짜로 교집합하면 하나도 안 맞는다. (연,월) 또는 (연,주)로 키를 잡는다.
    def key(d: date):
        return (d.year, d.month) if monthly else d.isocalendar()[:2]

    brp = {key(d): v for d, v in sorted(br.items())}
    nrp = {key(d): v for d, v in sorted(nr.items())}
    common = sorted(set(brp) & set(nrp))
    x = [brp[k] for k in common]
    y = [nrp[k] for k in common]
    full = pearson(x, y)

    by_year: dict[int, tuple[list[float], list[float]]] = {}
    for k in common:
        by_year.setdefault(k[0], ([], []))
        by_year[k[0]][0].append(brp[k])
        by_year[k[0]][1].append(nrp[k])
    min_pts = 6 if monthly else 10
    yearly = {y_: pearson(xs, ys) for y_, (xs, ys) in sorted(by_year.items())
              if len(xs) >= min_pts}

    # 국면 분할 — 이게 핵심 발견이다. 연도별은 표본이 작아 튀므로, 2020년
    # (코로나·기관 유입) 앞뒤로 크게 갈라 안정된 값을 본다.
    def era_corr(lo: int, hi: int):
        ks = [k for k in common if lo <= k[0] <= hi]
        return (pearson([brp[k] for k in ks], [nrp[k] for k in ks]), len(ks))
    eras = {"2013~2019": era_corr(2013, 2019), "2020~현재": era_corr(2020, 2100)}

    win = 24 if monthly else 26
    recent = common[-win:]
    rec = pearson([brp[k] for k in recent], [nrp[k] for k in recent]) \
        if len(recent) >= min_pts else None
    return {"n": len(common), "unit": "개월" if monthly else "주",
            "full": full, "yearly": yearly, "eras": eras, "recent": rec,
            "recent_win": win, "span": (common[0], common[-1]) if common else None}


# ---------------------------------------------------------------------------
# 분석 1b — VIX(공포지수) ↔ 비트코인 (수익률 vs VIX 변화)
# ---------------------------------------------------------------------------
def vix_analysis(btc: dict[date, float], vix: dict[date, float]) -> dict:
    """월간 BTC 로그수익률 ↔ 월간 VIX 변화(ΔVIX)의 상관.

    VIX 는 주식시장 공포지수다(레벨 10~80, 평균회귀). 비트코인이 '고베타
    위험자산'으로 움직이면 **공포가 커질 때(ΔVIX>0) 같이 빠진다** → 음의 상관이
    기대된다. 레벨이 아니라 변화로 재는 이유: 둘 다 우상향이 아니라, '이번 달
    공포가 늘었나/줄었나' vs '이번 달 BTC 가 올랐나/내렸나' 를 맞춰야 한다.
    """
    bm, vm = month_end(btc), month_end(vix)
    br = log_returns(bm)                      # BTC 월 로그수익률
    vd: dict[date, float] = {}                # ΔVIX (이번달 - 지난달)
    vs = sorted(vm)
    for a, b in zip(vs, vs[1:]):
        vd[b] = vm[b] - vm[a]

    def key(d: date):
        return (d.year, d.month)
    brp = {key(d): v for d, v in sorted(br.items())}
    vdp = {key(d): v for d, v in sorted(vd.items())}
    common = sorted(set(brp) & set(vdp))
    x = [brp[k] for k in common]
    y = [vdp[k] for k in common]

    def era(lo, hi):
        ks = [k for k in common if lo <= k[0] <= hi]
        return (pearson([brp[k] for k in ks], [vdp[k] for k in ks]), len(ks))
    return {"n": len(common), "full": pearson(x, y),
            "eras": {"2013~2019": era(2013, 2019), "2020~현재": era(2020, 2100)},
            "span": (common[0], common[-1]) if common else None}


# ---------------------------------------------------------------------------
# 분석 2 — M2 ↔ 비트코인 (전년비 증가율의 선행/후행)
# ---------------------------------------------------------------------------
def m2_lead_lag(btc_month: dict[date, float], m2: dict[date, float],
                max_lag: int = 6) -> dict:
    """BTC 전년비를 M2 전년비보다 k개월 뒤로 놓고 상관. 최적 k(=M2 선행)를 찾는다."""
    btc_yoy = yoy(btc_month)
    m2_yoy = yoy(month_end(m2))
    m2_dates = sorted(m2_yoy)
    if len(m2_dates) < 18:
        return {"n": 0}

    def shift_months(d: date, k: int) -> date:
        # k개월 전 날짜(근사) — 월말 격자라 15일 허용으로 맞춘다
        y_, m_ = d.year, d.month - k
        while m_ <= 0:
            m_ += 12
            y_ -= 1
        return date(y_, m_, 15)

    results: dict[int, Optional[float]] = {}
    for k in range(0, max_lag + 1):
        xs, ys = [], []
        for d in sorted(btc_yoy):
            target = shift_months(d, k)
            best, gap = None, 20
            for dm in m2_dates:
                g = abs((dm - target).days)
                if g < gap:
                    best, gap = dm, g
            if best is not None:
                xs.append(m2_yoy[best])
                ys.append(btc_yoy[d])
        results[k] = pearson(xs, ys) if len(xs) >= 12 else None

    valid = {k: v for k, v in results.items() if v is not None}
    best_k = max(valid, key=lambda k: valid[k]) if valid else None
    n = sum(1 for d in btc_yoy)
    return {"n": n, "by_lag": results, "best_lag": best_k,
            "best_corr": valid.get(best_k) if best_k is not None else None,
            "contemporaneous": results.get(0)}


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------
def report(btc: dict[date, float], macro: dict[str, dict[date, float]]) -> str:
    L: list[str] = []
    add = L.append
    add("=" * 78)
    add("  비트코인 ↔ 거시 연관성 — 무료 데이터, 변화율 기준")
    add("=" * 78)
    btc_month = month_end(btc)

    # --- 주식지수 (나스닥/S&P500) ---
    eq_key = next((k for k in macro if any(
        t in k.lower() for t in ("nasdaq", "ndq", "sp500", "s&p", "equit"))), None)
    label = eq_key or "주가지수"
    add(f"\n[1] {label} ↔ 비트코인 — 로그수익률 상관")
    add("-" * 78)
    if eq_key is None:
        add("  주가지수 열이 없습니다 (macro.csv 에 nasdaq 또는 sp500 추가).")
    else:
        r = nasdaq_analysis(btc, macro[eq_key])
        u = r["unit"]
        if r["span"]:
            add(f"  기간 {r['span'][0]} ~ {r['span'][1]} · 공통 {r['n']}{u} "
                f"({'월간' if u == '개월' else '주간'} 수익률)")
        add(f"  전 구간 상관 ρ = {r['full']:+.2f}" if r["full"] is not None else "  전 구간 상관 —")
        add("  국면별 (핵심):")
        for name, (v, npt) in r["eras"].items():
            add(f"    {name}  ρ {v:+.2f}   ({npt}{u})" if v is not None else f"    {name}  —")
        add(f"  최근 {r['recent_win']}{u} ρ = {r['recent']:+.2f}"
            if r["recent"] is not None else f"  최근 {r['recent_win']}{u} —")
        add("  연도별 ρ (표본 작아 참고):")
        for y_, v in r["yearly"].items():
            bar = "█" * max(0, round((v or 0) * 20))
            neg = "░" * max(0, round(-(v or 0) * 20))
            add(f"    {y_}  {v:+.2f}  {neg}{bar}")

    # --- VIX (공포지수) — 있으면 ---
    vix_key = next((k for k in macro if "vix" in k.lower()), None)
    if vix_key is not None:
        add("\n[1b] VIX(공포지수) ↔ 비트코인 — 월수익률 vs ΔVIX (위험자산성 점검)")
        add("-" * 78)
        v = vix_analysis(btc, macro[vix_key])
        if v["span"]:
            add(f"  기간 {v['span'][0]} ~ {v['span'][1]} · 공통 {v['n']}개월")
        add(f"  전 구간 ρ = {v['full']:+.2f}   (음수 = 공포 커질 때 BTC 하락 = 위험자산)"
            if v["full"] is not None else "  전 구간 —")
        for name, (val, npt) in v["eras"].items():
            add(f"    {name}  ρ {val:+.2f}   ({npt}개월)" if val is not None
                else f"    {name}  —")

    # --- M2 (열마다) ---
    add("\n[2] M2 ↔ 비트코인 — 전년비 증가율, 선행/후행 스캔")
    add("-" * 78)
    m2_keys = [k for k in macro if k.lower().startswith("m2") or "m2" in k.lower()]
    if not m2_keys:
        add("  M2 열이 없습니다 (macro.csv 에 m2_us, m2_global 등 추가).")
    else:
        add(f"  {'시리즈':<14}{'동시':>7}{'최적선행':>9}{'그때 ρ':>9}   (선행 = M2 가 BTC 를 앞선 개월)")
        rows = []
        for k in m2_keys:
            r = m2_lead_lag(btc_month, macro[k])
            if r.get("n", 0) == 0:
                add(f"  {k:<14}  데이터 부족")
                continue
            rows.append((k, r))
            con = r["contemporaneous"]
            bl, bc = r["best_lag"], r["best_corr"]
            add(f"  {k:<14}{(f'{con:+.2f}' if con is not None else '—'):>7}"
                f"{(f'{bl}개월' if bl is not None else '—'):>9}"
                f"{(f'{bc:+.2f}' if bc is not None else '—'):>9}")
        # 최적 시리즈
        best = max((r for _, r in rows if r.get("best_corr") is not None),
                   key=lambda r: r["best_corr"], default=None)
        if best is not None and rows:
            winner = next(k for k, r in rows if r is best)
            add(f"\n  → 가장 잘 설명하는 것: {winner} "
                f"(선행 {best['best_lag']}개월, ρ {best['best_corr']:+.2f})")
        add("\n  전체 선행/후행 곡선 (동시=0):")
        for k, r in rows:
            curve = "  ".join(
                f"{lag}:{(v if v is not None else float('nan')):+.2f}"
                for lag, v in sorted(r["by_lag"].items()))
            add(f"    {k:<12} {curve}")

    add("\n" + "=" * 78)
    add("  주의: 표본이 작고(통화 사이클 두세 번) 2020~21 유동성 급증이 상관을")
    add("  끌어올린다. 선행 관계는 표본 외에서 불안정하다 — 실행 크기 조절(LRS)")
    add("  용도이지 방향 신호가 아니다.")
    add("=" * 78)
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 자체 검증 — 합성 데이터로 로직이 맞는지
# ---------------------------------------------------------------------------
def self_test() -> int:
    """M2 가 BTC 를 정확히 3개월 앞서도록 합성해서, 스캔이 그걸 찾는지 본다."""
    import random
    rng = random.Random(42)
    d0 = date(2013, 1, 31)
    months = []
    d = d0
    for _ in range(150):
        months.append(d)
        # 다음 월말
        y_, m_ = (d.year + (1 if d.month == 12 else 0)), (1 if d.month == 12 else d.month + 1)
        d = date(y_, m_, 28)

    # M2 증가율: 완만한 파동
    m2_growth = [8 + 5 * math.sin(i / 9.0) for i in range(len(months))]
    m2 = {}
    lvl = 10000.0
    for i, dt in enumerate(months):
        lvl *= (1 + m2_growth[i] / 100 / 12)
        m2[dt] = lvl

    # BTC 증가율 = 3개월 전 M2 증가율 × 8 + 잡음  → M2 가 3개월 선행
    btc = {}
    price = 100.0
    for i, dt in enumerate(months):
        drive = m2_growth[i - 3] if i >= 3 else m2_growth[0]
        g = drive * 8 + rng.gauss(0, 15)          # 월 증가율(%)
        price *= (1 + g / 100)
        btc[dt] = max(1.0, price)

    r = m2_lead_lag(btc, m2, max_lag=6)
    print("자체 검증 — M2 를 BTC 3개월 선행으로 합성")
    print("  선행/후행 상관:", {k: (round(v, 2) if v is not None else None)
                              for k, v in r["by_lag"].items()})
    print(f"  찾은 최적 선행: {r['best_lag']}개월 (ρ {r['best_corr']:+.2f})")
    ok = r["best_lag"] in (2, 3, 4)     # 3±1 이면 통과
    print("  판정:", "✔ 통과 — 심어 둔 3개월 선행을 찾았다" if ok else "✘ 실패")
    return 0 if ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="비트코인 ↔ 거시 연관성 분석")
    ap.add_argument("--csv", default="data/market.csv", help="비트코인 가격 CSV")
    ap.add_argument("--macro", default="data/macro.csv", help="거시 시계열 CSV")
    ap.add_argument("--out", default=None, help="리포트 저장 경로")
    ap.add_argument("--self-test", action="store_true", help="합성 데이터로 로직 검증")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    btc = load_btc(args.csv)
    macro = load_macro(args.macro)
    text = report(btc, macro)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
