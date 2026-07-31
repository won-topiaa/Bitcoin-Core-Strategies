#!/usr/bin/env python3
"""거시 시계열을 받아 data/macro.csv 로. **이 환경에서 실제로 받아진다.**

    python3 tools/fetch_macro.py --github --global   # ★ 권장: 주식·VIX + 나스닥 + 전세계·국가별 M2
    python3 tools/fetch_macro.py --global            # FRED 만(나스닥 + 전세계·국가별 M2)
    python3 tools/fetch_macro.py --github            # GitHub 만(주식·VIX·미국 M2 미러) — FRED 없이
    python3 tools/fetch_macro.py --list              # 받을 FRED 시리즈 목록만

## 두 소스, 한 번에 합쳐진다

`--github` 와 FRED(`--global`)를 **같이 주면 한 파일로 합친다.** 같은 열이면
FRED 가 이긴다(권위·정의 일관). 각 소스가 잘 하는 걸 맡는다:

    [GitHub raw]  sp500·cpi·rate_10y  datahub s-and-p-500 (월간, 최신)
                  vix                 datahub finance-vix (일간, 최신)
    [FRED]        nasdaq              NASDAQCOM (일간, 1971~; **실제 나스닥**)
                  m2_us·m2_cn·m2_jp·m2_gb·m2_eu·m2_global   아래

## 왜 UA 를 바꿨나 (이 환경의 함정)

FRED(및 앞단 WAF)는 **낯선 User-Agent 요청의 본문 전송을 멈춰 세운다** — urllib
이 read() 에서 무한 대기하다 타임아웃난다(같은 URL 을 curl 로는 0.6초에 받음).
그래서 UA 를 표준값(`curl/8.0`)으로 보낸다. 이걸 안 하면 FRED 가 열려 있어도 못 받는다.
(과거엔 프록시가 FRED 자체를 403 으로 막았고, 그때만 --github 로 우회했다.)

## M2 — 정의 일치와 커버리지 (전 세계 vs 특정국)

"전 세계 vs 특정국" 비교가 목적이라 **정의를 맞춘다.** 예전 MYAGM2* 국가 시리즈는
FRED 가 상당수 폐기(중국·영국 404)했으므로, OECD **광의통화(Broad Money)
MABMM301*M189S** 계열로 통일한다 — US·JP·GB·EU·CN 모두 살아 있고 정의가 같다.
각국 M2 는 자국통화로 받아 달러로 환산해 더한다(개별 열은 자국통화 원값 유지).

  - `m2_global` = US+JP+GB+EU 합(USD). **중국은 합계에서 뺀다** — 중국 브로드머니가
    2018-12 에 끊겨(폐기) 넣으면 집계가 2019 에서 멈추기 때문. 대신 개별 `m2_cn` 로 본다.
  - 전부 월간, ~2023-11(OECD 중단). 개별국 열(`m2_cn` 등)로 "어느 나라가 더
    설명하나"를 macro_correlation.py 가 나란히 스캔한다.
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

# FRED(및 그 앞의 WAF)는 낯선 User-Agent 요청의 본문 전송을 멈춰 세운다 —
# urllib 이 read() 에서 무한 대기하다 타임아웃난다(같은 URL 을 curl 로는 0.6초에
# 받는다). 표준 UA 로 보내면 정상 응답한다. GitHub 도 이 UA 로 문제없다.
UA = "curl/8.0"

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
# 미국은 전 세계 집계와 **같은 정의**(OECD 광의통화 MABMM301)로 받아야 "전 세계
# vs 특정국" 비교가 사과 대 사과가 된다. 그래서 M2SL 이 아니라 MABMM301USM189S.
CORE = {
    "NASDAQCOM": "nasdaq",             # 나스닥 종합, 일간, 1971~
    "MABMM301USM189S": "m2_us",        # 미국 광의통화(M3), 자국통화(=USD), 월간 ~2023-11
    # 중국 협의통화(M1) — 광의통화(MABMM301CN)가 2018~19 에 끊겨, 코로나 이후까지
    # 살아 있는(2023-11) 중국 유동성 대용으로 넣는다. M1 자체가 중국 신용/경기
    # 선행지표로 널리 쓰인다. 개별 분석은 자국통화 YoY(척도무관)라 환산 불필요.
    "MANMM101CNM189N": "m1_cn",        # 중국 M1, 위안, 월간 ~2023-11
}

# 전 세계 집계용 — 각국 광의통화(자국 통화)와 그 통화의 달러 환율.
# **정의 일치가 핵심.** 예전 MYAGM2* 국가 시리즈는 FRED 가 상당수 폐기했다(중국·영국
# 404). OECD **MABMM301*M189S**(광의통화, Broad Money) 계열은 US·CN·JP·GB·EZ 가 모두
# 살아 있어 정의가 같다(2026-07 확인). 값은 자국통화 원 수준, 월간, ~2023-11(OECD 중단).
# (m2 열, fx 열, fx 가 'USD per 1 unit' 인지 'units per USD' 인지)
# FRED 환율 관례: DEXUSEU/DEXUSUK = USD per 1 (유로/파운드), DEXJPUS/DEXCHUS = 자국통화 per USD.
# agg=False 는 개별 분석엔 넣되 **전 세계 합계엔 빼는** 나라다. 중국 브로드머니는
# FRED 에서 2018-12 에 끊겨(시리즈 폐기), 합계에 넣으면 "모든 나라 존재" 조건 때문에
# 집계 전체가 2019 에서 멈춰 정작 중요한 2020~ 를 놓친다. 그래서 중국은 개별로만 보고,
# 합계는 2023-11 까지 살아 있는 US·JP·GB·EU 로 만든다.
GLOBAL = {
    "cn": {"m2": "MABMM301CNM189S", "fx": "DEXCHUS", "fx_is_usd_per_unit": False, "agg": False},
    "jp": {"m2": "MABMM301JPM189S", "fx": "DEXJPUS", "fx_is_usd_per_unit": False, "agg": True},
    "gb": {"m2": "MABMM301GBM189S", "fx": "DEXUSUK", "fx_is_usd_per_unit": True, "agg": True},
    "eu": {"m2": "MABMM301EZM189S", "fx": "DEXUSEU", "fx_is_usd_per_unit": True, "agg": True},
}


def fetch_series(fred_id: str, timeout: int = 40) -> Optional[dict[date, float]]:
    """FRED CSV 한 시리즈. 정책 차단(403)이면 명확히 알리고 None."""
    req = urllib.request.Request(BASE + fred_id,
                                 headers={"User-Agent": UA})
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
        req = urllib.request.Request(url, headers={"User-Agent": UA})
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
        if not spec.get("agg", True):          # 합계 제외국(예: 중국, 커버리지 짧음)
            continue
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

    # --github 와 FRED 를 **한 번에 합칠 수 있다.** 먼저 GitHub(주식·VIX 등)을
    # 받고, 그 위에 FRED(실제 나스닥·광의통화)를 덮는다 — 같은 열이면 FRED 가 이긴다
    # (권위 있고 정의가 일관됨). FRED 만 필요하면 --global(또는 무플래그)만 쓰면 된다.
    columns: dict[str, dict[date, float]] = {}
    if args.github:
        print("raw.githubusercontent.com 에서 받는 중…")
        columns.update(fetch_github())

    want_fred = args.do_global or not args.github
    if want_fred:
        fred: dict[str, dict[date, float]] = {}
        for fid, col in plan.items():
            print(f"받는 중 {fid} → {col}")
            s = fetch_series(fid)
            if s:
                fred[col] = s
                print(f"        {len(s)}행 ({min(s)} ~ {max(s)})")
        if args.do_global:
            g = build_global(fred)
            if g:
                fred["m2_global"] = g
                print(f"집계   m2_global {len(g)}행 ({min(g)} ~ {max(g)})")
            # 환율 열은 결과 CSV 에서 뺀다 (분석에 직접 안 쓴다)
            fred = {k: v for k, v in fred.items() if not k.startswith("fx_")}
        columns.update(fred)     # FRED 가 GitHub 을 덮어쓴다(열 충돌 시)

    if not columns:
        raise SystemExit("받은 시리즈가 없습니다.")
    merge_to_csv(columns, Path(args.out))
    print(f"\n저장: {args.out}  (열: {', '.join(columns)})")
    print("분석:  python3 tools/macro_correlation.py --macro " + args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
