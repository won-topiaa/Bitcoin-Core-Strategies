#!/usr/bin/env python3
"""BTC 에 맞춘 설정을 다른 자산에 그대로 적용해 본다.

    python3 tools/cross_asset.py                     # 원격 조회
    python3 tools/cross_asset.py --csv-dir data/x    # 저장해 둔 CSV 사용
    python3 tools/cross_asset.py --save data/x       # 받아서 저장

## 왜 이게 과적합 대책 중 가장 강한가

이 저장소의 근본 문제는 **유효 표본이 비트코인 사이클 4개**라는 것이다.
파라미터를 줄여도 그 사실은 안 바뀐다. 표본을 실제로 늘리는 방법은 하나다 —
**설정을 만들 때 쓰지 않은 자산에 그대로 적용해 보는 것.**

라이트코인은 특히 값지다. PoW 이고 반감기가 있어 구조가 비트코인과 같은데
(2015-08 / 2019-08 / 2023-08), 데이터가 2011년부터 있어 사이클이 3~4개 더
들어온다. 이더리움은 2015년부터이고 2022년에 PoS 로 바뀌어 해시레이트가
끊기지만 밸류에이션·가격 계열은 그대로 계산된다.

전환점은 사람이 고르지 않는다. 가격에서 국소 극값을 자동 검출한다 — 그래야
"내가 아는 전환점만 골라서 맞췄다"가 되지 않는다. 그 대신 국소 극값에는
사이클 고점이 아닌 중간 랠리도 섞이므로, **놓친 것 전부가 실패는 아니다.**
그 판단은 사람이 표를 보고 해야 한다.

## 읽는 법

`포착`은 자동 검출된 전환점에서 BCS 가 사다리 첫 계단을 넘겼는가다.
고점이면 +45 이상, 저점이면 −30 이하.

같은 전환점에서 정규화 방식별로 비교하는 것이 핵심이다. 고정 앵커가
다른 자산에서도 최근 고점을 놓치면, 그건 비트코인 2025 사이클의 특수성이
아니라 고정 임계 자체의 성질이라는 뜻이다.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from btc_core.config import StrategyConfig, load_config       # noqa: E402
from btc_core.indicators import MarketData                    # noqa: E402
from btc_core.models import Reading                           # noqa: E402
from btc_core.score import compute_bcs, score_indicator       # noqa: E402
from btc_core.series import Series                            # noqa: E402

import backtest as bt                                         # noqa: E402

BASE_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
METRICS = ("PriceUSD", "CapMrktCurUSD", "CapMVRVCur", "SplyCur",
           "IssTotUSD", "IssTotNtv", "HashRate", "AdrActCnt")

ASSETS = (
    ("ltc", "라이트코인 (PoW·반감기, 2011~)"),
    ("eth", "이더리움 (2015~, 2022 PoS 전환)"),
)

EXTREMA_WINDOW = 270      # 앞뒤 이 기간 안에서 유일한 극값
SAME_PHASE_DAYS = 400     # 이 안의 같은 종류 극값은 한 국면으로 묶는다
PERCENTILE_DAYS = int(4 * 365.25)

MODES = ((0.0, "고정 앵커만"), (0.35, "현행 65:35"), (1.0, "퍼센타일만"))


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------

def fetch(asset: str, timeout: int = 120) -> list[dict]:
    params = {"assets": asset, "metrics": ",".join(METRICS), "frequency": "1d",
              "start_time": "2011-01-01", "end_time": date.today().isoformat(),
              "page_size": "10000"}
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    rows: list[dict] = []
    seen: set[str] = set()
    while url and url not in seen and len(seen) < 30:
        seen.add(url)
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "btc-core/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        rows.extend(payload.get("data", []))
        url = payload.get("next_page_url", "")
    return rows


def save_rows(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(("date",) + METRICS)
        for r in rows:
            w.writerow([r["time"][:10]] + [r.get(c, "") for c in METRICS])


def read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return [{"time": r["date"], **{k: r[k] for k in METRICS if r.get(k)}}
                for r in csv.DictReader(fh)]


def to_market(rows: list[dict]) -> MarketData:
    cols: dict[str, list[tuple[date, float]]] = {}
    for r in rows:
        try:
            d = date.fromisoformat(r["time"][:10])
        except (KeyError, ValueError):
            continue
        for k in METRICS:
            v = r.get(k)
            if v in (None, ""):
                continue
            try:
                cols.setdefault(k, []).append((d, float(v)))
            except (TypeError, ValueError):
                pass
    S = {k: Series.from_pairs(v, name=k) for k, v in cols.items()}
    if "PriceUSD" not in S:
        raise SystemExit("가격 데이터가 없습니다.")

    mc, mv = S.get("CapMrktCurUSD"), S.get("CapMVRVCur")
    realized = None
    if mc is not None and mv is not None:
        r = mc.map_with(mv, lambda c, m: c / m if m else None)
        realized = Series(r.dates, r.values, "realized_cap")

    return MarketData(
        price=S["PriceUSD"], market_cap=mc, realized_cap=realized,
        issuance_usd=S.get("IssTotUSD"), issuance_btc=S.get("IssTotNtv"),
        supply=S.get("SplyCur"), hashrate=S.get("HashRate"),
        active_addresses=S.get("AdrActCnt"),
    ).normalized()


# ---------------------------------------------------------------------------
# 전환점 자동 검출
# ---------------------------------------------------------------------------

def turning_points(m: MarketData) -> list[tuple[str, date, float]]:
    p = m.price
    vals = list(p.values)
    found: list[tuple[str, date, float]] = []
    for i in range(EXTREMA_WINDOW, len(vals) - EXTREMA_WINDOW):
        if vals[i] is None:
            continue
        seg = [v for v in vals[i - EXTREMA_WINDOW:i + EXTREMA_WINDOW + 1] if v is not None]
        if not seg:
            continue
        if vals[i] == max(seg):
            found.append(("고점", p.dates[i], vals[i]))
        elif vals[i] == min(seg):
            found.append(("저점", p.dates[i], vals[i]))

    merged: list[tuple[str, date, float]] = []
    for kind, d, v in found:
        if merged and merged[-1][0] == kind and (d - merged[-1][1]).days < SAME_PHASE_DAYS:
            better = (kind == "고점" and v > merged[-1][2]) or (kind == "저점" and v < merged[-1][2])
            if better:
                merged[-1] = (kind, d, v)
        else:
            merged.append((kind, d, v))
    return merged


# ---------------------------------------------------------------------------
# 평가
# ---------------------------------------------------------------------------

def bcs_map(cfg: StrategyConfig, m: MarketData,
            adaptive: float) -> dict[date, tuple[Optional[float], float]]:
    series = bt.build_indicator_series(m)
    lut = {k: dict(zip(s.dates, s.values)) for k, s in series.items()}
    numeric = [k for k in series if k != "hash_ribbons"]
    hist: dict[str, list[tuple[date, float]]] = {k: [] for k in numeric}
    out: dict[date, tuple[Optional[float], float]] = {}

    for d in m.price.dates:
        raws = {k: t.get(d) for k, t in lut.items() if t.get(d) is not None}
        for k in numeric:
            if k in raws:
                hist[k].append((d, float(raws[k])))
        readings: dict[str, Reading] = {}
        for key, spec in cfg.indicators.items():
            h = None
            if adaptive > 0 and key in numeric:
                arr = hist[key]
                i = bisect.bisect_left(arr, (d - timedelta(days=PERCENTILE_DAYS), float("-inf")))
                w = [v for _, v in arr[i:]]
                if len(w) >= 90:
                    h = w
            readings[key] = score_indicator(cfg, key, raws.get(key), history=h,
                                            adaptive_weight=adaptive, spec=spec)
        bcs, _, cov = compute_bcs(cfg, readings)
        out[d] = (bcs, cov)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv-dir", default=None, help="<asset>.csv 가 있는 디렉터리")
    ap.add_argument("--save", default=None, help="받은 데이터를 이 디렉터리에 저장")
    args = ap.parse_args(argv)

    cfg = load_config()
    first_d = min(float(s["trigger_bcs"]) for s in cfg.ladders["distribute"]["steps"])
    first_a = max(float(s["trigger_bcs"]) for s in cfg.ladders["accumulate"]["steps"])

    print("=" * 96)
    print("  교차자산 검증 — BTC 에 맞춘 설정을 그대로 다른 자산에 적용한다")
    print("=" * 96)
    print(f"  포착 기준: 고점에서 BCS ≥ {first_d:+.0f}, 저점에서 BCS ≤ {first_a:+.0f}")
    print("  (사다리 첫 계단. 그보다 못 가면 아무 실행도 열리지 않는다)")
    print("\n  주의: 전환점은 가격의 국소 극값을 자동 검출한 것이라 사이클 고점이")
    print("  아닌 중간 랠리도 섞인다. 놓친 것 전부가 실패는 아니다 — 표를 보고 판단할 것.")

    tally: dict[str, list[int]] = {label: [0, 0] for _, label in MODES}
    for asset, title in ASSETS:
        if args.csv_dir:
            rows = read_csv(Path(args.csv_dir) / f"{asset}.csv")
        else:
            rows = fetch(asset)
            if args.save:
                save_rows(rows, Path(args.save) / f"{asset}.csv")
        m = to_market(rows)
        tps = turning_points(m)
        print(f"\n{'─' * 96}\n  {title}   —  전환점 {len(tps)}곳 자동 검출  "
              f"({m.price.dates[0]} ~ {m.price.dates[-1]})")
        maps = {label: bcs_map(cfg, m, ad) for ad, label in MODES}

        head = f"  {'국면':4s} {'날짜':11s} {'가격':>11s}"
        for _, label in MODES:
            head += f" │ {label:>12s}"
        print(head)
        print("  " + "─" * 92)
        for kind, d, v in tps:
            line = f"  {kind:4s} {d} {v:>11,.2f}"
            for _, label in MODES:
                b, cov = maps[label].get(d, (None, 0.0))
                if b is None:
                    line += f" │ {'산출불가':>12s}"
                    continue
                ok = (b >= first_d) if kind == "고점" else (b <= first_a)
                tally[label][1] += 1
                tally[label][0] += 1 if ok else 0
                line += f" │ {'O' if ok else 'X'} {b:+9.1f}"
            print(line)

    print(f"\n{'=' * 96}")
    print("  종합 — 설정을 만들 때 쓰지 않은 자산에서의 전환점 포착률")
    for _, label in MODES:
        hit, tot = tally[label]
        pct = hit / tot * 100 if tot else 0
        print(f"      {label:14s} {hit:>3d}/{tot:<3d}  {pct:5.1f}%")
    print("\n  같은 전환점에서 방식별로 비교하는 것이 핵심이다. 고정 앵커가 다른")
    print("  자산에서도 최근 고점을 놓치면, 그건 비트코인 2025 사이클의 특수성이")
    print("  아니라 고정 임계 자체의 성질이라는 뜻이다.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
