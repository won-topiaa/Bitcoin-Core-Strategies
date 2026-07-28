#!/usr/bin/env python3
"""전체 이력에 대해 BCS를 하루씩 계산한다.

앵커 좌표를 실측에 맞추기 위한 도구다. "이 앵커로 2017년 고점에서 BCS가
몇 점이 나왔을까"에 답한다.

    python3 tools/backtest.py --csv data/market.csv
    python3 tools/backtest.py --csv data/market.csv --adaptive 0.6
    python3 tools/backtest.py --csv data/market.csv --out reports/backtest.md

**중요한 한계** — 보유자 행동 계열(RHODL / Reserve Risk / LTH 배수)은 수동
입력이라 과거 이력이 없다. 따라서 이 백테스트는 4계열 중 3계열(밸류에이션 30,
가격 25, 공급 20 → 재정규화 후 40/33.3/26.7)만 쓴다. 커버리지 75%다.
실전 BCS와 정확히 같은 값이 아니라는 뜻이고, 앵커의 상대적 위치를 보는
용도로만 써야 한다.
"""

from __future__ import annotations

import argparse
import bisect
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc_core.config import load_config                      # noqa: E402
from btc_core.datasources.csv_source import load_csv_bundle   # noqa: E402
from btc_core.indicators import (                             # noqa: E402
    GRM_BASE, HASH_EXPANSION_RATIO, HASH_FAST, HASH_RECOVERY_WINDOW, HASH_SLOW,
    MA_2Y, MA_200W, PI_FAST, PI_SLOW, PUELL_WINDOW,
)
from btc_core.models import Reading                           # noqa: E402
from btc_core.score import compute_bcs, evaluate_consensus, score_indicator  # noqa: E402
from btc_core.series import Series, expanding_stdev, ratio, sma  # noqa: E402

PERCENTILE_YEARS = 4.0
HISTORY_DAYS = int(PERCENTILE_YEARS * 365.25)

# 사이클 구간. 저점에서 다음 저점 직전까지를 한 사이클로 본다.
CYCLES = [
    ("2013 사이클", date(2012, 1, 1), date(2014, 6, 30)),
    ("2017 사이클", date(2016, 1, 1), date(2018, 6, 30)),
    ("2021 사이클", date(2020, 1, 1), date(2022, 6, 30)),
    ("2025 사이클", date(2023, 6, 1), date(2026, 12, 31)),
]

# 퍼센타일 혼합을 적용할 지표 (4년치 이력을 만들 수 있는 것들)
ADAPTIVE_KEYS = {"mvrv_z", "puell", "thermocap"}


@dataclass
class Day:
    on: date
    price: float
    raws: dict[str, float | str]
    bcs: Optional[float] = None
    band: str = ""
    coverage: float = 0.0
    consensus: bool = False


# ---------------------------------------------------------------------------
# 지표 시계열 — 하루씩 다시 계산하지 않고 한 번에 만든다
# ---------------------------------------------------------------------------

def build_indicator_series(market) -> dict[str, Series]:
    m = market.normalized()
    p = m.price

    fast, slow = sma(p, PI_FAST), sma(p, PI_SLOW)
    pi = fast.map_with(slow, lambda f, s: f / (s * 2.0) if s else None)

    grm = ratio(p, sma(p, GRM_BASE))
    ma200w = ratio(p, sma(p, MA_200W))
    ma2y = ratio(p, sma(p, MA_2Y))

    out = {"pi_cycle": pi, "grm": grm, "ma200w_mult": ma200w, "ma2y_mult": ma2y}

    if m.market_cap is not None and m.realized_cap is not None:
        diff = m.market_cap.map_with(m.realized_cap, lambda a, r: a - r)
        out["mvrv_z"] = ratio(diff, expanding_stdev(m.market_cap))
        out["nupl"] = m.market_cap.map_with(
            m.realized_cap, lambda a, r: (a - r) / a if a else None
        )

    revenue = m.issuance_usd
    if revenue is None and m.issuance_btc is not None:
        revenue = m.issuance_btc.map_with(p, lambda i, q: i * q if i else None)
    if revenue is not None:
        out["puell"] = ratio(revenue, sma(revenue, PUELL_WINDOW))

    if m.issuance_usd is not None and m.market_cap is not None:
        cum, total = [], 0.0
        for v in m.issuance_usd.values:
            if v is not None:
                total += v
            cum.append(total if total > 0 else None)
        thermo = Series(m.issuance_usd.dates, tuple(cum), "thermocap")
        out["thermocap"] = ratio(m.market_cap, thermo)

    if m.hashrate is not None:
        out["hash_ribbons"] = hash_ribbon_states(m.hashrate)

    return out


def hash_ribbon_states(hashrate: Series) -> Series:
    """Hash Ribbons 상태를 시계열로. 문자열을 값으로 담는다."""
    fast, slow = sma(hashrate, HASH_FAST), sma(hashrate, HASH_SLOW)
    states: list[Optional[str]] = []
    last_cross: Optional[date] = None
    prev: Optional[tuple[float, float]] = None

    for d, f, s in zip(fast.dates, fast.values, slow.values):
        if f is None or s is None:
            states.append(None)
            continue
        if prev is not None and prev[0] < prev[1] and f >= s:
            last_cross = d
        prev = (f, s)

        if f < s:
            states.append("capitulation")
        elif last_cross is not None and (d - last_cross).days <= HASH_RECOVERY_WINDOW:
            states.append("recovery")
        elif s and f / s >= HASH_EXPANSION_RATIO:
            states.append("expansion")
        else:
            states.append("normal")
    return Series(fast.dates, tuple(states), "hash_ribbons")


# ---------------------------------------------------------------------------
# 하루씩 점수화
# ---------------------------------------------------------------------------

def run(cfg, market, adaptive: float) -> list[Day]:
    series = build_indicator_series(market)
    p = market.normalized().price
    prices = dict(zip(p.dates, p.values))

    lut = {k: dict(zip(s.dates, s.values)) for k, s in series.items()}
    # 퍼센타일용 누적 이력 (날짜 오름차순 값 목록)
    hist: dict[str, list[tuple[date, float]]] = {k: [] for k in ADAPTIVE_KEYS}

    days: list[Day] = []
    for d in p.dates:
        raws: dict[str, float | str] = {}
        for key, table in lut.items():
            v = table.get(d)
            if v is not None:
                raws[key] = v

        for key in ADAPTIVE_KEYS:
            if key in raws and isinstance(raws[key], (int, float)):
                hist[key].append((d, float(raws[key])))

        readings: dict[str, Reading] = {}
        for key, spec in cfg.indicators.items():
            h = None
            if key in ADAPTIVE_KEYS and adaptive > 0:
                cutoff = d - timedelta(days=HISTORY_DAYS)
                arr = hist[key]
                i = bisect.bisect_left(arr, (cutoff, float("-inf")))
                window = [v for _, v in arr[i:]]
                if len(window) >= 90:
                    h = window
            readings[key] = score_indicator(
                cfg, key, raws.get(key), history=h, adaptive_weight=adaptive, spec=spec
            )

        bcs, families, coverage = compute_bcs(cfg, readings)
        c = evaluate_consensus(cfg, families, bcs)
        days.append(
            Day(
                on=d,
                price=prices.get(d) or 0.0,
                raws=raws,
                bcs=bcs,
                band=cfg.band_for(bcs)["key"] if bcs is not None else "",
                coverage=coverage,
                consensus=c.passed,
            )
        )
    return days


# ---------------------------------------------------------------------------
# 사이클 전환점 탐지
# ---------------------------------------------------------------------------

def find_extremes(days: list[Day], *, window: int = 270) -> list[tuple[str, Day]]:
    """국소 최고가/최저가를 찾는다. 앞뒤 window 일 안에서 유일한 극값인 지점."""
    vals = [d.price for d in days]
    out: list[tuple[str, Day]] = []
    n = len(days)
    for i in range(window, n - window):
        seg = vals[i - window:i + window + 1]
        if vals[i] == max(seg) and vals[i] > 0:
            out.append(("고점", days[i]))
        elif vals[i] == min(seg) and vals[i] > 0:
            out.append(("저점", days[i]))
    # 인접한 같은 종류를 병합 (가장 극단인 것만 남긴다)
    merged: list[tuple[str, Day]] = []
    for kind, d in out:
        if merged and merged[-1][0] == kind and (d.on - merged[-1][1].on).days < window:
            better = (kind == "고점" and d.price > merged[-1][1].price) or (
                kind == "저점" and d.price < merged[-1][1].price
            )
            if better:
                merged[-1] = (kind, d)
        else:
            merged.append((kind, d))
    return merged


def actionable(cfg, days: list[Day]) -> list[Day]:
    """실제 엔진이 실행을 허용하는 날만 남긴다.

    합의 게이트 통과 + 커버리지 하한 충족. 이 필터가 없으면 데이터 시작
    직후처럼 이동평균이 아직 안 나온 구간에서 헛신호가 잡힌다.
    """
    min_cov = float(cfg.coverage.get("min_coverage_for_action", 0.0))
    return [
        d for d in days
        if d.bcs is not None and d.consensus and d.coverage >= min_cov
        and "ma200w_mult" in d.raws        # 200주 이동평균이 나온 뒤부터
    ]


def peak_cycle_usage(cfg, days: list[Day], trades, ladder: str) -> float:
    """한 사이클 안에서 실행된 누적의 최대치.

    사이클 경계는 재무장 규칙과 같은 기준 — BCS 가 반대편 구간(±rearm_opposite_bcs)
    을 방문하면 예산이 초기화된다. 달력으로 자르면 두 사이클이 한 칸에 섞여서
    상한을 넘은 것처럼 보인다.
    """
    opp = float(cfg.hysteresis.get("rearm_opposite_bcs", 0))
    hist = [(d.on, d.bcs) for d in days if d.bcs is not None]
    rows = [t for t in trades if t[1] == ladder]
    peak = run = 0.0
    prev = None
    for t in rows:
        if prev is not None and opp > 0:
            between = [v for dd, v in hist if prev < dd <= t[0]]
            crossed = (
                (between and min(between) <= -opp) if ladder == "distribute"
                else (between and max(between) >= opp)
            )
            if crossed:
                run = 0.0
        run += t[3]
        prev = t[0]
        peak = max(peak, run)
    return peak


def simulate(cfg, days: list[Day]) -> list[tuple[date, str, float, float, float, float]]:
    """실제 전략 엔진을 하루씩 돌려 사다리 실행을 재현한다.

    ``next_ladder_step`` / ``commit_action`` 을 그대로 쓰므로, 확정 대기·계단 간
    간격·재무장 규칙이 프로덕션과 동일하게 적용된다. 즉 이 함수는 전략 코드의
    회귀 검증이기도 하다.
    """
    from btc_core.strategy import ExecutionState, commit_action, next_ladder_step

    min_cov = float(cfg.coverage.get("min_coverage_for_action", 0.0))
    state = ExecutionState()
    out = []
    for d in days:
        if d.bcs is None:
            continue
        state.record_bcs(d.on, d.bcs)
        if not d.consensus or d.coverage < min_cov or "ma200w_mult" not in d.raws:
            continue
        ladder = "distribute" if d.bcs > 0 else "accumulate"
        step = next_ladder_step(cfg, ladder, d.bcs, None, state, d.on)
        if step is None or not step.executable:
            continue
        commit_action(state, step, d.bcs, on=d.on)
        out.append((d.on, ladder, step.trigger, step.size_pct, d.price, d.bcs))
    return out


def ladder_events(cfg, days: list[Day]) -> list[tuple[date, str, float, float]]:
    """각 사다리 계단이 실제로 실행 가능해진 최초 시점."""
    live = actionable(cfg, days)
    events = []
    for ladder, sign in (("distribute", 1), ("accumulate", -1)):
        for step in cfg.ladders[ladder]["steps"]:
            trig = float(step["trigger_bcs"])
            for d in live:
                if (d.bcs >= trig) if sign > 0 else (d.bcs <= trig):
                    events.append((d.on, ladder, trig, d.price))
                    break
    return sorted(events)


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

def summarize(cfg, days: list[Day], adaptive: float) -> str:
    L: list[str] = []
    add = L.append
    live = [d for d in days if d.bcs is not None]
    if not live:
        return "산출 가능한 날이 없습니다."

    add(f"# 백테스트 — adaptive_weight = {adaptive:.2f}")
    add("")
    add(f"- 기간: {live[0].on} ~ {live[-1].on} ({len(live):,}일)")
    add(f"- 커버리지: {live[-1].coverage:.0%} "
        f"(보유자 행동 계열은 수동 입력이라 과거 이력 없음)")
    add(f"- BCS 범위: {min(d.bcs for d in live):+.1f} ~ {max(d.bcs for d in live):+.1f}")
    add("")

    add("## 사이클 전환점에서의 BCS")
    add("")
    add("| 구분 | 날짜 | 가격 | BCS | 밴드 | 합의 | MVRV Z | NUPL | Puell | 200W배수 |")
    add("|---|---|---:|---:|---|:--:|---:|---:|---:|---:|")
    for kind, d in find_extremes(days):
        if d.bcs is None:
            continue
        r = d.raws
        add(
            f"| {kind} | {d.on} | ${d.price:,.0f} | **{d.bcs:+.1f}** | "
            f"{cfg.band_for(d.bcs)['label']} | {'O' if d.consensus else 'X'} | "
            f"{_n(r.get('mvrv_z'))} | {_n(r.get('nupl'), 3)} | "
            f"{_n(r.get('puell'))} | {_n(r.get('ma200w_mult'))} |"
        )
    add("")

    add("## 밴드별 체류 일수")
    add("")
    add("| 밴드 | 일수 | 비율 |")
    add("|---|---:|---:|")
    counts: dict[str, int] = {}
    for d in live:
        counts[d.band] = counts.get(d.band, 0) + 1
    for band in cfg.bands:
        k = band["key"]
        n = counts.get(k, 0)
        add(f"| {band['label']} | {n:,} | {n / len(live):.1%} |")
    add("")

    add("## 사다리 최초 발동 시점")
    add("")
    add("합의 게이트 통과 + 커버리지 하한 충족 + 200주 이동평균 산출 가능한 날만.")
    add("")
    ev = ladder_events(cfg, days)
    if not ev:
        add("발동한 계단 없음.")
    else:
        add("| 날짜 | 사다리 | 트리거 | 그때 가격 |")
        add("|---|---|---:|---:|")
        for on, ladder, trig, price in ev:
            add(f"| {on} | {ladder} | {trig:+.0f} | ${price:,.0f} |")
    add("")

    add("## 사다리 실행 시뮬레이션")
    add("")
    add("실제 전략 엔진(`next_ladder_step` + `commit_action`)을 하루씩 그대로 돌린 결과다.")
    add("확정 대기, 계단 간 간격, 반대편 밴드 재무장 규칙이 모두 적용된다.")
    add("")
    trades = simulate(cfg, days)
    if not trades:
        add("실행된 계단 없음.")
    else:
        add("| 날짜 | 사다리 | 트리거 | 크기 | 그때 가격 | 그때 BCS |")
        add("|---|---|---:|---:|---:|---:|")
        for t in trades:
            add(f"| {t[0]} | {t[1]} | {t[2]:+.0f} | {t[3]:.0f}% | ${t[4]:,.0f} | {t[5]:+.1f} |")
        add("")
        for ladder, unit in (("distribute", "보유 BTC"), ("accumulate", "예비 현금")):
            rows = [t for t in trades if t[1] == ladder]
            if not rows:
                continue
            total = sum(t[3] for t in rows)
            vwap = sum(t[3] * t[4] for t in rows) / total
            add(f"- **{ladder}** {len(rows)}회 · 16년 누적 {total:.0f}% ({unit} 대비) · "
                f"실행 가중평균가 **${vwap:,.0f}**")
        add("")
        add("검증 대상은 **한 사이클 안의 누적이 상한을 넘지 않는가** 다. 16년 누적은")
        add("여러 사이클에 걸친 합이라 상한과 직접 비교할 수 없다. 사이클 경계는")
        add("달력이 아니라 엔진이 쓰는 기준(BCS 가 반대편 구간을 방문한 시점)으로 자른다.")
        add("")
        cap = 100.0 - float(cfg.ladders["distribute"]["core_hold_pct"])
        add("| 사다리 | 사이클 내 최대 누적 | 상한 | 판정 |")
        add("|---|---:|---:|:--:|")
        for ladder, limit in (("distribute", cap), ("accumulate", 100.0)):
            peak = peak_cycle_usage(cfg, days, trades, ladder)
            add(f"| {ladder} | {peak:.0f}% | {limit:.0f}% | "
                f"{'OK' if peak <= limit + 1e-9 else '초과'} |")
    add("")

    add("## 사이클별 실행 요약")
    add("")
    add("| 구간 | BCS 최대 | 그날 | 그때 가격 | 이후 최고가 | BCS 최소 | 그날 | 그때 가격 |")
    add("|---|---:|---|---:|---:|---:|---|---:|")
    live_act = [d for d in live if "ma200w_mult" in d.raws]
    for label, s, e in CYCLES:
        seg = [d for d in live_act if s <= d.on <= e]
        if not seg:
            continue
        hi = max(seg, key=lambda d: d.bcs)
        lo = min(seg, key=lambda d: d.bcs)
        after = max(d.price for d in seg if d.on >= hi.on)
        add(f"| {label} | {hi.bcs:+.1f} | {hi.on} | ${hi.price:,.0f} | ${after:,.0f} | "
            f"{lo.bcs:+.1f} | {lo.on} | ${lo.price:,.0f} |")
    add("")

    add("## 지표별 분포")
    add("")
    add("| 지표 | 최소 | p10 | 중앙 | p90 | 최대 | 최근값 |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for key in ("mvrv_z", "nupl", "puell", "pi_cycle", "grm", "ma200w_mult", "ma2y_mult"):
        vals = sorted(
            float(d.raws[key]) for d in days
            if key in d.raws and isinstance(d.raws[key], (int, float))
        )
        if not vals:
            continue
        cur = days[-1].raws.get(key)
        add(f"| {cfg.indicators[key]['label'].split(' (')[0]} | {vals[0]:.2f} | "
            f"{_q(vals, .10):.2f} | {_q(vals, .50):.2f} | {_q(vals, .90):.2f} | "
            f"{vals[-1]:.2f} | {_n(cur)} |")
    add("")

    cur = live[-1]
    add("## 현재")
    add("")
    add(f"- {cur.on} · ${cur.price:,.0f}")
    add(f"- **BCS {cur.bcs:+.1f}** — {cfg.band_for(cur.bcs)['label']}")
    add(f"- 합의 게이트: {'통과' if cur.consensus else '미달'}")
    return "\n".join(L)


def _n(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, str):
        return v
    return f"{v:.{digits}f}"


def _q(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def main() -> int:
    ap = argparse.ArgumentParser(description="BCS 전체 이력 백테스트")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--config", default=None)
    ap.add_argument("--adaptive", type=float, default=0.35)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    bundle = load_csv_bundle(args.csv)
    days = run(cfg, bundle.market, args.adaptive)
    text = summarize(cfg, days, args.adaptive)

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
