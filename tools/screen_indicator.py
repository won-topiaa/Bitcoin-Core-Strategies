#!/usr/bin/env python3
"""후보 지표가 사이클 타이밍에 쓸모가 있는지 검정한다.

지표를 추가하기 전에 반드시 통과시켜야 하는 관문이다. "그럴듯하다"와
"실제로 정보가 있다"는 다르고, 노이즈를 25% 가중치 계열에 넣으면 시스템이
나빠진다.

    python3 tools/screen_indicator.py --csv data/market.csv

## 검정 방법

각 지표에 대해 **선행 수익률과의 순위상관(Spearman)**을 본다.

- 과열 지표라면 값이 높을 때 이후 수익률이 낮아야 한다 → **음의 상관**
- 상관이 0 근처면 그 지표에는 사이클 정보가 없다

순위상관을 쓰는 이유는 지표마다 스케일과 분포가 제각각이고, 우리가 쓰는
정규화도 단조변환(꺾은선)이라 순위만 보존되면 충분하기 때문이다.

## 사분위 분리도

상관계수 하나로는 부족하다. 실제로 쓰는 방식은 "극단 구간에서 행동한다"
이므로, **상위 25% 구간과 하위 25% 구간의 이후 수익률 차이**를 함께 본다.
쓸 만한 과열 지표라면 상위 사분위의 이후 수익률이 하위 사분위보다 낮아야 한다.

## 주의

표본이 독립이 아니다. 일간 관측이 겹치는 365일 창을 공유하므로 유효 표본
수는 실제 관측 수보다 훨씬 적다(대략 사이클 개수). p값을 계산하지 않는 이유가
이것이다. 여기서 얻는 것은 통계적 유의성이 아니라 **효과의 방향과 크기**다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from btc_core.datasources.csv_source import load_csv_bundle   # noqa: E402
from btc_core.series import Series, sma                        # noqa: E402

HORIZONS = (180, 365, 730)


# ---------------------------------------------------------------------------
# 통계
# ---------------------------------------------------------------------------

def rank(values: Sequence[float]) -> list[float]:
    """동순위를 평균으로 처리하는 순위."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) < 30:
        return None
    rx, ry = rank(xs), rank(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else None


def quartile_split(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """(지표값, 이후수익률) → (상위 25% 평균 수익률, 하위 25% 평균 수익률)."""
    s = sorted(pairs, key=lambda p: p[0])
    q = max(1, len(s) // 4)
    low = sum(r for _, r in s[:q]) / q
    high = sum(r for _, r in s[-q:]) / q
    return high, low


# ---------------------------------------------------------------------------
# 지표 시계열 만들기
# ---------------------------------------------------------------------------

def candidate_series(market) -> dict[str, Series]:
    """기존 지표 + 신규 후보를 한자리에 모은다."""
    import backtest as bt

    out = dict(bt.build_indicator_series(market))
    out.pop("hash_ribbons", None)          # 범주형이라 순위상관 대상이 아니다

    m = market.normalized()
    ex, sply = m.exchange_supply, m.supply
    fin, fout = m.exchange_inflow, m.exchange_outflow

    if ex is not None and sply is not None:
        ratio = ex.map_with(sply, lambda e, s: e / s * 100.0 if s else None)
        out["ex_supply_ratio"] = Series(ratio.dates, ratio.values, "ex_supply_ratio")

        # 추세 제거 1 — 자기 1년 이동평균 대비
        base = sma(ratio, 365)
        rel = ratio.map_with(base, lambda v, b: v / b if b else None)
        out["ex_supply_rel1y"] = Series(rel.dates, rel.values, "ex_supply_rel1y")

        # 추세 제거 2 — 90일 변화(%p)
        vals = list(ratio.values)
        chg = [None] * len(vals)
        for i in range(90, len(vals)):
            if vals[i] is not None and vals[i - 90] is not None:
                chg[i] = vals[i] - vals[i - 90]
        out["ex_supply_chg90"] = Series(ratio.dates, tuple(chg), "ex_supply_chg90")

    if fin is not None and fout is not None and sply is not None:
        net = fin.map_with(fout, lambda a, b: a - b)
        net30 = sma(Series(net.dates, net.values, "net"), 30)
        nf = net30.map_with(sply, lambda v, s: v / s * 100.0 * 365.0 if s else None)
        out["ex_netflow"] = Series(nf.dates, nf.values, "ex_netflow")

    # 실현시총 증가율 — 보유자 행동 후보.
    # 실현시총은 코인이 움직일 때만 값이 갱신되므로, 증가 속도는 곧
    # "오래된 원가가 얼마나 빨리 새 매수자의 원가로 교체되고 있는가"다.
    # RHODL 이 재려는 것과 개념이 같고, 무료 데이터로 만들 수 있다.
    rcap = m.realized_cap
    if rcap is not None:
        for win, key in ((30, "rcap_growth30"), (90, "rcap_growth90")):
            vals = list(rcap.values)
            g = [None] * len(vals)
            for i in range(win, len(vals)):
                a, b = vals[i], vals[i - win]
                if a is not None and b:
                    g[i] = (a / b - 1.0) * 100.0
            out[key] = Series(rcap.dates, tuple(g), key)

    # --- 저점 특성 후보 -----------------------------------------------------
    # 사이클 저점의 공통점에서 뽑았다. 저점 4곳에서 관측된 값:
    #   ATH 대비 낙폭   -92.7% / -84.5% / -83.8% / -76.6%
    #   ATH 이후 경과일   163 / 406 / 364 / 378
    #   MVRV < 1 여부     전부 해당 (0.42 / 0.56 / 0.69 / 0.78)
    price = m.price

    # 1) 역대 최고가 대비 낙폭. 이동평균이 아니라 '단일 과거 극값' 기준이라
    #    가격 계열 4종과 계산 원리가 다르다.
    peak = 0.0
    dd, since = [], []
    days_since_peak = 0
    for v in price.values:
        if v is not None and v > peak:
            peak, days_since_peak = v, 0
        else:
            days_since_peak += 1
        dd.append((v / peak - 1.0) * 100.0 if v is not None and peak else None)
        since.append(float(days_since_peak) if peak else None)
    out["drawdown_from_ath"] = Series(price.dates, tuple(dd), "drawdown_from_ath")

    # 2) 최고가 이후 경과일. 시스템에 시간 축이 하나도 없어서 후보로 넣는다.
    out["days_since_ath"] = Series(price.dates, tuple(since), "days_since_ath")

    # 3) 시장 전체가 원가 아래인 기간의 누적 일수.
    #    수준(NUPL)이 아니라 '얼마나 오래'를 본다.
    if m.realized_cap is not None and m.supply is not None:
        sup = dict(zip(m.supply.dates, m.supply.values))
        run, uw = 0, []
        for d, rc in zip(m.realized_cap.dates, m.realized_cap.values):
            s, px = sup.get(d), dict(zip(price.dates, price.values)).get(d)
            if rc and s and px:
                run = run + 1 if px < rc / s else 0
                uw.append(float(run))
            else:
                uw.append(None)
        out["underwater_days"] = Series(m.realized_cap.dates, tuple(uw), "underwater_days")

    # 서멀캡 배수 — 누적 채굴 수익 대비 시가총액.
    # 밸류에이션 후보. 실현시총과 달리 '지금까지 네트워크에 투입된 비용'이 분모다.
    if m.issuance_usd is not None and m.market_cap is not None:
        cum, total = [], 0.0
        for v in m.issuance_usd.values:
            if v is not None:
                total += v
            cum.append(total if total > 0 else None)
        thermo = Series(m.issuance_usd.dates, tuple(cum), "thermocap")
        tm = m.market_cap.map_with(thermo, lambda mc, tc: mc / tc if tc else None)
        out["thermocap_mult"] = Series(tm.dates, tm.values, "thermocap_mult")

    return out


# ---------------------------------------------------------------------------
# 검정
# ---------------------------------------------------------------------------

def forward_returns(price: Series, horizon: int) -> dict[date, float]:
    lut = dict(zip(price.dates, price.values))
    out = {}
    for d, v in zip(price.dates, price.values):
        if v is None or v <= 0:
            continue
        fut = lut.get(d + timedelta(days=horizon))
        if fut:
            out[d] = (fut / v - 1.0) * 100.0
    return out


def screen(market, *, start: Optional[date] = None) -> list[dict]:
    cands = candidate_series(market)
    price = market.normalized().price
    fwd = {h: forward_returns(price, h) for h in HORIZONS}

    results = []
    for key, s in sorted(cands.items()):
        row = {"key": key}
        for h in HORIZONS:
            pairs = [
                (float(v), fwd[h][d])
                for d, v in zip(s.dates, s.values)
                if v is not None and d in fwd[h] and (start is None or d >= start)
            ]
            if len(pairs) < 200:
                row[h] = None
                continue
            rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            hi, lo = quartile_split(pairs)
            row[h] = {"rho": rho, "hi": hi, "lo": lo, "n": len(pairs)}
        results.append(row)
    return results


def render(results: list[dict], label: str) -> str:
    L = [f"### {label}", "",
         "| 지표 | ρ(180일) | ρ(1년) | ρ(2년) | 상위25% 1년수익 | 하위25% 1년수익 | 분리도 |",
         "|---|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        cells = []
        for h in HORIZONS:
            v = r.get(h)
            cells.append(f"{v['rho']:+.2f}" if v and v["rho"] is not None else "—")
        y = r.get(365)
        if y:
            hi, lo = y["hi"], y["lo"]
            cells += [f"{hi:+,.0f}%", f"{lo:+,.0f}%", f"{hi - lo:+,.0f}%p"]
        else:
            cells += ["—", "—", "—"]
        L.append(f"| {r['key']} | " + " | ".join(cells) + " |")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="후보 지표의 사이클 정보량 검정")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--since", default=None, help="이 날짜 이후만 (YYYY-MM-DD)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    bundle = load_csv_bundle(args.csv)
    # --since 는 예전에 파싱만 하고 아무 데도 쓰이지 않았다. 이 도구의 출력이
    # config 에서 지표 편입 근거로 인용되므로(strategy.yaml), "2023년 이후로
    # 걸러 봤다"고 믿은 사람이 실제로는 16년 전체 표본을 보게 되는 상태였다.
    if args.since:
        floor = date.fromisoformat(args.since)
        spans = [(floor, f"{args.since} 이후")]
    else:
        spans = [(None, "전체 기간"), (date(2017, 1, 1), "2017년 이후"),
                 (date(2020, 1, 1), "2020년 이후")]
    blocks: list[str] = []
    for start, label in spans:
        if blocks:
            blocks.append("")
        blocks.append(render(screen(bundle.market, start=start) if start
                             else screen(bundle.market), label))
    text = (
        "# 지표 선별 검정\n\n"
        "ρ = 지표값과 이후 수익률의 순위상관. **과열 지표는 음수여야 한다**\n"
        "(값이 높을 때 이후 수익률이 낮아야 하므로).\n\n"
        "분리도 = 상위 사분위 1년 수익 − 하위 사분위 1년 수익. **음수가 클수록 좋다.**\n\n"
        "표본이 독립이 아니므로(겹치는 창) p값은 계산하지 않는다.\n"
        "여기서 보는 것은 효과의 방향과 크기다.\n\n"
        + "\n".join(blocks) + "\n"
    )
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
