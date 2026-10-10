#!/usr/bin/env python3
"""data/macro.csv 의 열마다 마지막 값이 얼마나 낡았는지 보고, 멈춘 열을 경고한다.

왜 있나 — 2026-10 감사에서 세 열이 조용히 멈춰 있었다. 아무도 몰랐다:

    m2_cn   2026-05 에서 멈춤  chinadata.live 원본이 멈췄다. 화면은 그 값을
                               '지금 임펄스'라 부르며 유동성 레짐을 매겼다
    vix     09-22 에서 멈춤     datahub 미러가 멈췄다(FRED 는 최신이었다)
    usdjpy  07-24 에서 멈춤     --global 계획이 같은 FRED id 를 다른 열로 덮었다

fetch_macro 는 열 단위로 병합해 한 소스가 막혀도 옛 값을 지킨다 — 그게 장점이지만,
같은 이유로 **소스가 영영 멈춰도 파일은 멀쩡해 보인다.** 그래서 열마다 나이를 잰다.

    python3 tools/macro_freshness.py data/macro.csv            # 경고만(종료 0)
    python3 tools/macro_freshness.py data/macro.csv --strict   # 멈춘 열이 있으면 1

경고는 GitHub Actions 의 ``::warning::`` 형식이라 실행 요약에 그대로 뜬다.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path
from typing import Optional

# 열 → 허용하는 최대 나이(일). 발표 주기와 발표 지연을 더하고 여유를 둔 값이다.
WATCH: dict[str, int] = {
    # 일간 — 길어야 며칠 늦는다
    "nasdaq": 14, "vix": 14, "us_rate": 14, "realyield": 14,
    "hy_spread": 14, "breakeven": 14, "curve": 14, "oil": 21,
    # 일간이지만 FRED 가 한 주쯤 늦게 싣는다(H.10 환율·주간 집계)
    "usdjpy": 30, "dxy": 30, "em_fx": 30, "net_liq": 30,
    # 월간 — 달 첫날 날짜로 실리고 다음 달 중순쯤 나온다
    "m2_cn": 100, "m2_us": 100, "sp500": 100, "gold": 100,
    "jp_rate": 120, "kospi": 120, "china_eq": 120,
    # 월간인데 두세 달 늦게 싣는다
    "copper": 150,
}
# 원본이 **끝난** 열 — OECD 광의통화가 2023-11 에 멈췄다(fetch_macro.GLOBAL 주석).
# 더 늘어날 수 없으므로 감시하지 않는다. 새 열은 둘 중 한쪽에 넣어야 한다
# (tests/test_macro_freshness.py 가 지킨다) — 말없이 감시 밖에 두지 않는다.
ENDED: frozenset[str] = frozenset({"m1_cn", "m2_jp", "m2_gb", "m2_eu", "m2_global"})


def last_dates(path: Path) -> dict[str, date]:
    out: dict[str, date] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                d = date.fromisoformat((row.get("date") or "")[:10])
            except ValueError:
                continue
            for col, v in row.items():
                if col == "date" or v in (None, ""):
                    continue
                if col not in out or d > out[col]:
                    out[col] = d
    return out


def judge(last: dict[str, date], today: date) -> tuple[list[str], list[str]]:
    """(멈춘 열 설명들, 분류 안 된 열들)."""
    stale = []
    for col, limit in WATCH.items():
        d = last.get(col)
        if d is None:
            stale.append(f"{col}: 값이 하나도 없습니다")
            continue
        age = (today - d).days
        if age > limit:
            stale.append(f"{col}: 마지막 값 {d} — {age}일 전(허용 {limit}일)")
    unknown = sorted(c for c in last if c not in WATCH and c not in ENDED)
    return stale, unknown


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv", nargs="?", default="data/macro.csv")
    ap.add_argument("--today", help="기준일(YYYY-MM-DD). 기본은 오늘(UTC 아님, 러너 시계)")
    ap.add_argument("--strict", action="store_true", help="멈춘 열이 있으면 종료 1")
    a = ap.parse_args(argv)

    path = Path(a.csv)
    if not path.exists():
        print(f"::warning::{path} 가 없습니다 — 거시 데이터 신선도를 잴 수 없습니다.")
        return 1 if a.strict else 0
    today = date.fromisoformat(a.today) if a.today else date.today()
    stale, unknown = judge(last_dates(path), today)
    for s in stale:
        print(f"::warning::거시 데이터가 멈췄습니다 — {s}")
    for c in unknown:
        print(f"::warning::{c} 열은 신선도 감시 목록(WATCH/ENDED)에 없습니다")
    if not stale and not unknown:
        print(f"거시 데이터 {len(WATCH)}개 열 모두 허용 범위 안입니다.")
    return 1 if (a.strict and stale) else 0


if __name__ == "__main__":
    sys.exit(main())
