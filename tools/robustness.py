#!/usr/bin/env python3
"""과적합 노출도를 측정한다.

    python3 tools/robustness.py --csv data/market.csv

이 저장소의 가장 큰 구조적 위험은 **조정 가능한 숫자가 269개인데 유효 표본이
사이클 4개**라는 것이다. 고전적 비율로는 재앙적이다. 그래서 "얼마나
과적합인가"를 말로 하지 않고 세 가지로 재기로 했다.

## 1. 자유도 세기

설정 파일 안의 조정 가능한 숫자를 항목별로 센다. 줄어들었는지 늘어났는지
매번 확인할 수 있게 하는 것이 목적이다.

## 2. 교란 실험 — 파라미터가 판단을 떠받치고 있나

앵커 좌표를 통째로 스케일·이동하고, 계열 가중치를 균등으로 바꾸고, 밸류에이션
집계를 뒤집어 본다. 그래도 **밴드와 DCA 배수가 안 바뀌면** 그 파라미터들은
실행 판단을 떠받치고 있지 않다는 뜻이다. 조정 가능한 숫자가 많은 것과
그 숫자에 결론이 달려 있는 것은 다른 문제다.

## 3. 정규화 일반화 검정 — 사이클을 넘어 같은 값을 내는가

같은 전환점에서 BCS 가 사이클마다 다르게 나오면 그 정규화는 일반화되지 않는다.
네 고점에서의 BCS 표준편차로 잰다. 이게 이 도구의 핵심이다.

고정 앵커는 마지막 사이클에 맞춰 조정된 것이라 구조적으로 이 검정에 약하다.
퍼센타일 정규화는 파라미터가 창 길이 하나뿐이라 강하다. 어느 쪽이 실제로
나은지는 측정으로만 알 수 있다.
"""

from __future__ import annotations

import argparse
import bisect
import copy
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from btc_core.cycles import MEASURED_BOTTOMS, MEASURED_TOPS   # noqa: E402
from btc_core.config import StrategyConfig, load_config          # noqa: E402
from btc_core.datasources.csv_source import load_csv_bundle       # noqa: E402
from btc_core.datasources.manual import load_manual               # noqa: E402
from btc_core.engine import evaluate                              # noqa: E402
from btc_core.models import Reading                               # noqa: E402
from btc_core.normalize import percentile_rank                    # noqa: E402
from btc_core.score import compute_bcs                            # noqa: E402
from btc_core.score import score_indicator                        # noqa: E402

import backtest as bt                                             # noqa: E402

TOPS = MEASURED_TOPS          # btc_core.cycles 가 소유한다 (docs/21)
BOTTOMS = MEASURED_BOTTOMS
NEAR_DAYS = 45
PERCENTILE_DAYS = int(4 * 365.25)


# ---------------------------------------------------------------------------
# 1. 자유도
# ---------------------------------------------------------------------------

def degrees_of_freedom(cfg: StrategyConfig) -> list[tuple[str, int]]:
    ind = cfg.indicators
    rows = [
        ("BCS 지표 앵커 좌표", sum(len(s.get("anchors", [])) * 2 for s in ind.values())),
        ("범주형 상태값", sum(len(s.get("states", {})) for s in ind.values())),
        ("LRS 앵커·상태",
         sum(len(s.get("anchors", [])) * 2 + len(s.get("states", {}))
             for s in cfg.lrs_components.values())),
        ("계열 가중치", len(cfg.bcs_families)),
        ("밴드 경계", len(cfg.bands) * 2),
        ("DCA 배수", len(cfg.ladders.get("dca_multiplier", {}))),
        ("사다리 계단",
         sum(len(cfg.ladders.get(k, {}).get("steps", [])) * 2
             for k in ("distribute", "accumulate")) + 1),
        ("히스테리시스", len(cfg.hysteresis)),
        ("합의 게이트", len(cfg.consensus)),
        ("바닥선·매수구간",
         len(cfg.floors.get("lines", [])) + len(cfg.floors.get("buy_zones", [])) * 2),
        ("프로파일 임계",
         len(cfg.bottom_profile.get("conditions", []))
         + len(cfg.top_profile.get("conditions", []))),
        ("LRS 가중·조정계수",
         len(cfg.lrs_components) + len(cfg.lrs_modifier.get("rules", [])) * 2),
    ]
    return rows


# ---------------------------------------------------------------------------
# 2. 교란
# ---------------------------------------------------------------------------

def scaled_anchors(base: StrategyConfig, scale_x: float = 1.0,
                   shift_y: float = 0.0) -> StrategyConfig:
    raw = copy.deepcopy(dict(base.raw))
    for spec in raw["indicators"].values():
        if "anchors" in spec:
            spec["anchors"] = [
                [x * scale_x, max(-1.0, min(1.0, y + shift_y))] for x, y in spec["anchors"]
            ]
    return StrategyConfig(raw=raw, path=base.path)


def equal_weights(base: StrategyConfig) -> StrategyConfig:
    raw = copy.deepcopy(dict(base.raw))
    fams = list(raw["bcs"]["families"])
    each = 100 // len(fams)
    for i, k in enumerate(fams):
        raw["bcs"]["families"][k]["weight"] = each + (100 - each * len(fams) if i == 0 else 0)
    return StrategyConfig(raw=raw, path=base.path)


def flipped_aggregate(base: StrategyConfig) -> StrategyConfig:
    raw = copy.deepcopy(dict(base.raw))
    raw["bcs"]["families"]["valuation"]["aggregate"] = "mean"
    return StrategyConfig(raw=raw, path=base.path)


# ---------------------------------------------------------------------------
# 3. 정규화 변형별 BCS 시계열
# ---------------------------------------------------------------------------

def bcs_series(cfg: StrategyConfig, market, *, mode: str, adaptive: float,
               history_for_all: bool = False) -> dict[date, Optional[float]]:
    """하루씩 BCS 를 만든다.

    mode='config'    설정대로 (앵커 + 지원 지표만 퍼센타일 혼합)
    mode='pure_pct'  모든 수치 지표를 4년 퍼센타일 점수로 직접 대체 (앵커 미사용)
    history_for_all  config 모드에서 모든 지표에 4년 이력을 넘겨 혼합 대상을 넓힌다
    """
    m = market.normalized()
    series = bt.build_indicator_series(m)
    lut = {k: dict(zip(s.dates, s.values)) for k, s in series.items()}
    numeric = [k for k in series if k != "hash_ribbons"]
    hist: dict[str, list[tuple[date, float]]] = {k: [] for k in numeric}
    out: dict[date, Optional[float]] = {}

    for d in m.price.dates:
        raws = {k: t.get(d) for k, t in lut.items() if t.get(d) is not None}
        for k in numeric:
            if k in raws:
                hist[k].append((d, float(raws[k])))

        def window(key: str) -> Optional[list[float]]:
            arr = hist.get(key, [])
            i = bisect.bisect_left(arr, (d - timedelta(days=PERCENTILE_DAYS), float("-inf")))
            w = [v for _, v in arr[i:]]
            return w if len(w) >= 90 else None

        readings: dict[str, Reading] = {}
        for key, spec in cfg.indicators.items():
            if mode == "pure_pct" and key in numeric:
                w = window(key)
                v = raws.get(key)
                score = percentile_rank(float(v), w) if (v is not None and w) else None
                readings[key] = Reading(key, spec.get("label", key), spec["family"],
                                        v, score, "auto")
                continue
            eligible = history_for_all or key in bt.ADAPTIVE_KEYS
            h = window(key) if (eligible and adaptive > 0) else None
            readings[key] = score_indicator(cfg, key, raws.get(key), history=h,
                                            adaptive_weight=adaptive, spec=spec)
        bcs, _, _ = compute_bcs(cfg, readings)
        out[d] = bcs
    return out


VARIANTS = (
    ("A. 고정 앵커만",          dict(mode="config", adaptive=0.0)),
    ("B. 현행 (65:35, 4개)",   dict(mode="config", adaptive=0.35)),
    ("C. 혼합 대상 전체 (0.35)", dict(mode="config", adaptive=0.35, history_for_all=True)),
    ("D. 전 지표 퍼센타일만",    dict(mode="pure_pct", adaptive=1.0)),
)


def _near(lookup: dict, target: date) -> Optional[float]:
    for off in range(NEAR_DAYS + 1):
        for d in (target - timedelta(days=off), target + timedelta(days=off)):
            if lookup.get(d) is not None:
                return lookup[d]
    return None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--manual", default="data/manual_input.example.yaml")
    args = ap.parse_args(argv)

    base = load_config()
    bundle = load_csv_bundle(args.csv)
    manual = load_manual(args.manual, reference=bundle.market.price.last_date)

    print("=" * 92)
    print("  과적합 노출도")
    print("=" * 92)

    # --- 1. 자유도 ---
    rows = degrees_of_freedom(base)
    total = sum(v for _, v in rows)
    print("\n[1] 조정 가능한 숫자")
    for lab, v in rows:
        print(f"      {lab:24s} {v:>4d}")
    print(f"      {'합계':24s} {total:>4d}")
    print(f"      유효 표본 = 사이클 4개 → 자유도/표본 = {total / 4:.0f}")
    print("      고전적으로는 재앙적 비율이다. 그래서 [2]에서 실제로 떠받치고")
    print("      있는지를 따로 잰다.")

    # --- 2. 교란 ---
    print("\n[2] 교란 실험 — 밴드와 DCA 가 바뀌는가")
    trials: list[tuple[str, StrategyConfig]] = []
    for sc in (0.7, 0.85, 1.0, 1.15, 1.3, 1.5):
        trials.append((f"앵커 x좌표 ×{sc:.2f}", scaled_anchors(base, scale_x=sc)))
    for sh in (-0.20, -0.10, 0.10, 0.20):
        trials.append((f"앵커 점수 {sh:+.2f} 편향", scaled_anchors(base, shift_y=sh)))
    trials.append(("계열 가중치 균등", equal_weights(base)))
    trials.append(("밸류에이션 집계 → 평균", flipped_aggregate(base)))

    seen_bands, seen_dca, bcs_vals = set(), set(), []
    for label, cfg in trials:
        snap, _ = evaluate(cfg, bundle=bundle, manual=manual, record=False)
        seen_bands.add(snap.plan.band_key)
        seen_dca.add(snap.plan.dca_multiplier)
        bcs_vals.append(snap.bcs)
        print(f"      {label:26s} BCS {snap.bcs:+7.1f}  {snap.plan.band_key:13s} "
              f"DCA×{snap.plan.dca_multiplier:.1f}")
    ref, _ = evaluate(base, bundle=bundle, manual=manual, record=False)
    print(f"      {'(교란 없음)':26s} BCS {ref.bcs:+7.1f}  {ref.plan.band_key:13s} "
          f"DCA×{ref.plan.dca_multiplier:.1f}")
    print(f"\n      BCS 폭 {min(bcs_vals):+.1f} ~ {max(bcs_vals):+.1f} "
          f"({max(bcs_vals) - min(bcs_vals):.1f}점)")
    print(f"      밴드 {sorted(seen_bands)}   DCA {sorted(seen_dca)}")
    if len(seen_dca) <= 2:
        print("      → 실행 결론(적립 배수)은 교란에 견딘다. 파라미터가 많은 것과")
        print("        결론이 그 파라미터에 달린 것은 다른 문제다.")
    else:
        print("      → 실행 결론이 교란에 흔들린다. 파라미터가 판단을 떠받치고 있다.")

    # --- 3. 정규화 일반화 ---
    print("\n[3] 정규화 일반화 — 같은 전환점에서 사이클마다 같은 값을 내는가")
    first = min(float(s["trigger_bcs"]) for s in base.ladders["distribute"]["steps"])
    print(f"      분배 첫 계단은 BCS {first:+.0f} 다. 고점에서 그보다 낮으면 아무것도 안 팔린다.")
    print(f"\n      {'정규화':24s}" + "".join(f"{t.year:>8d}고" for t in TOPS)
          + " │" + "".join(f"{b.year:>8d}저" for b in BOTTOMS) + "   고점σ  첫계단")
    print("      " + "-" * 106)
    for label, kw in VARIANTS:
        lut = bcs_series(base, bundle.market, **kw)
        tv = [_near(lut, t) for t in TOPS]
        bv = [_near(lut, b) for b in BOTTOMS]
        f = lambda xs: "".join(f"{x:+9.1f}" if x is not None else "        —" for x in xs)
        ok = [x for x in tv if x is not None]
        sd = statistics.pstdev(ok) if len(ok) > 1 else 0.0
        fired = sum(1 for x in tv if x is not None and x >= first)
        print(f"      {label:24s}{f(tv)} │{f(bv)}   {sd:5.1f}   {fired}/4")
    print("\n      고점σ 가 작을수록 사이클을 넘어 같은 값을 낸다는 뜻이다.")
    print("      '첫계단'은 네 고점 중 몇 곳에서 분배가 실제로 열렸는가다.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
