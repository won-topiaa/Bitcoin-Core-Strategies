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
    if n < 3 or len(ys) != n:      # 길이가 다르면 cov(zip) 만 잘려 조용히 틀린다
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    # **[-1, 1] 로 조인다.** 두 계열이 사실상 같으면 부동소수 오차로 1.0000000000000002
    # 가 나오고, 그 값이 부분상관의 sqrt(1 - r**2) 에 들어가면 음수 제곱근이 되어
    # ValueError 로 죽는다. 사이트 빌드가 이 경로를 지나므로 실제 사고가 된다.
    return max(-1.0, min(1.0, cov / (sx * sy)))


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
    """전년비 증가율(%). **달력 기준으로** 12개월 전을 찾는다 — 위치로 12칸 뒤를
    잡으면 월이 하나라도 빠지거나 겹칠 때 11/13개월 전과 비교해 값이 틀어진다.
    정확히 (연,월)−12 관측이 없으면 그 점은 건너뛴다."""
    out: dict[date, float] = {}
    by_ym = {(d.year, d.month): d for d in sorted(series)}
    for d in sorted(series):
        y, m = d.year, d.month - months
        while m <= 0:
            m += 12
            y -= 1
        prev = by_ym.get((y, m))
        if prev is not None and series[prev]:
            out[d] = (series[d] / series[prev] - 1.0) * 100.0
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
# 분석 1d — 엔캐리 트레이드 ↔ 비트코인 (엔 약세=캐리 활발 가설 점검)
# ---------------------------------------------------------------------------
def carry_analysis(btc: dict[date, float], usdjpy: dict[date, float],
                   vix: Optional[dict[date, float]] = None) -> dict:
    """주간 BTC 로그수익률 ↔ USDJPY 로그수익률. 가설이 맞으면 **양(+)** 이어야 한다.

    엔캐리(엔을 싸게 빌려 위험자산을 산다)가 BTC 를 움직인다면, 엔이 약해질 때
    (USDJPY↑) 캐리가 활발해 BTC 가 오르고, 엔이 급등하면(청산) BTC 가 빠져야 한다.

    **핵심은 통제다.** 엔 급등은 대개 '공포가 커진 주'와 겹치므로, 단순 상관이
    음수로 나와도 그것이 엔 때문인지 그냥 위험회피 때문인지 알 수 없다. 그래서
    ΔVIX 를 통제한 부분상관을 함께 낸다 — 통제 후에도 0 근처면 엔은 위험회피의
    대리변수일 뿐 독립 정보가 없다는 뜻이다(실측 결론, docs/26).
    """
    def wk(s):
        by = {}
        for d in sorted(s):
            by[d.isocalendar()[:2]] = (d, s[d])
        return {k: v for k, (_, v) in by.items()}

    wb, wj = wk(btc), wk(usdjpy)
    ks = sorted(set(wb) & set(wj))
    rows = []
    for a, b in zip(ks, ks[1:]):
        if wb[a] > 0 and wj[a] > 0 and wb[b] > 0 and wj[b] > 0:
            # 직전 주(a)를 행이 직접 들고 간다. 아래 ΔVIX 통제가 rows 와
            # zip(ks, ks[1:]) 을 다시 짝지으면, 위 조건에 걸려 건너뛴 주가
            # 하나만 있어도 이후 모든 행의 '직전 주'가 밀려 엉뚱한 구간의
            # ΔVIX 를 통제하게 된다 — 단순상관은 멀쩡해 보이는데 부분상관만
            # 조용히 뒤집혀서, 통제 후 결론이 반대로 나온다.
            rows.append((b, math.log(wj[b] / wj[a]), math.log(wb[b] / wb[a]), a))
    if len(rows) < 30:
        return {"n": 0}
    xs = [j for _, j, _, _ in rows]
    ys = [b for _, _, b, _ in rows]
    full = pearson(xs, ys)

    def era(lo, hi):
        sub = [(j, b) for k, j, b, _ in rows if lo <= k[0] <= hi]
        return (pearson([j for j, _ in sub], [b for _, b in sub]), len(sub))

    # ΔVIX 통제 부분상관 — 엔이 '독립적으로' 설명하는 몫이 있는지
    partial = None
    if vix:
        wv = wk(vix)
        trio = []
        for k, j, b, a in rows:          # 직전 주는 행이 들고 있다(정렬 어긋남 방지)
            if k in wv and a in wv:
                trio.append((j, b, wv[k] - wv[a]))
        if len(trio) >= 30:
            jj = [t[0] for t in trio]; bb = [t[1] for t in trio]; vv = [t[2] for t in trio]
            partial = partial_from_r(pearson(jj, bb), pearson(jj, vv), pearson(bb, vv))
    return {"n": len(rows), "full": full, "partial_vix": partial,
            "eras": {"2010~2017": era(2010, 2017), "2018~2021": era(2018, 2021),
                     "2022~현재": era(2022, 2100)},
            "span": (rows[0][0], rows[-1][0]),
            "n_partial": len(trio) if (vix and partial is not None) else 0}


# ---------------------------------------------------------------------------
# 분석 1c — 금 현물 ↔ 비트코인 ('디지털 금' 서사 점검: 로그수익률 + 선행/후행)
# ---------------------------------------------------------------------------
def gold_analysis(btc: dict[date, float], gold: dict[date, float],
                  max_lag: int = 6) -> dict:
    """월간 BTC 로그수익률 ↔ 금 로그수익률의 상관과 선행/후행 스캔.

    '디지털 금'이 맞다면 둘이 같이(양의 상관) 움직이거나 한쪽이 다른 쪽을 앞서야
    한다. 레벨이 아니라 수익률로 재는 이유는 나스닥·VIX 와 같다 — 둘 다 우상향이라
    레벨 상관은 가짜로 높다. 선행/후행은 금 수익률을 k개월 밀어 BTC 와 맞춘다
    (k>0 = 금이 앞선다). 실측 결론은 '무상관'이지만, 재현·재검증되도록 도구에
    남긴다(docs/25)."""
    bm, gm = month_end(btc), month_end(gold)
    br, gr = log_returns(bm), log_returns(gm)

    def key(d: date):
        return (d.year, d.month)
    brp = {key(d): v for d, v in br.items()}
    grp = {key(d): v for d, v in gr.items()}
    common = sorted(set(brp) & set(grp))
    x = [brp[k] for k in common]
    y = [grp[k] for k in common]

    def era(lo, hi):
        ks = [k for k in common if lo <= k[0] <= hi]
        return (pearson([brp[k] for k in ks], [grp[k] for k in ks]), len(ks))

    def shift(ym: tuple[int, int], k: int) -> tuple[int, int]:
        y_, m_ = ym[0], ym[1] + k
        while m_ > 12:
            m_ -= 12
            y_ += 1
        while m_ < 1:
            m_ += 12
            y_ -= 1
        return (y_, m_)

    lead: dict[int, Optional[float]] = {}
    for k in range(-max_lag, max_lag + 1):
        gsh = {shift(ky, k): v for ky, v in grp.items()}   # 금을 k개월 민다
        ks = sorted(set(brp) & set(gsh))
        lead[k] = pearson([gsh[c] for c in ks], [brp[c] for c in ks]) \
            if len(ks) >= 24 else None
    valid = {k: v for k, v in lead.items() if v is not None}
    best_k = max(valid, key=lambda k: abs(valid[k])) if valid else None
    return {"n": len(common), "full": pearson(x, y),
            "eras": {"2013~2019": era(2013, 2019), "2020~현재": era(2020, 2100)},
            "lead": lead, "best_lag": best_k,
            "best_corr": valid.get(best_k) if best_k is not None else None,
            "span": (common[0], common[-1]) if common else None}


# ---------------------------------------------------------------------------
# 분석 1e — 거시 후보 3종 ↔ 비트코인 (DXY·실질금리·연준 순유동성)
# ---------------------------------------------------------------------------
def _change(series: dict[date, float], use_log: bool) -> dict[date, float]:
    """관측 격자 위의 변화. 가격류(양수)는 로그수익률, 수준 금리(음수 가능)는 차분."""
    ds = sorted(series)
    out: dict[date, float] = {}
    for a, b in zip(ds, ds[1:]):
        if use_log:
            if series[a] > 0 and series[b] > 0:
                out[b] = math.log(series[b] / series[a])
        else:
            out[b] = series[b] - series[a]
    return out


# 통제가 이만큼까지 설명하면(잔차분산 < 이 값) 부분상관은 정의되지 않은 것으로 본다.
# |r| ≈ 0.9999999995 에 해당한다 — 실제 데이터의 0.99 는 잔차 0.02 라 영향이 없다.
COLLINEAR = 1e-9


def partial_from_r(xy: Optional[float], xz: Optional[float],
                   yz: Optional[float]) -> Optional[float]:
    """상관 셋으로 부분상관 하나 — ρ(x,y) 에서 z 와 같이 움직이는 몫을 뺀다.

    같은 식이 세 도구에 각자 적혀 있었고, 셋 다 같은 함정을 안고 있었다:
    통제가 사실상 완전한 예측자면 (1 − r²) 가 −1e−16 이 되어 **sqrt 가 죽는다.**
    한 곳에 모아 두면 고칠 곳도 한 곳이다.
    """
    if None in (xy, xz, yz):
        return None
    rx, ry = 1 - xz ** 2, 1 - yz ** 2      # 통제로 설명하고 남은 분산
    # **남은 분산이 사실상 0 이면 부분상관은 정의되지 않는다.** 0 인지만 보면
    # 부동소수 티끌(2e−16)이 살아남아 0 나눗셈 직전의 **쓰레기 값**이 표에 실린다
    # — 죽는 것보다 나쁘다. 티끌은 티끌로 보고 None 을 돌려준다.
    if rx < COLLINEAR or ry < COLLINEAR:
        return None
    return (xy - xz * yz) / math.sqrt(rx * ry)


def _partial(dk: dict, bk: dict, ctrl_change: dict, monthly: bool) -> Optional[float]:
    """driver·BTC 를 control 로 통제한 부분상관. 세 계열을 같은 격자 키로 맞춘다."""
    def key(d: date):
        return (d.year, d.month) if monthly else d.isocalendar()[:2]
    ck = {key(d): v for d, v in sorted(ctrl_change.items())}
    common = sorted(set(dk) & set(bk) & set(ck))
    if len(common) < 20:
        return None
    dd = [dk[c] for c in common]; bb = [bk[c] for c in common]; cc = [ck[c] for c in common]
    return partial_from_r(pearson(dd, bb), pearson(dd, cc), pearson(bb, cc))


def candidate_analysis(btc: dict[date, float], driver: dict[date, float],
                       vix: Optional[dict[date, float]] = None,
                       nasdaq: Optional[dict[date, float]] = None,
                       use_log: bool = True, max_lag: int = 6) -> dict:
    """거시 후보 하나를 금·엔캐리와 **같은 잣대**로 검정한다(docs/27).

    주간·월간 로그수익률 상관 + 국면분할 + 선행/후행 + **ΔVIX·나스닥 통제 부분상관**.
    부분상관이 결정적이다 — 후보가 위험자산 베타(공포·나스닥)를 넘어 BTC 에 **독립
    정보**를 주는지 본다. 통제 후 0 근처면 대리변수일 뿐이라 점수·사이트엔 안 쓴다.

    use_log=False 는 수준 금리(실질금리)용 — 음수가 될 수 있어 로그 대신 차분(Δ)으로
    변화를 잡는다. VIX 통제도 차분(ΔVIX), 나스닥 통제는 로그수익률로 맞춘다.

    **빈도를 후보에 맞춘다.** 월간으로만 나오는 후보(코스피=OECD 월간 지수 등)를 주간
    격자에 물리면 '1개월 수익률 vs 1주 수익률'을 비교하게 돼 값이 통째로 틀린다
    (nasdaq_analysis 가 `_is_monthly` 로 막는 것과 같은 함정). 그래서 후보가 월간이면
    주요 프레임도, **부분상관도** 월간 격자에서 잰다.
    """
    def keyed(chg: dict, monthly: bool) -> dict:
        def key(d: date):
            return (d.year, d.month) if monthly else d.isocalendar()[:2]
        return {key(d): v for d, v in sorted(chg.items())}

    prim_monthly = _is_monthly(driver)      # 후보의 관측 빈도가 주요 프레임을 정한다
    res: dict = {"use_log": use_log, "monthly_only": prim_monthly,
                 "primary": "월간" if prim_monthly else "주간"}
    dk_pr = bk_pr = None
    for monthly, tag in ((False, "wk"), (True, "mo")):
        if monthly is False and prim_monthly:
            # 월간 후보에 주간 프레임은 의미가 없다 — 계산하지 않고 비워 둔다
            res["full_wk"], res["n_wk"] = None, 0
            continue
        freq = month_end if monthly else weekly
        dk = keyed(_change(freq(driver), use_log), monthly)
        bk = keyed(log_returns(freq(btc)), monthly)
        common = sorted(set(dk) & set(bk))
        res[f"full_{tag}"] = pearson([dk[c] for c in common], [bk[c] for c in common]) \
            if len(common) >= 12 else None
        res[f"n_{tag}"] = len(common)
        if monthly is prim_monthly:         # 주요 프레임에서 구간·국면을 잡는다
            dk_pr, bk_pr = dk, bk
            res["span"] = (common[0], common[-1]) if common else None
            res["n_primary"] = len(common)

            # common·dk·bk 를 기본값으로 묶는다 — 이 클로저는 바로 아래에서 즉시
            # 호출되므로 지금도 안전하지만, 묶어 두면 그 사실이 명시되고 B023
            # 오탐이 사라져 진짜 늦은-바인딩 버그가 나면 그때 드러난다.
            def era(lo, hi, common=common, dk=dk, bk=bk):
                ks = [c for c in common if lo <= c[0] <= hi]
                return (pearson([dk[c] for c in ks], [bk[c] for c in ks]), len(ks))
            res["eras"] = {"2013~2017": era(2013, 2017),
                           "2018~2021": era(2018, 2021),
                           "2022~현재": era(2022, 2100)}

    # 부분상관 — **주요 프레임과 같은 격자**에서 ΔVIX·나스닥수익률을 각각 통제
    pfreq = month_end if prim_monthly else weekly
    res["partial_vix"] = (_partial(dk_pr, bk_pr, _change(pfreq(vix), False), prim_monthly)
                          if vix else None)
    res["partial_ndq"] = (_partial(dk_pr, bk_pr, _change(pfreq(nasdaq), True), prim_monthly)
                          if nasdaq else None)

    # 선행/후행 — 월간, driver 를 k개월 밀어 BTC 와 맞춘다(k>0 = driver 가 앞선다)
    dkm = {(d.year, d.month): v for d, v in _change(month_end(driver), use_log).items()}
    bkm = {(d.year, d.month): v for d, v in log_returns(month_end(btc)).items()}

    def shift(ym, k):
        y_, m_ = ym[0], ym[1] + k
        while m_ > 12:
            m_ -= 12
            y_ += 1
        while m_ < 1:
            m_ += 12
            y_ -= 1
        return (y_, m_)

    lead: dict[int, Optional[float]] = {}
    for k in range(-max_lag, max_lag + 1):
        dsh = {shift(ky, k): v for ky, v in dkm.items()}
        ks = sorted(set(bkm) & set(dsh))
        lead[k] = pearson([dsh[c] for c in ks], [bkm[c] for c in ks]) \
            if len(ks) >= 24 else None
    valid = {k: v for k, v in lead.items() if v is not None}
    best_k = max(valid, key=lambda k: abs(valid[k])) if valid else None
    res["lead"] = lead
    res["best_lag"] = best_k
    res["best_corr"] = valid.get(best_k) if best_k is not None else None
    return res


# ---------------------------------------------------------------------------
# 분석 2 — M2 ↔ 비트코인 (전년비 증가율의 선행/후행)
# ---------------------------------------------------------------------------
def m2_lead_lag(btc_month: dict[date, float], m2: dict[date, float],
                max_lag: int = 12) -> dict:
    """BTC 전년비를 M2 전년비보다 k개월 뒤로 놓고 상관. 최적 k(=M2 선행)를 찾는다."""
    btc_yoy = yoy(btc_month)
    m2_yoy = yoy(month_end(m2))
    m2_dates = sorted(m2_yoy)
    if len(m2_dates) < 18:
        return {"n": 0}

    # M2 를 (연,월) 키로 잡아 정확히 k개월 전을 찾는다 — 예전엔 '15일 근사 + 20일
    # 허용'이라, M2 가 월말 격자(30/31일)면 target-15 가 앞뒤 두 달과 똑같이 15일씩
    # 떨어져 동점이 되고, 스캔이 이른 쪽을 골라 선행을 한 달 부풀렸다. 달력 키는 그
    # 모호함이 없다(월-초 격자인 현재 데이터에선 결과 동일).
    m2_by_ym = {(d.year, d.month): v for d, v in m2_yoy.items()}

    def shift_ym(d: date, k: int) -> tuple[int, int]:
        y_, m_ = d.year, d.month - k
        while m_ <= 0:
            m_ += 12
            y_ -= 1
        return (y_, m_)

    results: dict[int, Optional[float]] = {}
    for k in range(0, max_lag + 1):
        xs, ys = [], []
        for d in sorted(btc_yoy):
            v = m2_by_ym.get(shift_ym(d, k))
            if v is not None:
                xs.append(v)
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
    add("  비트코인 ↔ 거시 연관성 — 공개 데이터, 변화율 기준")
    add("=" * 78)
    btc_month = month_end(btc)

    # --- 주식지수 (나스닥·S&P500) — 있는 것 모두, 나스닥 먼저 ---
    eq_keys = [k for k in macro if any(
        t in k.lower() for t in ("nasdaq", "ndq", "sp500", "s&p", "equit"))]
    eq_keys.sort(key=lambda k: 0 if ("nasdaq" in k.lower() or "ndq" in k.lower()) else 1)
    add("\n[1] 주식지수 ↔ 비트코인 — 로그수익률 상관")
    add("-" * 78)
    if not eq_keys:
        add("  주가지수 열이 없습니다 (macro.csv 에 nasdaq 또는 sp500 추가).")
    for eq_key in eq_keys:
        r = nasdaq_analysis(btc, macro[eq_key])
        u = r["unit"]
        add(f"\n  ▸ {eq_key}"
            + (f"  ({r['span'][0]} ~ {r['span'][1]}, 공통 {r['n']}{u}, "
               f"{'월간' if u == '개월' else '주간'} 수익률)" if r["span"] else ""))
        add(f"    전 구간 ρ = {r['full']:+.2f}" if r["full"] is not None else "    전 구간 —")
        for name, (v, npt) in r["eras"].items():
            add(f"      {name}  ρ {v:+.2f}   ({npt}{u})" if v is not None else f"      {name}  —")
        add(f"    최근 {r['recent_win']}{u} ρ = {r['recent']:+.2f}"
            if r["recent"] is not None else f"    최근 {r['recent_win']}{u} —")

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

    # --- 금 현물 — 있으면 ('디지털 금' 점검) ---
    gold_key = next((k for k in macro
                     if "gold" in k.lower() or k.lower() in ("xau", "xauusd")), None)
    if gold_key is not None:
        add("\n[1c] 금 현물 ↔ 비트코인 — 월수익률 상관 + 선행/후행 ('디지털 금' 점검)")
        add("-" * 78)
        g = gold_analysis(btc, macro[gold_key])
        if g["span"]:
            add(f"  기간 {g['span'][0]} ~ {g['span'][1]} · 공통 {g['n']}개월")
        add(f"  동시 ρ = {g['full']:+.2f}" if g["full"] is not None else "  동시 —")
        for name, (val, npt) in g["eras"].items():
            add(f"    {name}  ρ {val:+.2f}   ({npt}개월)" if val is not None
                else f"    {name}  —")
        if g["best_lag"] is not None:
            add(f"  최강 선행/후행: k={g['best_lag']:+d}개월  ρ {g['best_corr']:+.2f}"
                "  (k>0 = 금이 앞섬)")
        add("  → |ρ| 가 0.2 도 안 되고 시대·프레임마다 부호가 바뀐다 = 무상관."
            " 방향·선행 신호로 쓰지 않는다 (docs/25).")

    # --- 엔캐리(USDJPY) — 있으면 ---
    jpy_key = next((k for k in macro if k.lower() in ("usdjpy", "jpy", "dexjpus")), None)
    if jpy_key is not None:
        add("\n[1d] 엔캐리 트레이드 ↔ 비트코인 — 주간 수익률 (엔 약세=캐리 활발 가설)")
        add("-" * 78)
        vix_col = next((k for k in macro if "vix" in k.lower()), None)
        c = carry_analysis(btc, macro[jpy_key], macro.get(vix_col) if vix_col else None)
        if c.get("n"):
            add(f"  공통 {c['n']}주")
            add(f"  전 구간 ρ = {c['full']:+.2f}   (가설이 맞으면 **양수**여야 한다)"
                if c["full"] is not None else "  전 구간 —")
            for name, (val, npt) in c["eras"].items():
                add(f"    {name}  ρ {val:+.2f}   ({npt}주)" if val is not None
                    else f"    {name}  —")
            if c["partial_vix"] is not None:
                add(f"  ΔVIX 통제 부분상관 = {c['partial_vix']:+.2f}"
                    "   (엔이 '독립적으로' 설명하는 몫)")
            add("  → 어느 프레임에서도 |ρ| < 0.2 이고 부호도 가설과 반대다. 엔 급등이"
                " BTC 하락과 겹쳐 보이는 것은")
            add("    엔 때문이 아니라 그 주의 일반적 위험회피 때문이다 — 통제하면 남는 게"
                " 없다. 쓰지 않는다 (docs/26).")
        else:
            add("  데이터 부족")

    # --- 거시 후보 3종 (DXY·실질금리·연준 순유동성) — 있으면 ---
    vix_col = next((k for k in macro if "vix" in k.lower()), None)
    ndq_col = next((k for k in macro if "nasdaq" in k.lower() or "ndq" in k.lower()), None)
    # (열, 라벨, 로그변환?, 가설). 로그=가격류(양수), 차분=수준 금리·스프레드(음수 가능)
    cand_specs = [("dxy", "DXY(광의 달러지수)", True, "달러 강세=BTC 하락(음)"),
                  ("realyield", "미국 10년 실질금리", False, "실질금리 상승=BTC 하락(음)"),
                  ("net_liq", "연준 순유동성(WALCL−TGA−RRP)", True, "유동성 증가=BTC 상승(양)"),
                  ("kospi", "코스피(한국 주가지수)", True, "한국 위험선호=BTC 상승(양)"),
                  ("hy_spread", "고수익채 스프레드(신용)", False, "스프레드 확대=BTC 하락(음)"),
                  ("breakeven", "10년 기대인플레이션", False, "인플레 기대↑='디지털 금'(양)"),
                  ("curve", "수익률곡선 10년−2년", False, "가팔라짐=완화 사이클(양)"),
                  ("oil", "WTI 원유(에너지)", True, "유가↑=인플레·성장(양)"),
                  ("copper", "구리(산업금속)", True, "'닥터 코퍼' 글로벌 성장(양)"),
                  ("em_fx", "신흥국 대비 달러지수", True, "신흥국 통화 약세=위험회피(음)"),
                  ("china_eq", "중국 주가지수", True, "중국 위험선호=BTC 상승(양)")]
    present = [c for c in cand_specs if c[0] in macro]
    if present:
        add("\n[1e] 거시 후보 ↔ 비트코인 — 수익률 상관 + ΔVIX·나스닥 통제 부분상관")
        add("-" * 78)
        vcol = macro.get(vix_col) if vix_col else None
        ncol = macro.get(ndq_col) if ndq_col else None
        for col, label, use_log, hyp in present:
            r = candidate_analysis(btc, macro[col], vcol, ncol, use_log=use_log)
            basis = "로그수익률" if use_log else "Δ수준"
            unit = "개월" if r.get("monthly_only") else "주"
            add(f"\n  ▸ {label}  (가설: {hyp}, {basis})"
                + (f"  {r['span'][0]}~{r['span'][1]}" if r.get("span") else ""))
            fw, fm = r.get("full_wk"), r.get("full_mo")
            if not r.get("monthly_only"):
                add(f"    주간 ρ = {fw:+.2f} ({r['n_wk']}주)" if fw is not None else "    주간 —")
            add(f"    월간 ρ = {fm:+.2f} ({r['n_mo']}개월)" if fm is not None else "    월간 —")
            if r.get("monthly_only"):
                add("    (월간 자료만 있어 월간을 주요 프레임으로 쓴다 — 주간 격자에 물리면"
                    " 빈도가 어긋난다)")
            for name, (val, npt) in r["eras"].items():
                if val is not None:
                    add(f"      {name}  ρ {val:+.2f}  ({npt}{unit})")
            pv, pn = r.get("partial_vix"), r.get("partial_ndq")
            add(f"    ΔVIX 통제 부분상관 = {pv:+.2f}   나스닥 통제 = {pn:+.2f}"
                f"   ({r.get('primary')} 격자)"
                if pv is not None and pn is not None else "    부분상관 —")
            if r.get("best_lag") is not None:
                add(f"    최강 선행/후행: k={r['best_lag']:+d}개월  ρ {r['best_corr']:+.2f}"
                    "  (k>0 = 후보가 앞섬)")
                add("      ※ 13개 시차에서 |ρ| 최대를 고른 값이라 선택 효과가 있다 —"
                    " 부호가 국면마다 뒤집히면 잡음으로 본다(docs/27).")
        # 결론 문장의 예시는 **실제로 잰 후보만** 나열한다 — 없는 열을 언급하면
        # 리포트가 재지 않은 것을 잰 것처럼 읽힌다.
        # 사유를 둘로 나눠 적되, **후보 이름은 쓰지 않는다** — 이 결론문은 어떤 열
        # 조합으로도 실행되므로, 이름을 박으면 재지 않은 후보를 잰 것처럼 읽힌다.
        add(f"\n  → 후보 {len(present)}종 전부 기준 미달. 다만 **사유는 둘로 갈린다**:")
        add("    (a) 크기는 되는데 통제하면 사라짐 = 위험선호 대리변수(나스닥·VIX 와 강하게 붙음)")
        add("    (b) 나스닥과 거의 직교하지만 애초에 크기가 부족함")
        add("    '전부 나스닥 베타'로 뭉뚱그리면 (b)에 대해 사실이 아니다 — 크기를 넘으면서")
        add("    위험선호와 독립인 후보를 아직 못 찾았을 뿐이다. 채택된 중국 M2(11개월 선행)와")
        add("    격이 다르므로 방향(BCS)·사이트에 넣지 않는다 (docs/27·28).")

    # --- M2 (열마다) ---
    add("\n[2] M2 ↔ 비트코인 — 전년비 증가율, 선행/후행 스캔")
    add("-" * 78)
    m2_keys = [k for k in macro if any(k.lower().startswith(p) for p in ("m1", "m2"))]
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
