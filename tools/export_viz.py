#!/usr/bin/env python3
"""시각화용 데이터 추출 — 하루씩 BCS 구간·계열 점수·밴드를 계산해 JSON 으로.

    python3 tools/export_viz.py --csv data/market.csv --out reports/viz-data.json
    python3 tools/export_viz.py --csv data/market.csv --stats-only

시각화 페이지는 **자기완결이어야 한다**(외부 요청 금지). 그래서 데이터를 미리
계산해 파일로 떨어뜨리고, 페이지가 그것을 인라인으로 품는다.

주 단위로 추려서 내보낸다. 16년 × 365일 = 5,855점을 그대로 넣으면 파일이
커지고, **사이클 위치를 보는 데 일 단위 해상도는 필요하지 않다.** 대신 사이클
전환점과 최근 구간은 일 단위로 남긴다.

동시에 구간 폭 통계를 출력한다 — 문서에 적히는 숫자가 이 도구에서 나온다.
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics as st
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest as bt  # noqa: E402

from btc_core.config import StrategyConfig, load_config  # noqa: E402
from btc_core.datasources.csv_source import load_csv_bundle  # noqa: E402
from btc_core.datasources.derive import backfill_bundle  # noqa: E402
from btc_core.indicators import HALVINGS, days_since_halving  # noqa: E402
from btc_core.models import Reading  # noqa: E402
from btc_core.score import (  # noqa: E402
    compute_bcs,
    compute_bcs_interval,
    evaluate_consensus,
    score_indicator,
)

# 사이클 전환점. docs/07 에서 확정한 날짜다.
TURNING_POINTS = (
    ("top", date(2013, 11, 30)),
    ("bottom", date(2015, 1, 14)),
    ("top", date(2017, 12, 17)),
    ("bottom", date(2018, 12, 15)),
    ("top", date(2021, 11, 10)),
    ("bottom", date(2022, 11, 21)),
    ("top", date(2025, 10, 6)),
    ("bottom", date(2026, 6, 6)),
)

WEEKLY_STRIDE = 7
NEAR_TURNING_DAYS = 45      # 전환점 주변은 일 단위로 남긴다
RECENT_DAILY_DAYS = 400     # 최근 구간도 일 단위


def daily_rows(cfg: StrategyConfig, market) -> list[dict]:
    """하루씩 전부 계산한다. 추리는 것은 나중 단계에서."""
    m = market.normalized()
    series = bt.build_indicator_series(m)
    lut = {k: dict(zip(s.dates, s.values)) for k, s in series.items()}
    prices = dict(zip(m.price.dates, m.price.values))
    adaptive = cfg.adaptive_weight

    hist: dict[str, list[tuple[date, float]]] = {k: [] for k in bt.ADAPTIVE_KEYS}
    rows: list[dict] = []

    for d in m.price.dates:
        raws = {k: v for k, t in lut.items() if (v := t.get(d)) is not None}
        for key in bt.ADAPTIVE_KEYS:
            if key in raws and isinstance(raws[key], (int, float)):
                hist[key].append((d, float(raws[key])))

        readings: dict[str, Reading] = {}
        for key, spec in cfg.indicators.items():
            h = None
            if key in bt.ADAPTIVE_KEYS and adaptive > 0:
                arr = hist[key]
                i = bisect.bisect_left(
                    arr, (d - timedelta(days=bt.HISTORY_DAYS), float("-inf"))
                )
                w = [v for _, v in arr[i:]]
                if len(w) >= 90:
                    h = w
            readings[key] = score_indicator(
                cfg, key, raws.get(key), history=h, adaptive_weight=adaptive, spec=spec
            )

        bcs, families, coverage = compute_bcs(cfg, readings)
        if bcs is None:
            continue
        iv = compute_bcs_interval(cfg, readings)
        cons = evaluate_consensus(cfg, families, bcs)
        fam = {f.key: (round(f.score, 3) if f.available else None) for f in families}
        missing = [r.label for r in readings.values() if not r.available]

        # 지표 하나하나의 현재 상태. 계열 점수만 내보내면 "왜 이 값이 나왔나"를
        # 화면에서 되짚을 수 없다. 점수는 2×퍼센타일−1 이라 되돌리면 곧
        # "최근 4년 중 몇 % 지점"이 되고, 그게 사람이 읽을 수 있는 형태다.
        ind = {}
        for key, r in readings.items():
            ind[key] = {
                "raw": (round(r.raw, 4) if isinstance(r.raw, (int, float))
                        else (r.raw if r.raw is not None else None)),
                "score": round(r.score, 3) if r.available else None,
                "pct": round((r.score + 1) / 2, 4) if r.available else None,
                "note": r.note or "",
            }

        rows.append({
            "d": d.isoformat(),
            "price": round(prices.get(d) or 0.0, 2),
            "bcs": round(bcs, 1),
            "low": round(iv.low, 1) if iv else round(bcs, 1),
            "high": round(iv.high, 1) if iv else round(bcs, 1),
            "band": cfg.band_for(bcs)["key"],
            "stable": bool(iv.stable) if iv else True,
            "cov": round(coverage, 3),
            "gate": bool(cons.passed),
            "fam": fam,
            # 계열 커버리지는 **멤버 결측을 숨긴다.** 밸류에이션은 max_abs 라
            # 서멀캡 하나만 살아 있어도 계열은 '있음'이 되고 커버리지는 100%
            # 로 나온다. 실제로 CoinMetrics 의 하루 지연이 그 상태를 만들고,
            # 그날 BCS 가 십수 점 움직인다. 그래서 멤버 결측 수를 따로 센다.
            "nmiss": len(missing),
            "miss": missing,
            "ind": ind,
            "dsh": days_since_halving(d),
        })
    return rows


def thin(rows: list[dict]) -> list[dict]:
    """주 단위로 추리되 전환점 주변과 최근 구간은 일 단위로 남긴다."""
    if not rows:
        return rows
    keep_dates: set[str] = set()
    for _, tp in TURNING_POINTS:
        for off in range(-NEAR_TURNING_DAYS, NEAR_TURNING_DAYS + 1):
            keep_dates.add((tp + timedelta(days=off)).isoformat())
    last = date.fromisoformat(rows[-1]["d"])
    for off in range(RECENT_DAILY_DAYS):
        keep_dates.add((last - timedelta(days=off)).isoformat())

    out = []
    for i, r in enumerate(rows):
        if i % WEEKLY_STRIDE == 0 or r["d"] in keep_dates or i == len(rows) - 1:
            slim = {k: v for k, v in r.items() if k not in ("miss", "ind")}
            out.append(slim)
    return out


def interval_stats(rows: list[dict]) -> dict:
    widths = [r["high"] - r["low"] for r in rows]
    q = sorted(widths)

    def pc(p: int) -> float:
        return round(q[max(0, int(len(q) * p / 100) - 1)], 1)

    stable = sum(1 for r in rows if r["stable"])
    return {
        "n": len(rows),
        "mean": round(st.mean(widths), 1),
        "median": round(st.median(widths), 1),
        "min": round(min(widths), 1),
        "max": round(max(widths), 1),
        "p10": pc(10), "p25": pc(25), "p75": pc(75), "p90": pc(90), "p99": pc(99),
        "stable_days": stable,
        "stable_pct": round(stable / len(rows) * 100, 1),
    }


def build(cfg: StrategyConfig, csv: str, *, derive: bool = True) -> dict:
    bundle = load_csv_bundle(csv)
    notes: tuple[str, ...] = ()
    if derive:
        # CoinMetrics 는 최신 하루가 늦다 — 마지막 행에 시총·유통량이 비어 있다.
        # 그대로 두면 마지막 날 커버리지가 60% 로 떨어져 "지금 위치"가 반쪽이 된다.
        # 파생으로 채우면 서멀캡이 살아나 밸류에이션 계열이 유지된다
        # (실현시총은 계산 불가라 MVRV·NUPL 은 여전히 결측).
        before = bundle.warnings
        bundle = backfill_bundle(bundle)
        notes = tuple(w for w in bundle.warnings if w not in before)
    market = bundle.market
    rows = daily_rows(cfg, market)
    if not rows:
        raise SystemExit("BCS 를 계산할 수 있는 날이 없습니다. CSV 를 확인하세요.")

    by_date = {r["d"]: r for r in rows}
    tps = []
    for phase, when in TURNING_POINTS:
        r = by_date.get(when.isoformat())
        if r:
            tps.append({"phase": phase,
                        **{k: v for k, v in r.items() if k not in ("miss", "ind")}})

    return {
        "generated_from": csv,
        "derive_notes": list(notes),
        "span": [rows[0]["d"], rows[-1]["d"]],
        "n_days": len(rows),
        "bands": [
            {"key": b["key"], "label": b["label"],
             "min": float(b["min"]), "max": float(b["max"])}
            for b in cfg.bands
        ],
        "families": [
            {"key": k, "label": f.get("label", k), "weight": float(f["weight"]),
             "aggregate": f.get("aggregate", "mean"),
             "explain": f.get("explain", ""),
             # 설명은 설정이 소유한다. 페이지에 따로 적어 두면 지표를 고칠 때
             # 둘이 어긋나고, 어긋난 설명은 없는 설명보다 나쁘다.
             "members": [
                 {"key": m,
                  "label": cfg.indicator(m).get("label", m),
                  "explain": cfg.indicator(m).get("explain", ""),
                  "explain_long": cfg.indicator(m).get("explain_long", "")}
                 for m in f["members"]
             ]}
            for k, f in cfg.bcs_families.items()
        ],
        "band_stances": {b["key"]: b.get("stance", "") for b in cfg.bands},
        "indicator_meta": [
            {"key": k,
             "label": s.get("label", k),
             "short": s.get("label", k).split(" (")[0],
             "family": s.get("family", ""),
             "unit": s.get("unit", ""),
             "categorical": s.get("input_mode") == "categorical",
             "explain": s.get("explain", ""),
             "explain_long": s.get("explain_long", "")}
            for k, s in cfg.indicators.items()
        ],
        "ladders": {
            "distribute": [
                {"bcs": float(s["trigger_bcs"]), "pct": float(s["pct_of_holdings"])}
                for s in cfg.ladders["distribute"]["steps"]
            ],
            "accumulate": [
                {"bcs": float(s["trigger_bcs"]), "pct": float(s["pct_of_reserve"])}
                for s in cfg.ladders["accumulate"]["steps"]
            ],
            "core_hold_pct": float(cfg.ladders["distribute"]["core_hold_pct"]),
        },
        "dca": {k: float(v) for k, v in cfg.ladders["dca_multiplier"].items()},
        "halvings": [h.isoformat() for h in HALVINGS],
        "turning_points": tps,
        "interval_stats": interval_stats(rows),
        # "지금 위치"의 기준일은 **커버리지가 온전한 마지막 날**이다.
        #
        # 실현시총은 계산으로 대체할 수 없고 커뮤니티 티어는 최신 하루가 늦다.
        # 그래서 마지막 하루는 밸류에이션 계열이 서멀캡 하나로 줄어드는데, 그
        # 상태의 점수를 헤드라인으로 쓰면 하루 지연 때문에 위치가 십수 점
        # 움직인 것을 "위치가 바뀌었다"로 읽게 된다. 온전한 날을 기준으로 삼고
        # 마지막 날은 따로 실어 둔다.
        "current": _last_full(rows),
        "latest": rows[-1],
        "series": thin(rows),
    }


def _last_full(rows: list[dict]) -> dict:
    """지표 9개가 전부 살아 있는 마지막 날. 헤드라인은 이 날을 쓴다."""
    for r in reversed(rows):
        if r["nmiss"] == 0:
            return r
    return rows[-1]


def print_stats(payload: dict) -> None:
    s = payload["interval_stats"]
    print("BCS 구간 폭 — 잭나이프 (지표 하나씩 제외)")
    print("=" * 68)
    print(f"  n={s['n']}  평균 {s['mean']}  중위 {s['median']}  "
          f"최소 {s['min']}  최대 {s['max']}")
    print(f"  p10 {s['p10']}   p25 {s['p25']}   p75 {s['p75']}   "
          f"p90 {s['p90']}   p99 {s['p99']}")
    print(f"  구간이 한 밴드 안에 들어오는 날 {s['stable_days']}/{s['n']} "
          f"= {s['stable_pct']}%")
    print("\n사이클 전환점에서의 구간")
    print("-" * 68)
    print(f"  {'국면':6s} {'날짜':12s} {'구간':>18s} {'폭':>6s}  밴드 안정")
    for t in payload["turning_points"]:
        label = "고점" if t["phase"] == "top" else "저점"
        w = t["high"] - t["low"]
        print(f"  {label:6s} {t['d']:12s} {t['low']:+7.1f} ~ {t['high']:+7.1f} "
              f"{w:6.1f}  {'O' if t['stable'] else 'X'}")
    c, latest = payload["current"], payload["latest"]
    print(f"\n현재(커버리지 온전) {c['d']}  ${c['price']:,.0f}  "
          f"BCS {c['low']:+.1f} ~ {c['high']:+.1f} (점 {c['bcs']:+.1f})")
    if latest["d"] != c["d"]:
        print(f"  마지막 행 {latest['d']} 은 지표 {latest['nmiss']}개 결측 "
              f"({', '.join(latest['miss'])}) — BCS {latest['bcs']:+.1f}, "
              f"헤드라인에서 제외")
    for n in payload["derive_notes"]:
        print(f"  파생: {n}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="시각화용 데이터 추출")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None, help="JSON 저장 경로")
    ap.add_argument("--stats-only", action="store_true", help="통계만 출력")
    ap.add_argument("--no-derive", action="store_true",
                    help="빠진 유통량·시총을 반감기 스케줄로 채우지 않는다")
    args = ap.parse_args(argv)

    cfg = load_config(args.config) if args.config else load_config()
    payload = build(cfg, args.csv, derive=not args.no_derive)
    print_stats(payload)

    if args.out and not args.stats_only:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")
        print(f"\n저장: {p}  ({p.stat().st_size / 1024:.0f} KB, "
              f"{len(payload['series'])}점)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
