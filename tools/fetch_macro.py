#!/usr/bin/env python3
"""거시 시계열을 FRED(미국 연준, **무료·키 불필요**)에서 받아 data/macro.csv 로.

    python3 tools/fetch_macro.py                 # 핵심(나스닥·미국 M2)만
    python3 tools/fetch_macro.py --global        # 전 세계 M2 집계까지
    python3 tools/fetch_macro.py --list          # 받을 시리즈 목록만 출력

FRED 는 CSV 를 키 없이 준다:

    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<시리즈ID>

**이 도구가 이 환경(에이전트 프록시)에서 막힐 수 있다.** 프록시가 정책상
fred.stlouisfed.org 로의 연결을 403 으로 거절하면, 받는 것은 여기서 안 된다.
그때는 FRED 가 열린 곳(로컬 등)에서 이 스크립트를 돌려 data/macro.csv 를
만든 뒤, 분석(tools/macro_correlation.py)만 이 환경에서 하면 된다.

## 시리즈

핵심 둘은 ID 가 확실하다.

    NASDAQCOM   나스닥 종합지수 (일간)
    M2SL        미국 M2 (월간, 계절조정, 십억 달러)

전 세계 M2 는 주요국 M2 를 **각국 통화로 받아 달러로 환산해 더한다.** 아래
국가별 M2·환율 ID 는 FRED 에서 바뀔 수 있으니 --list 로 확인하고, 안 받아지면
그 나라만 건너뛴다(그래도 나머지로 집계된다). "전 세계 vs 특정국" 비교가
목적이라 개별국 열도 그대로 남긴다.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="

# 확실한 핵심 — 항상 받는다.
CORE = {
    "NASDAQCOM": "nasdaq",
    "M2SL": "m2_us",           # 미국 M2, 십억 USD
}

# 전 세계 집계용 — 각국 M2(자국 통화)와 그 통화의 달러 환율.
# (m2 열, fx 열, fx 가 'USD per 1 unit' 인지 'units per USD' 인지)
# FRED 관례: DEXUSEU/DEXUSUK = USD per 1 (유로/파운드), DEXJPUS/DEXCHUS = 자국통화 per USD.
GLOBAL = {
    "eu": {"m2": "MYAGM2EZM196N", "fx": "DEXUSEU", "fx_is_usd_per_unit": True},
    "jp": {"m2": "MYAGM2JPM189S", "fx": "DEXJPUS", "fx_is_usd_per_unit": False},
    "cn": {"m2": "MYAGM2CNM189S", "fx": "DEXCHUS", "fx_is_usd_per_unit": False},
    "gb": {"m2": "MYAGM2GBM189S", "fx": "DEXUSUK", "fx_is_usd_per_unit": True},
}


def fetch_series(fred_id: str, timeout: int = 40) -> Optional[dict[date, float]]:
    """FRED CSV 한 시리즈. 정책 차단(403)이면 명확히 알리고 None."""
    req = urllib.request.Request(BASE + fred_id,
                                 headers={"User-Agent": "btc-core/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        # 정책 차단은 두 모습으로 온다: 프록시가 직접 403(HTTPError), 또는
        # CONNECT 터널을 403 으로 거절(URLError, reason 에 "403"). 둘 다 잡는다.
        code = getattr(exc, "code", None)
        blocked = code in (403, 407) or "403" in str(getattr(exc, "reason", exc))
        if blocked:
            raise SystemExit(
                "\nFRED 연결이 정책상 막혔습니다 (403).\n"
                "  이 환경의 에이전트 프록시가 fred.stlouisfed.org 를 거절합니다.\n"
                "  FRED 가 열린 곳(로컬 등)에서 이 스크립트를 돌려 data/macro.csv 를\n"
                "  만든 뒤, 분석만 여기서 하세요:\n"
                "    python3 tools/macro_correlation.py --macro data/macro.csv")
        print(f"  ! {fred_id}: {code or exc} — 건너뜀", file=sys.stderr)
        return None

    out: dict[date, float] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or row[0] in ("DATE", "observation_date"):
            continue
        try:
            out[date.fromisoformat(row[0][:10])] = float(row[1])
        except ValueError:
            continue                       # FRED 결측은 "." 로 온다
    return out or None


def merge_to_csv(columns: dict[str, dict[date, float]], path: Path) -> None:
    all_dates = sorted(set().union(*[set(s) for s in columns.values()]) if columns else set())
    names = list(columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date"] + names)
        for d in all_dates:
            w.writerow([d.isoformat()] +
                       [("" if d not in columns[n] else repr(columns[n][d])) for n in names])


def build_global(columns: dict[str, dict[date, float]]) -> Optional[dict[date, float]]:
    """미국 M2 + 각국 M2(달러 환산)의 합. 있는 나라만 더한다."""
    if "m2_us" not in columns:
        return None
    parts = [columns["m2_us"]]
    for cc, spec in GLOBAL.items():
        m2 = columns.get(f"m2_{cc}")
        fx = columns.get(f"fx_{cc}")
        if not m2 or not fx:
            continue
        usd: dict[date, float] = {}
        fxd = sorted(fx)
        for d, v in m2.items():
            # 가장 가까운 환율(월말 M2 vs 일간 환율)
            best, gap = None, 40
            for df in fxd:
                g = abs((df - d).days)
                if g < gap:
                    best, gap = df, g
            if best is None:
                continue
            rate = fx[best]
            usd[d] = v * rate if spec["fx_is_usd_per_unit"] else v / rate
        if usd:
            parts.append(usd)
    # 합 — 모든 시점에서 미국 M2 는 반드시 있고, 나머지는 있으면 더한다
    out: dict[date, float] = {}
    for d in parts[0]:
        total, ok = 0.0, True
        for p in parts:
            best, gap = None, 40
            for dp in p:
                g = abs((dp - d).days)
                if g < gap:
                    best, gap = dp, g
            if best is None:
                ok = False
                break
            total += p[best]
        if ok:
            out[d] = total
    return out or None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="FRED 에서 거시 시계열 받기 (무료)")
    ap.add_argument("--out", default="data/macro.csv")
    ap.add_argument("--global", dest="do_global", action="store_true",
                    help="주요국 M2 를 받아 전 세계 M2 집계까지")
    ap.add_argument("--list", action="store_true", help="받을 시리즈 목록만")
    args = ap.parse_args(argv)

    plan = dict(CORE)
    if args.do_global:
        for cc, spec in GLOBAL.items():
            plan[spec["m2"]] = f"m2_{cc}"
            plan[spec["fx"]] = f"fx_{cc}"

    if args.list:
        print("받을 FRED 시리즈 (id → 열):")
        for fid, col in plan.items():
            print(f"  {fid:<16} → {col}")
        print("\nFRED 에서 ID 를 확인하려면: https://fred.stlouisfed.org/series/<ID>")
        return 0

    columns: dict[str, dict[date, float]] = {}
    for fid, col in plan.items():
        print(f"받는 중 {fid} → {col}")
        s = fetch_series(fid)
        if s:
            columns[col] = s
            print(f"        {len(s)}행 ({min(s)} ~ {max(s)})")

    if args.do_global:
        g = build_global(columns)
        if g:
            columns["m2_global"] = g
            print(f"집계   m2_global {len(g)}행")
        # 환율 열은 결과 CSV 에서 뺀다 (분석에 직접 안 쓴다)
        columns = {k: v for k, v in columns.items() if not k.startswith("fx_")}

    if not columns:
        raise SystemExit("받은 시리즈가 없습니다.")
    merge_to_csv(columns, Path(args.out))
    print(f"\n저장: {args.out}  (열: {', '.join(columns)})")
    print("분석:  python3 tools/macro_correlation.py --macro " + args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
