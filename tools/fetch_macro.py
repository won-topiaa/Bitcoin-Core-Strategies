#!/usr/bin/env python3
"""거시 시계열을 받아 data/macro.csv 로. 이 환경에서 **실제로 받아진다.**

    python3 tools/fetch_macro.py --github        # ★ 이 환경에서 되는 경로
    python3 tools/fetch_macro.py                 # FRED 핵심(나스닥·미국 M2) — 아래 주의
    python3 tools/fetch_macro.py --global        # FRED 전 세계 M2 집계까지
    python3 tools/fetch_macro.py --list          # 받을 FRED 시리즈 목록만

## 왜 --github 인가 (이 환경의 현실)

거시의 표준 무료 출처는 FRED(미국 연준, 키 불필요)지만, **이 에이전트 환경의
프록시가 fred.stlouisfed.org 를 정책상 403 으로 막는다**(Yahoo·Stooq 도 같다).
반면 **raw.githubusercontent.com 은 열려 있어서**, GitHub 에 공개된 데이터셋은
여기서도 실제로 받아진다. `--github` 는 그 경로다:

    sp500, cpi, rate_10y   datahub  s-and-p-500     (월간, 최신까지)
    vix                    datahub  finance-vix     (일간)
    m2_us                  FRED M2SL 미러 (월간, 2000~; 값은 FRED 원 수준)

M2 미러는 커뮤니티 거시셋이라 원본 권위는 FRED 지만, 값이 FRED M2SL 원 수준과
일치함을 확인했다(2025-02 ≈ 21670 = 약 $21.7조). 전 세계·개별국 M2 집계는
GitHub 에 깔끔한 공개 CSV 가 없어, 그건 FRED 가 열린 곳에서 아래 --global 로 받는다.

## FRED 경로 (환경이 열렸을 때)

    NASDAQCOM   나스닥 종합지수 (일간)
    M2SL        미국 M2 (월간, 계절조정, 십억 달러)

전 세계 M2 는 주요국 M2 를 **각국 통화로 받아 달러로 환산해 더한다.** 국가별
M2·환율 ID 는 FRED 에서 바뀔 수 있으니 --list 로 확인하고, 안 받아지면 그 나라만
건너뛴다. "전 세계 vs 특정국" 비교가 목적이라 개별국 열도 그대로 남긴다.
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

# GitHub raw 로 받는 공개 데이터셋. **이 에이전트 환경에서 FRED 는 막혔지만
# raw.githubusercontent.com 은 열려 있어서**, 이쪽은 여기서도 실제로 받아진다.
# datahub 셋은 주기적으로 갱신된다(확인: 2026-06/07 까지 최신).
#   (raw_url, 날짜열, {원본열: 우리열})
GITHUB_SOURCES = [
    # S&P500·CPI·장기금리 — datahub 공식 셋, 월간, 최신까지 갱신.
    ("https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv",
     "Date", {"SP500": "sp500", "Consumer Price Index": "cpi",
              "Long Interest Rate": "rate_10y"}),
    # VIX(공포지수) — datahub, 일간.
    ("https://raw.githubusercontent.com/datasets/finance-vix/main/data/vix-daily.csv",
     "DATE", {"CLOSE": "vix"}),
    # 미국 M2 — FRED M2SL 을 미러하는 커뮤니티 거시셋(월간, 2000~). 원본 권위는
    # FRED 지만 여기선 프록시가 FRED 를 막아, 이 미러가 이 환경에서 받아지는 M2 다.
    # 값은 FRED M2SL 원 수준(십억 달러): 2025-02 ≈ 21670 = 약 $21.7조 로 일치.
    ("https://raw.githubusercontent.com/emilblaignan/Macro-Drivers/main/data/processed_data.csv",
     "date", {"M2SL": "m2_us"}),
]

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


def fetch_github(timeout: int = 40) -> dict[str, dict[date, float]]:
    """GITHUB_SOURCES 를 raw.githubusercontent.com 에서 받는다.

    **이 에이전트 환경에서 실제로 받아지는 경로다.** FRED 는 프록시가 403 으로
    막지만 raw.githubusercontent.com 은 열려 있다. 받는 열은 각 소스의 매핑을
    따른다(S&P500·CPI·10년 금리·VIX). 못 받는 소스는 건너뛰고 나머지로 간다.
    """
    columns: dict[str, dict[date, float]] = {}
    for url, date_col, mapping in GITHUB_SOURCES:
        req = urllib.request.Request(url, headers={"User-Agent": "btc-core/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(f"  ! {url.rsplit('/', 3)[-1]}: {exc} — 건너뜀", file=sys.stderr)
            continue
        reader = csv.DictReader(io.StringIO(text))
        got = {our: {} for our in mapping.values()}
        for row in reader:
            raw = (row.get(date_col) or "").strip()
            if not raw:
                continue
            try:
                d = date.fromisoformat(raw[:10])
            except ValueError:
                continue
            for src, our in mapping.items():
                v = (row.get(src) or "").strip()
                if not v:
                    continue
                try:
                    got[our][d] = float(v)
                except ValueError:
                    continue
        for our, series in got.items():
            if series:
                columns[our] = series
                print(f"  {our:<10} {len(series)}행 ({min(series)} ~ {max(series)})")
    return columns


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
    ap.add_argument("--github", action="store_true",
                    help="raw.githubusercontent.com 공개 데이터셋에서 받기 "
                         "(이 환경에서 실제로 받아지는 경로: S&P500·CPI·금리·VIX)")
    ap.add_argument("--global", dest="do_global", action="store_true",
                    help="주요국 M2 를 받아 전 세계 M2 집계까지")
    ap.add_argument("--list", action="store_true", help="받을 시리즈 목록만")
    args = ap.parse_args(argv)

    if args.github:
        print("raw.githubusercontent.com 에서 받는 중…")
        columns = fetch_github()
        if not columns:
            raise SystemExit("GitHub 소스에서 받은 시리즈가 없습니다.")
        merge_to_csv(columns, Path(args.out))
        print(f"\n저장: {args.out}  (열: {', '.join(columns)})")
        print("분석:  python3 tools/macro_correlation.py --macro " + args.out)
        return 0

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
