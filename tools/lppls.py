#!/usr/bin/env python3
"""LPPLS — 로그주기 파워로우 특이점 모형. 표시 전용 후보로 검정한다.

    python3 tools/lppls.py --csv data/market.csv
    python3 tools/lppls.py --csv data/market.csv --stride 7 --out reports/lppls.md

## 이게 뭔가

Sornette 의 거품 모형이다. 가격이 **지수보다 빠르게** 오르면서(파워로우 발산)
그 위에 **점점 빨라지는 진동**이 얹히면, 그건 "남들이 사니까 나도 산다"는
되먹임이 붙었다는 뜻이고 임계 시점 tc 에서 그 구조가 끝난다고 본다.

    ln p(t) = A + B(tc−t)^m + C(tc−t)^m · cos(ω·ln(tc−t) − φ)
              └─ 추세 ─┘   └── 파워로우 발산 ──┘   └── 로그주기 진동 ──┘

**이 시스템이 관심을 갖는 이유는 하나다.** 기존 지표들은 전부 *진폭*을 읽는다 —
Puell 은 발행액이 평균 대비 몇 배인지, Pi Cycle 은 이동평균이 몇 배 벌어졌는지.
그런데 사이클마다 진폭이 줄어들어서 고점 탐지력이 무너지고 있다
(Puell 네 고점 퍼센타일 +0.99 → +1.00 → +0.46 → −0.00, docs/11).

LPPLS 는 **진동수 ω** 를 읽는다. 진폭이 절반이 되어도 진동수는 그대로다.
즉 진폭 감쇠에 **구조적으로 면역**이다. 그래서 docs/16 이 미심사 후보 중
가장 유망하다고 적어 뒀다.

## 왜 점수가 아니라 표시 전용인가

파라미터가 7개다(tc, m, ω, A, B, C, φ). docs/14 에서 이미 자유도가 269개인데
여기에 7개를 얹으면서 "과적합을 줄였다"고 말할 수는 없다. 게다가 tc 는
**미래 시점**이라 그 자체가 예측이다 — 이 시스템은 예측이 아니라 위치를 말한다.

그래서 처음부터 표시 전용 후보로 검정한다. 통과해도 BCS 에는 안 들어간다.

## 무엇을 출력하는가 — 신뢰도 지표

한 번의 적합은 믿을 게 못 된다. 창을 조금만 바꿔도 tc 가 몇 달씩 움직인다.
그래서 Sornette 가 쓰는 방식을 그대로 쓴다: **창 길이를 바꿔가며 여러 번
적합하고, 필터를 통과한 창의 비율**을 낸다.

    양의 거품 신뢰도 = (필터 통과 & B<0 인 창) / 전체 창    → 위로 터질 구조
    음의 거품 신뢰도 = (필터 통과 & B>0 인 창) / 전체 창    → 아래로 터질 구조

필터는 문헌 표준이다(m·ω 범위, 감쇠 조건, 진동 횟수, tc 위치).

## 계산 방법 — 표준 라이브러리만

(tc, m, ω) 셋을 격자로 훑고, 그 안에서 (A, B, C1, C2) 는 **선형 최소제곱**으로
닫힌 해를 구한다. 비선형 최적화기를 쓰지 않는 것은 의존성 때문만이 아니라,
격자가 **재현 가능**하기 때문이다 — 최적화기는 초기값에 따라 다른 답을 낸다.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from btc_core.datasources.csv_source import load_csv_bundle    # noqa: E402

# --- 격자 --------------------------------------------------------------------
# 문헌 표준 범위 안에서만 훑는다. 넓히면 아무 데이터에나 뭔가는 맞는다.
M_GRID = tuple(round(0.10 + 0.085 * k, 3) for k in range(11))    # 0.10 ~ 0.95
W_GRID = tuple(float(4 + k) for k in range(12))                  # 4 ~ 15
TC_FRACS = tuple(round(0.005 + 0.022 * k, 4) for k in range(10))  # 창 길이 대비 tc 여유

# 창 길이(일). 짧은 창은 노이즈에, 긴 창은 사이클 경계에 걸린다.
WINDOWS = (270, 360, 450, 540, 720)
SAMPLE_STRIDE = 5        # 창 안에서 5일에 한 점만. LPPLS 는 매끄러운 곡선이라 충분하다.

# --- 필터 (Sornette / Filimonov 표준) ----------------------------------------
# m 은 0<m<1 이어야 특이점이 존재한다. 1 에 붙으면 발산이 사라져 그냥 추세다.
M_RANGE = (0.05, 0.95)
W_RANGE = (4.0, 15.0)
DAMPING_MIN = 0.8        # m|B| / (ω√(C1²+C2²)) — 진동이 추세를 삼키지 않을 것
OSC_MIN = 2.5            # 창 안에서 진동이 최소 이만큼은 돌 것

# 로그 잔차 RMSE. 로그 공간의 0.10 은 가격 기준 약 10% 오차다.
#
# 문헌 표준은 5% 인데 그 값은 주가지수 기준이다. 비트코인은 같은 창에서 변동성이
# 몇 배라 5% 로 두면 **16년 전체에서 통과하는 창이 하나도 없다**(실측). 그래서
# 10% 로 두고, 이 완화가 결론을 만들지 않는지 5%·15% 로도 재본다.
REL_ERR_MAX = 0.10


@dataclass(frozen=True)
class Fit:
    tc: float            # 임계 시점 (창 끝 이후 경과일)
    m: float
    w: float
    A: float
    B: float
    C1: float
    C2: float
    rss: float
    rel_err: float
    n: int
    window: int

    @property
    def C(self) -> float:
        return math.hypot(self.C1, self.C2)

    @property
    def damping(self) -> float:
        d = self.w * self.C
        return (self.m * abs(self.B) / d) if d > 0 else float("inf")

    def oscillations(self, t1: float, t2: float) -> float:
        """창 [t1, t2] 안에서 로그주기 진동이 몇 번 도는가."""
        a, b = self.tc - t1, self.tc - t2
        if a <= 0 or b <= 0:
            return 0.0
        return self.w / (2 * math.pi) * math.log(a / b)


def _solve4(ata: list[list[float]], atb: list[float]) -> Optional[list[float]]:
    """4×4 정규방정식을 부분 피벗 가우스 소거로 푼다."""
    n = 4
    M = [row[:] + [atb[i]] for i, row in enumerate(ata)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None                     # 특이행렬 — 이 (tc,m,ω) 는 버린다
        M[col], M[piv] = M[piv], M[col]
        p = M[col][col]
        for r in range(col + 1, n):
            f = M[r][col] / p
            if f:
                for c in range(col, n + 1):
                    M[r][c] -= f * M[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = M[r][n] - sum(M[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / M[r][r]
    return x


def fit_linear(ts: Sequence[float], ys: Sequence[float],
               tc: float, m: float, w: float) -> Optional[Fit]:
    """(tc, m, ω) 고정 시 (A, B, C1, C2) 의 최소제곱 해.

    ln p = A + B·f + C1·g + C2·h
        f = (tc−t)^m,  g = f·cos(ω ln(tc−t)),  h = f·sin(ω ln(tc−t))

    이렇게 두면 φ 가 C1, C2 안으로 흡수되어 선형이 된다 — 7개 파라미터 중
    4개가 닫힌 해로 풀리고 격자로 훑을 것은 3개만 남는다.
    """
    n = len(ts)
    if n < 20:
        return None

    # 설계행렬을 만들지 않고 곱합만 누적한다 (메모리·속도).
    S = [[0.0] * 4 for _ in range(4)]
    b = [0.0] * 4
    for i in range(n):
        dt = tc - ts[i]
        if dt <= 1e-9:
            return None
        f = dt ** m
        lg = w * math.log(dt)
        g, h = f * math.cos(lg), f * math.sin(lg)
        row = (1.0, f, g, h)
        y = ys[i]
        for r in range(4):
            b[r] += row[r] * y
            for c in range(r, 4):
                S[r][c] += row[r] * row[c]
    for r in range(4):
        for c in range(r):
            S[r][c] = S[c][r]

    sol = _solve4(S, b)
    if sol is None:
        return None
    A, B, C1, C2 = sol
    if not all(math.isfinite(v) for v in sol):
        return None

    rss = 0.0
    for i in range(n):
        dt = tc - ts[i]
        f = dt ** m
        lg = w * math.log(dt)
        pred = A + B * f + C1 * f * math.cos(lg) + C2 * f * math.sin(lg)
        rss += (ys[i] - pred) ** 2

    # 로그 잔차 RMSE. exp 하면 곧 가격의 상대오차라 해석이 바로 된다.
    return Fit(tc, m, w, A, B, C1, C2, rss, math.sqrt(rss / n), n, 0)


def best_fit(ts: Sequence[float], ys: Sequence[float], t2: float,
             window: int, *, validate: bool = True,
             rel_err_max: float = REL_ERR_MAX) -> Optional[Fit]:
    """격자를 훑어 **필터를 통과하는 것 중** 잔차가 가장 작은 적합.

    필터를 나중에 거는 것과 순서가 다르고, 그 차이가 크다. 최소잔차 하나를
    고른 뒤 필터를 걸면 "잔차는 제일 작지만 거품 구조는 아닌" 해가 뽑혀서
    창 전체가 탈락한다. 실제로 그렇게 짜 봤더니 16년 전 구간에서 통과가
    **하나도 없었다.** 물어야 할 것은 "최적해가 거품인가"가 아니라
    **"거품 구조로 이 창을 설명할 수 있는가"** 다.
    """
    best: Optional[Fit] = None
    for frac in TC_FRACS:
        tc = t2 + window * frac
        for m in M_GRID:
            for w in W_GRID:
                f = fit_linear(ts, ys, tc, m, w)
                if f is None:
                    continue
                if validate and not passes(f, ts[0], t2, rel_err_max=rel_err_max):
                    continue
                if best is None or f.rss < best.rss:
                    best = Fit(f.tc, f.m, f.w, f.A, f.B, f.C1, f.C2,
                               f.rss, f.rel_err, f.n, window)
    return best


def passes(f: Fit, t1: float, t2: float, *,
           rel_err_max: float = REL_ERR_MAX) -> bool:
    """문헌 표준 필터. 통과 못 하면 '거품 구조가 아니다'로 센다."""
    if not (M_RANGE[0] <= f.m <= M_RANGE[1]):
        return False
    if not (W_RANGE[0] <= f.w <= W_RANGE[1]):
        return False
    if f.tc <= t2:
        return False
    if f.rel_err > rel_err_max:
        return False
    if f.damping < DAMPING_MIN:
        return False
    if f.oscillations(t1, t2) < OSC_MIN:
        return False
    return True


def confidence_at(days: Sequence[int], logp: Sequence[float],
                  end_idx: int, *,
                  rel_err_max: float = REL_ERR_MAX
                  ) -> tuple[float, float, Optional[Fit]]:
    """end_idx 시점의 (양의 거품 신뢰도, 음의 거품 신뢰도, 대표 적합).

    창 길이를 바꿔가며 적합하고 **필터를 통과한 창의 비율**을 센다.
    한 번의 적합은 창을 조금만 바꿔도 tc 가 몇 달씩 움직여서 믿을 게 못 된다.
    """
    t2 = float(days[end_idx])
    pos = neg = tried = 0
    rep: Optional[Fit] = None

    for window in WINDOWS:
        start = end_idx - window
        if start < 0:
            continue
        tried += 1
        idx = range(start, end_idx + 1, SAMPLE_STRIDE)
        ts = [float(days[i]) for i in idx]
        ys = [logp[i] for i in idx]
        if len(ts) < 20:
            continue
        f = best_fit(ts, ys, t2, window, rel_err_max=rel_err_max)
        if f is None:
            continue
        if f.B < 0:
            pos += 1
            if rep is None or f.rss < rep.rss:
                rep = f
        else:
            neg += 1
    if not tried:
        return 0.0, 0.0, None
    return pos / tried, neg / tried, rep


def build_series(dates: Sequence[date], prices: Sequence[float],
                 stride: int = 14, *, rel_err_max: float = REL_ERR_MAX,
                 progress: bool = False
                 ) -> list[tuple[date, float, float, Optional[Fit]]]:
    """전 구간에 걸쳐 신뢰도를 계산한다. stride 일마다 한 번."""
    day0 = dates[0]
    days = [(d - day0).days for d in dates]
    logp = [math.log(max(1e-9, p)) for p in prices]

    out = []
    start = max(WINDOWS)
    idxs = list(range(start, len(dates), stride))
    for k, i in enumerate(idxs):
        pos, neg, rep = confidence_at(days, logp, i, rel_err_max=rel_err_max)
        out.append((dates[i], pos, neg, rep))
        if progress and k % 20 == 0:
            print(f"  {k}/{len(idxs)}  {dates[i]}  양 {pos:.2f} 음 {neg:.2f}",
                  file=sys.stderr, flush=True)
    return out


# ---------------------------------------------------------------------------
# 검정
# ---------------------------------------------------------------------------

TOPS = (date(2013, 11, 30), date(2017, 12, 17), date(2021, 11, 10), date(2025, 10, 6))
BOTTOMS = (date(2015, 1, 14), date(2018, 12, 15), date(2022, 11, 21), date(2026, 6, 6))
NEAR = 45


def _near(lookup: dict[date, float], target: date, span: int = NEAR) -> Optional[float]:
    best = None
    for off in range(span + 1):
        for d in (target - timedelta(days=off), target + timedelta(days=off)):
            if d in lookup:
                return lookup[d]
    return best


def sign_scan(days, logp, end_idx) -> tuple[Optional[float], Optional[float]]:
    """오차 필터를 빼고, 부호별로 **달성 가능한 최소 로그잔차 RMSE**를 돌려준다.

    임계값 하나를 정해 놓고 통과/탈락만 보면 "왜 탈락했는지"가 사라진다.
    달성 가능한 최소 오차를 그대로 보면 임계를 나중에 바꿔가며 재볼 수 있고,
    **한 임계로 네 사이클이 일관되는지**를 물을 수 있다.
    """
    t2 = float(days[end_idx])
    mp = mn = None
    for window in WINDOWS:
        s = end_idx - window
        if s < 0:
            continue
        idx = range(s, end_idx + 1, SAMPLE_STRIDE)
        ts = [float(days[i]) for i in idx]
        ys = [logp[i] for i in idx]
        if len(ts) < 20:
            continue
        for frac in TC_FRACS:
            tc = t2 + window * frac
            for m in M_GRID:
                for w in W_GRID:
                    f = fit_linear(ts, ys, tc, m, w)
                    if f is None:
                        continue
                    if not (M_RANGE[0] <= f.m <= M_RANGE[1]):
                        continue
                    if not (W_RANGE[0] <= f.w <= W_RANGE[1]):
                        continue
                    if f.tc <= t2 or f.damping < DAMPING_MIN:
                        continue
                    if f.oscillations(ts[0], t2) < OSC_MIN:
                        continue
                    if f.B < 0:
                        mp = f.rel_err if mp is None else min(mp, f.rel_err)
                    else:
                        mn = f.rel_err if mn is None else min(mn, f.rel_err)
    return mp, mn


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LPPLS 신뢰도 검정 (표시 전용 후보)")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--stride", type=int, default=21, help="며칠마다 계산할지")
    ap.add_argument("--out", default=None)
    ap.add_argument("--quick", action="store_true", help="전환점만 (전 구간 생략)")
    args = ap.parse_args(argv)

    bundle = load_csv_bundle(args.csv)
    price = bundle.market.normalized().price
    dates = [d for d, v in zip(price.dates, price.values) if v]
    prices = [v for v in price.values if v]
    day0 = dates[0]
    days = [(d - day0).days for d in dates]
    logp = [math.log(max(1e-9, v)) for v in prices]
    idx_of = {d: i for i, d in enumerate(dates)}

    L: list[str] = []
    add = L.append
    add("# LPPLS 신뢰도 — 표시 전용 후보 검정")
    add("")
    add(f"기간 {dates[0]} ~ {dates[-1]} · {len(dates)}일")
    add(f"창 {WINDOWS} · 격자 tc {len(TC_FRACS)} × m {len(M_GRID)} × ω {len(W_GRID)}"
        f" = {len(TC_FRACS)*len(M_GRID)*len(W_GRID)}")
    add("")

    # --- 1. 전환점에서 달성 가능한 최소 오차 --------------------------------
    add("## 1. 전환점에서 거품 구조가 얼마나 잘 맞는가")
    add("")
    add("오차 필터를 빼고 **부호별 달성 가능한 최소 로그잔차 RMSE** 를 본다.")
    add("작을수록 그 방향의 거품 구조로 잘 설명된다는 뜻이고, `—` 는 그 방향으로는")
    add("문헌 필터(m·ω 범위, 감쇠, 진동 횟수)를 통과하는 적합이 아예 없다는 뜻이다.")
    add("")
    add("| 국면 | 날짜 | 가격 | 양의 거품 | 음의 거품 | 방향 |")
    add("|---|---|---:|---:|---:|---|")
    tp_rows = []
    for label, group in (("고점", TOPS), ("저점", BOTTOMS)):
        for tp in group:
            i = idx_of.get(tp)
            if i is None:
                add(f"| {label} | {tp} | — | — | — | 데이터 없음 |")
                continue
            mp, mn = sign_scan(days, logp, i)
            tp_rows.append((label, tp, mp, mn))
            fp = f"{mp:.3f}" if mp is not None else "—"
            fn = f"{mn:.3f}" if mn is not None else "—"
            if mp is not None and (mn is None or mp < mn):
                arrow = "양(위로)" + (" ✓" if label == "고점" else " ✗")
            elif mn is not None:
                arrow = "음(아래로)" + (" ✓" if label == "저점" else " ✗")
            else:
                arrow = "없음"
            add(f"| {label} | {tp} | ${prices[i]:,.0f} | {fp} | {fn} | {arrow} |")
    add("")

    right = sum(1 for lab, _, mp, mn in tp_rows
                if (lab == "고점" and mp is not None and (mn is None or mp < mn))
                or (lab == "저점" and mn is not None and (mp is None or mn < mp)))
    add(f"**방향 정확도 {right}/{len(tp_rows)}.**")
    add("")

    # --- 2. 임계값 민감도 -----------------------------------------------------
    add("## 2. 한 임계값으로 네 사이클이 일관되는가")
    add("")
    THRESH = (0.05, 0.10, 0.15, 0.20, 0.30)
    add("| 국면 | 날짜 | " + " | ".join(f"≤{t*100:.0f}%" for t in THRESH) + " |")
    add("|---|---|" + "---:|" * len(THRESH))
    for lab, tp, mp, mn in tp_rows:
        cells = []
        for th in THRESH:
            hit = (mp is not None and mp <= th) if lab == "고점" else (mn is not None and mn <= th)
            cells.append("O" if hit else "·")
        add(f"| {lab} | {tp} | " + " | ".join(cells) + " |")
    add("")

    if args.quick:
        text = "\n".join(L)
        print(text)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        return 0

    # --- 3. 전 구간 기저율 ---------------------------------------------------
    add("## 3. 전환점이 아닐 때도 발화하는가 (기저율)")
    add("")
    start = max(WINDOWS)
    scan = []
    idxs = list(range(start, len(dates), args.stride))
    for k, i in enumerate(idxs):
        mp, mn = sign_scan(days, logp, i)
        scan.append((dates[i], prices[i], mp, mn))
        if k % 25 == 0:
            print(f"  {k}/{len(idxs)} {dates[i]}", file=sys.stderr, flush=True)

    add(f"{args.stride}일 간격 {len(scan)}개 지점.")
    add("")
    add("| 임계 | 양의 거품 발화 | 음의 거품 발화 | 고점 4곳 포착 | 저점 4곳 포착 |")
    add("|---|---:|---:|---:|---:|")
    for th in THRESH:
        pf = sum(1 for _, _, mp, _ in scan if mp is not None and mp <= th)
        nf = sum(1 for _, _, _, mn in scan if mn is not None and mn <= th)
        th_top = sum(1 for lab, _, mp, _ in tp_rows if lab == "고점" and mp is not None and mp <= th)
        th_bot = sum(1 for lab, _, _, mn in tp_rows if lab == "저점" and mn is not None and mn <= th)
        add(f"| ≤{th*100:.0f}% | {pf}/{len(scan)} ({pf/len(scan)*100:.0f}%) "
            f"| {nf}/{len(scan)} ({nf/len(scan)*100:.0f}%) | {th_top}/4 | {th_bot}/4 |")
    add("")
    add("**포착률이 기저율보다 크게 높지 않으면 그 지표는 아무것도 말하지 않는다.**")
    add("")

    # --- 4. 발화 구간이 실제로 어디였나 --------------------------------------
    for name, sel in (("양의 거품", lambda r: r[2]), ("음의 거품", lambda r: r[3])):
        th = 0.15
        hot = [(d, px) for d, px, mp, mn in scan
               if (sel((d, px, mp, mn)) is not None and sel((d, px, mp, mn)) <= th)]
        add(f"### {name} ≤15% 발화 구간 — {len(hot)}개 지점")
        add("")
        if not hot:
            add("없음.")
            add("")
            continue
        runs, cur = [], [hot[0]]
        for prev, nxt in zip(hot, hot[1:]):
            if (nxt[0] - prev[0]).days <= args.stride * 2:
                cur.append(nxt)
            else:
                runs.append(cur)
                cur = [nxt]
        runs.append(cur)
        px_lut = dict(zip(dates, prices))
        add("| 구간 | 지점 | 시작가 | 이후 180일 | 이후 365일 |")
        add("|---|---:|---:|---:|---:|")
        for run in runs:
            d0, d1 = run[0][0], run[-1][0]
            p0 = run[0][1]

            def fwd(n: int) -> str:
                q = px_lut.get(d1 + timedelta(days=n))
                return f"{(q/run[-1][1]-1)*100:+.0f}%" if q else "—"

            add(f"| {d0} ~ {d1} | {len(run)} | ${p0:,.0f} | {fwd(180)} | {fwd(365)} |")
        add("")

    text = "\n".join(L)
    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
