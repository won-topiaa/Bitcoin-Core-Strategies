#!/usr/bin/env python3
"""사이클 저점의 공통점을 찾는다.

    python3 tools/bottom_study.py --csv data/market.csv
    python3 tools/bottom_study.py --csv data/market.csv --out reports/bottoms.md

저점을 사후에 지정하고 그 시점의 특성을 재는 방식이라 **순환논리 위험이 크다.**
여기서 나온 조건을 점수에 넣지 않는 이유가 그것이다. 결과는 표시 전용
저점 프로파일(config 의 `bottom_profile`)로만 쓴다.

정밀도 표는 그 위험을 정량화하려는 시도다. 조건을 저점에 맞춰 만들었으니
저점에서 맞는 건 당연하고, 실제로 물어야 할 것은 **"저점이 아닐 때도 자주
맞는가"** 다.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from btc_core.config import load_config                       # noqa: E402
from btc_core.datasources.csv_source import load_csv_bundle    # noqa: E402
import backtest as bt                                          # noqa: E402

# 각 약세장의 최저 종가. 사후 지정이며, 이 선택 자체가 결과에 영향을 준다.
BOTTOMS = [
    ("2011", date(2011, 11, 18)),
    ("2015", date(2015, 1, 14)),
    ("2018", date(2018, 12, 15)),
    ("2022", date(2022, 11, 21)),
]
TOPS = [
    ("2013", date(2013, 12, 4)),
    ("2017", date(2017, 12, 16)),
    ("2021", date(2021, 11, 8)),
    ("2025", date(2025, 10, 6)),
]
HALVINGS = [date(2012, 11, 28), date(2016, 7, 9), date(2020, 5, 11), date(2024, 4, 20)]

NEAR_DAYS = 90          # 저점 ±이 기간을 '저점 근처'로 본다
WARMUP = date(2014, 6, 1)   # 200주 이동평균이 나오기 시작하는 시점


def build(market):
    """조건 평가에 필요한 값들을 날짜별로 만든다."""
    m = market.normalized()
    p = m.price
    price = dict(zip(p.dates, p.values))
    series = bt.build_indicator_series(market)
    lut = {k: dict(zip(s.dates, s.values)) for k, s in series.items()}

    sup = dict(zip(m.supply.dates, m.supply.values)) if m.supply else {}
    mvrv = {}
    if m.realized_cap is not None:
        for d, rc in zip(m.realized_cap.dates, m.realized_cap.values):
            s, px = sup.get(d), price.get(d)
            if rc and s and px:
                mvrv[d] = px / (rc / s)

    peak, dd, since = 0.0, {}, {}
    n = 0
    for d in p.dates:
        v = price.get(d)
        if v and v > peak:
            peak, n = v, 0
        else:
            n += 1
        if peak and v:
            dd[d] = (v / peak - 1.0) * 100.0
            since[d] = n

    return price, lut, mvrv, dd, since


def conditions(cfg):
    ops = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
           ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
    return [
        (c["label"], c["key"], ops[c.get("op", "<")], float(c["threshold"]), c.get("unit", ""))
        for c in cfg.bottom_profile.get("conditions", [])
    ]


def values_on(d, price, lut, mvrv, dd, since) -> dict[str, Optional[float]]:
    return {
        "drawdown_pct": dd.get(d),
        "days_since_ath": float(since[d]) if d in since else None,
        "mvrv": mvrv.get(d),
        "nupl": lut.get("nupl", {}).get(d),
        "puell": lut.get("puell", {}).get(d),
        "ma200w_mult": lut.get("ma200w_mult", {}).get(d),
    }


def report(cfg, market) -> str:
    price, lut, mvrv, dd, since = build(market)
    dates = sorted(price)
    conds = conditions(cfg)
    L: list[str] = []
    add = L.append

    add("# 사이클 저점 연구")
    add("")
    add("> 저점을 **사후에 지정**하고 그 시점의 특성을 재는 방식이라 순환논리 위험이 크다.")
    add("> 아래 조건이 저점에서 맞는 것은 조건을 그렇게 만들었기 때문이기도 하다.")
    add("> 그래서 이 결과는 점수에 들어가지 않고 표시 전용으로만 쓴다.")
    add("")

    # --- 시간 구조 ---
    add("## 시간 구조")
    add("")
    add("| 구간 | 일수 |")
    add("|---|---:|")
    for (la, a), (lb, b) in zip(TOPS, BOTTOMS[1:]):
        add(f"| {la} 고점 → {lb} 저점 | {(b - a).days} |")
    for (la, a), (lb, b) in zip(BOTTOMS[1:], TOPS[1:]):
        add(f"| {la} 저점 → {lb} 고점 | {(b - a).days} |")
    add("")
    add("| 사건 | 직전 반감기로부터 | 다음 반감기까지 |")
    add("|---|---:|---:|")
    for lab, d in TOPS + BOTTOMS:
        prev = max([h for h in HALVINGS if h <= d], default=None)
        nxt = min([h for h in HALVINGS if h > d], default=None)
        kind = "고점" if (lab, d) in TOPS else "저점"
        add(f"| {lab} {kind} ({d}) | "
            f"{f'+{(d - prev).days}' if prev else '—'} | "
            f"{f'{(nxt - d).days}' if nxt else '—'} |")
    add("")

    # --- 저점에서의 조건 충족 ---
    add("## 저점에서의 조건 충족")
    add("")
    header = "| 저점 | 가격 | " + " | ".join(l for l, *_ in conds) + " | 일치 |"
    add(header)
    add("|---|---:|" + "---:|" * len(conds) + ":--:|")
    for lab, d in BOTTOMS:
        vals = values_on(d, price, lut, mvrv, dd, since)
        cells, hits = [], 0
        for label, key, op, thr, unit in conds:
            v = vals.get(key)
            if v is None:
                cells.append("—")
                continue
            ok = op(v, thr)
            hits += ok
            cells.append(f"{'**' if ok else ''}{v:,.2f}{'**' if ok else ''}")
        add(f"| {lab} ({d}) | ${price[d]:,.0f} | " + " | ".join(cells) + f" | {hits}/{len(conds)} |")
    add("")

    # --- 정밀도 ---
    add("## 일치 개수별 정밀도")
    add("")
    add(f"200주 이동평균이 산출되는 {WARMUP} 이후 구간. "
        f"'저점 근처'는 위 저점 ±{NEAR_DAYS}일.")
    add("")
    valid = [d for d in dates if d >= WARMUP]
    bottoms = [d for _, d in BOTTOMS]
    near = lambda d: any(abs((d - x).days) <= NEAR_DAYS for x in bottoms)

    buckets: dict[int, list[date]] = {}
    for d in valid:
        vals = values_on(d, price, lut, mvrv, dd, since)
        h = sum(
            1 for _, key, op, thr, _ in conds
            if vals.get(key) is not None and op(vals[key], thr)
        )
        buckets.setdefault(h, []).append(d)

    add("| 일치 | 일수 | 비율 | 이후 1년 수익 중앙값 | 저점 근처 비중 |")
    add("|---:|---:|---:|---:|---:|")
    for h in sorted(buckets):
        ds = buckets[h]
        fwd = []
        for d in ds:
            f = price.get(d + timedelta(days=365))
            if f and price.get(d):
                fwd.append((f / price[d] - 1.0) * 100.0)
        med = f"{statistics.median(fwd):+,.0f}%" if fwd else "—"
        add(f"| {h} | {len(ds):,} | {len(ds) / len(valid):.1%} | {med} | "
            f"{sum(1 for d in ds if near(d)) / len(ds):.0%} |")
    add("")

    # --- 현재 ---
    cur = dates[-1]
    vals = values_on(cur, price, lut, mvrv, dd, since)
    hits = sum(1 for _, key, op, thr, _ in conds
               if vals.get(key) is not None and op(vals[key], thr))
    add("## 현재")
    add("")
    add(f"{cur} · ${price[cur]:,.0f} · **{hits}/{len(conds)} 일치**")
    add("")
    add("| | 조건 | 현재값 | 기준 |")
    add("|:--:|---|---:|---|")
    for label, key, op, thr, unit in conds:
        v = vals.get(key)
        ok = v is not None and op(v, thr)
        shown = f"{v:,.2f}{unit}" if v is not None else "—"
        sym = {"<": "<", "<=": "≤", ">": ">", ">=": "≥"}[
            next(c["op"] for c in cfg.bottom_profile["conditions"] if c["key"] == key)
        ]
        add(f"| {'O' if ok else 'X'} | {label} | {shown} | {sym} {thr:g}{unit} |")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="사이클 저점의 공통점 연구")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    text = report(cfg, load_csv_bundle(args.csv).market)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"저장: {p}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
