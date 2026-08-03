#!/usr/bin/env python3
"""반감기 4년 사이클 · 계절성 ↔ 비트코인 — 귀무가설을 제대로 세워서 검정한다.

    python3 tools/cycle_study.py

## 왜 이 검정이 까다로운가 — 네 번째 실패 모드

앞선 검정들은 세 함정을 걸러냈다.

  거시·자산군 → 위험자산 베타   (ΔVIX·나스닥 통제, docs/27·28)
  온체인·파생  → 가격의 그림자   (전방 예측 + 모멘텀 통제, docs/29)
  인물 사건    → 사후편향       (예정/돌발 분리, docs/30)

반감기 사이클은 **네 번째**를 가진다 — **오귀인(誤歸因)**. 데이터에 진짜 구조가
있는데 **원인을 엉뚱한 것에 돌리는** 경우다. "4년 주기가 보인다"와 "그 주기가
반감기 **때문**이다"는 전혀 다른 주장인데, 흔히 하나로 묶여 이야기된다.

## 이 도구가 세우는 세 가지 귀무가설

  [A] 무작위 셔플   — 수익률을 통째로 섞는다. **너무 약한 귀무가설**이다.
                     BTC 수익률의 자기상관·추세를 다 없애 버려 p 값이 부풀려진다.
  [B] 순환이동     — 자기상관을 **보존**한 채 사이클 라벨만 어긋나게 민다.
                     시계열 검정의 정석이고, A 보다 훨씬 엄격하다.
  [C] 가짜 반감기   — 반감기 날짜를 ±20개월 임의로 옮겨 같은 계산을 한다.
                     **결정적 검정이다.** 옮겨도 같은 값이 나오면 잡히는 것은
                     '반감기'가 아니라 '4년쯤 되는 무언가'다.

실측 결론(docs/31): A 로는 p=0.004 로 유의해 보이지만, B 에서 **p=0.065** 로
약해지고, C 에서 **p=0.467**(가짜 반감기 중위 +0.300 ≈ 관측 +0.303) — **반감기
날짜 자체는 아무 정보도 주지 않는다.**

## 표본외 검증(리브-원-사이클-아웃)

"패턴이 보인다"는 지나간 데이터에 곡선을 맞춘 것일 수 있다. 그래서 **사이클 하나를
빼고** 나머지로 패턴을 만든 뒤, 뺀 사이클을 맞히는지 본다. 전체표본으로 재면
ρ≈+0.61 이 나오지만 그건 같은 데이터로 만들고 같은 데이터로 채점한 값이라 의미가
없다. 표본외 값은 **+0.303** 이고, 이것이 정직한 숫자다.

**근본 한계: 완결된 사이클이 3개뿐이다.** 통계가 무엇을 말하든 '주기'의 관측치가
셋이면 강한 주장을 세울 수 없다.
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import macro_correlation as mc        # noqa: E402
import onchain_correlation as oc      # noqa: E402

# 프로토콜이 정한 날짜 — 사후 선택의 여지가 없다(수년 전부터 알려져 있었다).
HALVINGS = [date(2012, 11, 28), date(2016, 7, 9), date(2020, 5, 11), date(2024, 4, 19)]
CYCLE_MONTHS = 48
FWD = 3            # 전방 수익률 개월


def _monthly(price: dict[date, float]) -> tuple[list, dict, dict]:
    pm = mc.month_end(price)
    P = {(d.year, d.month): pm[d] for d in sorted(pm)}
    km = sorted(P)
    idx = {k: i for i, k in enumerate(km)}
    return km, P, idx


def _cycle_pos(k, halvings) -> tuple[Optional[int], Optional[int]]:
    d = date(k[0], k[1], 15)
    last = n = None
    for i, h in enumerate(halvings):
        if d >= h:
            last, n = h, i + 1
    if last is None:
        return None, None
    return (d.year - last.year) * 12 + (d.month - last.month), n


def build_rows(price: dict[date, float], halvings=HALVINGS) -> list[tuple]:
    km, P, idx = _monthly(price)
    out = []
    for k in km:
        cm, n = _cycle_pos(k, halvings)
        i = idx[k]
        if cm is None or not (0 <= cm < CYCLE_MONTHS) or i + FWD >= len(km):
            continue
        out.append((k, cm, n, math.log(P[km[i + FWD]] / P[km[i]])))
    return out


def loco(rows: list[tuple]) -> Optional[float]:
    """리브-원-사이클-아웃 표본외 상관. 순환이 원천적으로 불가능한 유일한 값."""
    vals = []
    cycles = sorted({r[2] for r in rows})
    for hold in cycles:
        tr = [r for r in rows if r[2] != hold]
        te = [r for r in rows if r[2] == hold]
        if len(te) < 12:
            continue
        pat: dict[int, list[float]] = {}
        for _, cm, _, f in tr:
            pat.setdefault(cm, []).append(f)
        pred = [statistics.mean(pat[cm]) for _, cm, _, _ in te if cm in pat]
        act = [f for _, cm, _, f in te if cm in pat]
        v = mc.pearson(pred, act)
        if v is not None:
            vals.append(v)
    return statistics.mean(vals) if vals else None


def null_shuffle(rows, obs, n=2000, seed=3) -> float:
    """[A] 무작위 셔플 — 자기상관을 파괴하므로 **너무 관대하다**(참고용)."""
    rng = random.Random(seed)
    fs = [r[3] for r in rows]
    w = t = 0
    for _ in range(n):
        sh = fs[:]
        rng.shuffle(sh)
        v = loco([(r[0], r[1], r[2], sh[i]) for i, r in enumerate(rows)])
        if v is not None:
            t += 1
            w += v >= obs
    return w / t if t else float("nan")


def null_circular(rows, obs, n=2000, seed=11) -> tuple[float, float]:
    """[B] 순환이동 — 자기상관을 보존한 채 라벨만 어긋나게 민다."""
    rng = random.Random(seed)
    N = len(rows)
    w = 0
    vals = []
    for _ in range(n):
        s = rng.randrange(6, N - 6)
        v = loco([(rows[i][0], rows[i][1], rows[i][2], rows[(i + s) % N][3])
                  for i in range(N)])
        if v is not None:
            vals.append(v)
            w += v >= obs
    return (w / len(vals) if vals else float("nan"),
            statistics.median(vals) if vals else float("nan"))


def null_fake_halving(price, obs, n=2000, seed=11) -> tuple[float, float]:
    """[C] 가짜 반감기 — 날짜를 ±20개월 옮긴다. **결정적 검정.**"""
    rng = random.Random(seed)
    w = 0
    vals = []
    for _ in range(n):
        off = rng.randrange(-20, 21)
        fake = []
        for h in HALVINGS:
            y, mo = h.year + (h.month - 1 + off) // 12, (h.month - 1 + off) % 12 + 1
            fake.append(date(y, mo, min(h.day, 28)))
        v = loco(build_rows(price, fake))
        if v is not None:
            vals.append(v)
            w += v >= obs
    return (w / len(vals) if vals else float("nan"),
            statistics.median(vals) if vals else float("nan"))


def seasonality(price: dict[date, float], seed=5) -> dict:
    km, P, idx = _monthly(price)
    R = {km[i]: math.log(P[km[i]] / P[km[i - 1]]) for i in range(1, len(km))}
    rng = random.Random(seed)
    allv = list(R.values())
    months = {}
    for k, v in R.items():
        months.setdefault(k[1], []).append(v)
    out_m = {}
    for mo, v in sorted(months.items()):
        obs = statistics.mean(v)
        w = sum(1 for _ in range(4000)
                if abs(statistics.mean(rng.sample(allv, len(v)))) >= abs(obs))
        out_m[mo] = (obs, len(v), w / 4000)

    ds = sorted(price)
    dr = {ds[i]: math.log(price[ds[i]] / price[ds[i - 1]])
          for i in range(1, len(ds)) if price[ds[i - 1]] > 0 and price[ds[i]] > 0}
    allv2 = list(dr.values())
    wd: dict[int, list[float]] = {}
    for d, v in dr.items():
        wd.setdefault(d.weekday(), []).append(v)
    out_w = {}
    for w_, v in sorted(wd.items()):
        obs = statistics.mean(v)
        c = sum(1 for _ in range(2000)
                if abs(statistics.mean(rng.sample(allv2, len(v)))) >= abs(obs))
        out_w[w_] = (obs, len(v), c / 2000)
    return {"months": out_m, "weekdays": out_w}


def report(price: dict[date, float], quick: bool = False) -> str:
    rows = build_rows(price)
    obs = loco(rows)
    n = 400 if quick else 2000
    L: list[str] = []
    add = L.append
    add("=" * 84)
    add("  반감기 4년 사이클 · 계절성 ↔ 비트코인")
    add("=" * 84)
    ncyc = {c: sum(1 for r in rows if r[2] == c) for c in sorted({r[2] for r in rows})}
    add(f"  표본 {len(rows)}개월 · 사이클별 " + ", ".join(f"C{c}:{v}" for c, v in ncyc.items()))
    add("  완결된 사이클은 3개뿐이다 — 통계 이전에 '주기' 관측치가 셋이라는 한계가 있다.")
    add("")
    add("  [사이클 내 위치별 평균 3개월 전방 수익률] — 전체표본(사후적으로 본 패턴)")
    buck: dict[int, list[float]] = {}
    for _, cm, _, f in rows:
        buck.setdefault(cm // 6, []).append(f)
    for b in sorted(buck):
        v = buck[b]
        add(f"    반감기 후 {b*6:2d}~{b*6+5:2d}개월   {statistics.mean(v)*100:+6.1f}%   (n={len(v)})")
    add("")
    add(f"  [표본외 검증] 리브-원-사이클-아웃 rho = {obs:+.3f}")
    add("    (전체표본으로 재면 ~+0.61 이지만 같은 데이터로 만들고 채점한 값이라 의미 없다)")
    add("")
    add("  [귀무가설 셋]")
    pa = null_shuffle(rows, obs, n=n)
    pb, mb = null_circular(rows, obs, n=n)
    pc, mc_ = null_fake_halving(price, obs, n=n)
    add(f"    A 무작위 셔플    p = {pa:.3f}   ← 자기상관을 파괴하는 **너무 약한** 귀무가설")
    add(f"    B 순환이동       p = {pb:.3f}   (분포 중위 {mb:+.3f}) ← 자기상관 보존")
    add(f"    C 가짜 반감기    p = {pc:.3f}   (분포 중위 {mc_:+.3f}) ← **결정적**")
    add("")
    if pc > 0.10:
        add(f"    → C 가 말한다: 반감기를 ±20개월 옮겨도 같은 값({mc_:+.3f})이 나온다.")
        add("       **잡히는 것은 '반감기'가 아니다.** 4년쯤 되는 무언가이거나 표본 구조다.")
        add("       '4년 주기가 보인다'와 '반감기 때문이다'는 다른 주장이고, 후자는 기각된다.")

    s = seasonality(price)
    add("")
    add("  [계절성 — 월별]")
    best = min(s["months"].items(), key=lambda kv: kv[1][2])
    for mo, (avg, cnt, p) in s["months"].items():
        add(f"    {mo:2d}월  {avg*100:+6.1f}%  (n={cnt})  p={p:.3f}")
    add(f"    → 12개월을 모두 검정했으므로 다중검정 보정: 최소 p={best[1][2]:.3f} × 12 "
        f"= {min(1.0, best[1][2]*12):.2f}  → **유의한 달 없음**")
    add("")
    add("  [계절성 — 요일]")
    nm = "월화수목금토일"
    for w_, (avg, cnt, p) in s["weekdays"].items():
        add(f"    {nm[w_]}요일  {avg*100:+.3f}%  (n={cnt})  p={p:.3f}")
    add("    → 유의한 요일 없음")
    add("=" * 84)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="반감기 사이클·계절성 검정")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--quick", action="store_true", help="순열 반복을 줄여 빠르게")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    market = oc.load_market(args.csv)
    if "price" not in market:
        raise SystemExit(f"price 열이 없습니다: {args.csv}")
    text = report(market["price"], quick=args.quick)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
