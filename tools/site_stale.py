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

## 세 가지 뒤처짐

1. **데이터가 낡음** — 구운 페이지의 기준일이 오늘로부터 너무 멀다.
   원본(CoinMetrics 커뮤니티 티어)이 하루 늦는 것은 정상이라 여유를 둔다.

2. **소스와 어긋남** — 사이트를 만드는 코드(`viz/`, `tools/`, `src/`, `config/`)가
   바뀌었는데 그 코드로 다시 굽지 않았다. 기준일만 보면 멀쩡해 보여서 **1번
   검사로는 절대 안 잡힌다.** 실제로 이 경우가 있었다 — 관련성 지도를 월간
   격자로 고친 커밋 4개가 재빌드되지 않아, 저장소는 맞는데 화면은 옛 주간 값을
   계속 보여 줬다.

3. **정기 갱신이 아예 안 돎** — 매일 도는 워크플로가 실패하거나 취소됐다.
   1번은 사흘이 지나야 울리므로 그 사이 이틀을 놓친다. 마지막으로 **성공한**
   갱신이 언제였는지를 따로 본다.

## 2번을 시각이 아니라 **커밋 SHA** 로 재는 이유

처음에는 `viz/site/` 와 소스의 마지막 커밋 **시각**을 비교했다. 두 군데서 틀렸다.

- 뉴스 갱신은 소스를 다시 굽지 않으면서 `viz/site/` 만 건드린다. 그래서 아직
  반영 안 된 소스 변경이 있어도 뉴스 커밋 하나면 "사이트가 더 최신"이 되어
  **검사가 통째로 무력해졌다.**
- 같은 날 두 번 구우면 결과가 바이트 단위로 같아 커밋이 안 생긴다. 그러면
  `viz/site/` 시각이 영원히 안 움직여서, 감시자가 **3시간마다 무한히**
  재빌드를 부르고도 상태가 안 변했다.

지금은 굽는 쪽이 **그때의 소스 SHA 를 페이지에 찍는다**(`build_viz._stamp`).
감시자는 찍힌 SHA 와 현재 소스 SHA 를 비교한다. 뉴스 커밋은 SHA 를 안 바꾸고,
소스만 바뀐 재빌드는 스탬프가 달라지므로 결과가 반드시 한 번은 커밋된다 —
두 실패가 같은 방식으로 사라진다.

순수 함수들(시간·git·HTTP 를 인자로 받는다)이 판정을 소유하고, 워크플로는 값을
넘기기만 한다. tests/test_site_stale.py 가 경계를 못 박는다.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from datetime import date
from typing import Optional

# 원본이 하루 늦는 것은 정상이다. 이틀까지는 기다리고, 사흘째부터 뒤처짐으로 본다
# (화면의 '갱신이 멈췄을 수 있습니다' 경고도 3일을 쓴다 — 같은 기준으로 맞춘다).
MAX_DATA_AGE_DAYS = 3

# 정기 갱신은 하루 한 번(02:20 UTC) 돈다. 26시간이면 한 번 걸러진 것이고,
# 그건 데이터 나이가 사흘이 되기를 기다릴 일이 아니라 지금 되살릴 일이다.
MAX_REFRESH_GAP_HOURS = 26

# 사이트를 만드는 입력들. `viz/site` 는 **결과물**이라 뺀다 — 넣으면 빌드 커밋
# 자신이 '소스가 바뀌었다'로 잡혀 감시자가 자기 꼬리를 문다.
SOURCE_SPEC = ("viz", "tools", "src", "config", ":(exclude)viz/site")

# 굽는 쪽이 페이지 끝에 남기는 표식. 형식을 바꾸면 양쪽이 같이 바뀌어야 한다.
STAMP_RE = re.compile(r"<!--\s*bcs-source:\s*([0-9a-f]{7,40}|unknown)\s*-->")


def stamp_line(sha: Optional[str]) -> str:
    """페이지 맨 끝에 붙일 표식 한 줄. 굽는 쪽과 읽는 쪽이 이 함수를 공유한다."""
    return f"<!-- bcs-source: {sha or 'unknown'} -->"


def read_stamp(html: str) -> Optional[str]:
    """구운 페이지에서 소스 SHA 표식을 읽는다. 없으면 None.

    **마지막** 것을 쓴다. 표식은 굽기의 맨 마지막에 붙으므로, 본문(뉴스 제목 등)에
    우연히 같은 모양이 들어와도 뒤에 오는 진짜가 이긴다.
    """
    found = STAMP_RE.findall(html or "")
    if not found:
        return None
    return None if found[-1] == "unknown" else found[-1]


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


def source_changed(stamped_sha: Optional[str],
                   current_sha: Optional[str]) -> tuple[bool, str]:
    """구울 때의 소스와 지금의 소스가 다른가.

    한쪽이라도 모르면 판정하지 않는다 — 모른다고 재빌드를 무한히 부르면 감시자가
    스스로 소음이 된다(예전에 실제로 그랬다).
    """
    if not stamped_sha or not current_sha:
        return False, "소스 표식이 없어 판정하지 않습니다 (예전 빌드이거나 git 을 못 읽었습니다)"
    if stamped_sha != current_sha:
        return True, (f"구울 때의 소스 {stamped_sha[:9]} ≠ 지금의 소스 {current_sha[:9]} "
                      "— 고친 내용이 화면에 반영되지 않았습니다")
    return False, f"소스 {current_sha[:9]} 로 구운 페이지 — 정상"


def refresh_is_overdue(last_success_epoch: Optional[int], now_epoch: int,
                       max_hours: int = MAX_REFRESH_GAP_HOURS) -> tuple[bool, str]:
    """마지막으로 **성공한** 정기 갱신이 너무 오래됐는가.

    데이터 나이(사흘)만 보면 실패한 정기 갱신을 이틀 동안 못 잡는다. 실패는
    실패한 그날 잡아야 한다.
    """
    if last_success_epoch is None:
        return True, "성공한 갱신 기록을 찾지 못했습니다 — 워크플로가 한 번도 통과하지 못했을 수 있습니다"
    gap = now_epoch - last_success_epoch
    if gap < 0:
        return False, "마지막 갱신이 미래로 기록돼 있습니다 — 판정하지 않습니다"
    hours = gap / 3600
    if hours > max_hours:
        return True, f"마지막 성공한 갱신이 {hours:.1f}시간 전입니다 (허용 {max_hours}시간)"
    return False, f"마지막 성공한 갱신 {hours:.1f}시간 전 — 정상"


def _git(args: list[str]) -> Optional[str]:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    s = out.stdout.strip()
    return s or None


def source_sha() -> Optional[str]:
    """사이트를 만드는 코드를 마지막으로 건드린 커밋 SHA."""
    return _git(["git", "log", "-1", "--format=%H", "--", *SOURCE_SPEC])


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="배포된 사이트가 뒤처졌는지 판정")
    ap.add_argument("--asof", default=None, help="구운 페이지의 기준일 (YYYY-MM-DD)")
    ap.add_argument("--today", default=None, help="오늘 날짜 (기본: 시스템)")
    ap.add_argument("--max-age", type=int, default=MAX_DATA_AGE_DAYS)
    ap.add_argument("--site", default="viz/site/index.html",
                    help="소스 표식을 읽을 페이지")
    ap.add_argument("--last-refresh", type=int, default=None,
                    help="마지막으로 성공한 정기 갱신의 epoch 초. 주지 않으면 이 검사를 건너뛴다")
    ap.add_argument("--now", type=int, default=None, help="현재 시각 epoch 초 (기본: 시스템)")
    args = ap.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else date.today()
    stale_data, why_data = data_is_stale(args.asof, today, args.max_age)

    try:
        html = open(args.site, encoding="utf-8").read()
    except OSError:
        html = ""
    stale_src, why_src = source_changed(read_stamp(html), source_sha())

    print(f"데이터  {why_data}")
    print(f"소스    {why_src}")

    stale_run = False
    if args.last_refresh is not None:
        now = args.now if args.now is not None else int(time.time())
        stale_run, why_run = refresh_is_overdue(args.last_refresh, now)
        print(f"정기실행 {why_run}")

    if stale_data or stale_src or stale_run:
        print("판정: 뒤처짐 — 재빌드가 필요합니다")
        return 1
    print("판정: 최신")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
