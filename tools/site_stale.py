#!/usr/bin/env python3
"""배포된 사이트가 뒤처졌는지 판정한다 — 자동 갱신의 **결과**를 검사하는 자리.

## 왜 필요한가

이 저장소의 자동 갱신은 여러 번 조용히 멈췄고, 멈춘 이유가 매번 달랐다.

    · 낡은 빌드가 신선한 빌드를 덮어썼다        (배포 정문이 둘이었다)
    · 토큰 push 가 배포를 트리거하지 못했다       (뉴스가 한 번도 안 올라갔다)
    · 짧은 시간에 여러 번 푸시하자 실행이 전부 취소됐다 (동시성 큐에서 유실)

앞의 둘은 원인을 찾아 고쳤다. 그런데 **셋 다 공통점이 하나 있다** — 갱신이
안 됐다는 사실 자체를 아무도 알아채지 못했다는 것. 원인을 하나씩 막는 것만으로는
부족하고, **결과를 보는 감시자**가 있어야 한다. 이 파일이 그 판정을 맡는다.

## 두 가지 뒤처짐

1. **데이터가 낡음** — 구운 페이지의 기준일이 오늘로부터 너무 멀다.
   원본(CoinMetrics 커뮤니티 티어)이 하루 늦는 것은 정상이라 여유를 둔다.

2. **소스보다 낡음** — 사이트를 만드는 코드(`viz/`, `tools/`, `src/`, `config/`)가
   `viz/site/` 보다 나중에 커밋됐다. 고친 내용이 화면에 반영되지 않은 상태인데,
   기준일만 보면 멀쩡해 보여서 **1번 검사로는 절대 안 잡힌다.** 실제로 이 경우가
   있었다 — 관련성 지도를 월간 격자로 고친 커밋 4개가 재빌드되지 않아, 저장소는
   맞는데 화면은 옛 주간 값을 계속 보여 줬다.

두 함수 모두 순수하다(시간·git 을 인자로 받는다). 워크플로가 값을 넘기고,
테스트가 경계를 못 박는다.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from typing import Optional

# 원본이 하루 늦는 것은 정상이다. 이틀까지는 기다리고, 사흘째부터 뒤처짐으로 본다
# (화면의 '갱신이 멈췄을 수 있습니다' 경고도 3일을 쓴다 — 같은 기준으로 맞춘다).
MAX_DATA_AGE_DAYS = 3

# 사이트를 만드는 입력들. 이 밖의 변경(문서·테스트)은 화면을 바꾸지 않으므로
# 재빌드를 요구하지 않는다.
SOURCE_PATHS = ("viz/", "tools/", "src/", "config/")
SITE_PATH = "viz/site/"


def data_is_stale(site_asof: Optional[str], today: date,
                  max_age: int = MAX_DATA_AGE_DAYS) -> tuple[bool, str]:
    """구운 페이지의 기준일이 오늘로부터 너무 먼가."""
    if not site_asof:
        return True, "구운 페이지에서 기준일을 읽지 못했습니다 — 빌드가 깨졌을 수 있습니다"
    try:
        asof = date.fromisoformat(site_asof)
    except ValueError:
        return True, f"기준일 형식이 이상합니다: {site_asof!r}"
    age = (today - asof).days
    if age < 0:
        return True, f"기준일 {site_asof} 이 오늘({today})보다 미래입니다 — 데이터가 손상됐습니다"
    if age > max_age:
        return True, f"기준일 {site_asof} 이 {age}일 전입니다 (허용 {max_age}일) — 갱신이 멈췄습니다"
    return False, f"기준일 {site_asof} · {age}일 전 — 정상"


def source_is_newer(source_epoch: Optional[int],
                    site_epoch: Optional[int]) -> tuple[bool, str]:
    """사이트를 만드는 코드가 구운 결과보다 나중에 커밋됐는가.

    이 검사가 없으면 '기준일은 오늘인데 내용은 옛 코드로 구운' 상태를 못 잡는다.
    """
    if source_epoch is None or site_epoch is None:
        return False, "비교할 커밋 시각이 없습니다 — 판정하지 않습니다"
    if source_epoch > site_epoch:
        gap = source_epoch - site_epoch
        return True, (f"사이트를 만드는 소스가 구운 결과보다 {gap // 60}분 나중입니다 "
                      "— 고친 내용이 화면에 반영되지 않았습니다")
    return False, "구운 결과가 소스보다 최신입니다 — 정상"


def _git_epoch(paths: tuple[str, ...] | str) -> Optional[int]:
    """해당 경로를 마지막으로 건드린 커밋의 committer 시각(epoch)."""
    args = ["git", "log", "-1", "--format=%ct", "--"]
    args += list(paths) if isinstance(paths, tuple) else [paths]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    s = out.stdout.strip()
    return int(s) if s.isdigit() else None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="배포된 사이트가 뒤처졌는지 판정")
    ap.add_argument("--asof", default=None, help="구운 페이지의 기준일 (YYYY-MM-DD)")
    ap.add_argument("--today", default=None, help="오늘 날짜 (기본: 시스템)")
    ap.add_argument("--max-age", type=int, default=MAX_DATA_AGE_DAYS)
    args = ap.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else date.today()
    stale_data, why_data = data_is_stale(args.asof, today, args.max_age)
    stale_src, why_src = source_is_newer(_git_epoch(SOURCE_PATHS), _git_epoch(SITE_PATH))

    print(f"데이터  {why_data}")
    print(f"소스    {why_src}")
    if stale_data or stale_src:
        print("판정: 뒤처짐 — 재빌드가 필요합니다")
        return 1
    print("판정: 최신")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
