#!/usr/bin/env python3
"""헤드라인이 보류된 날의 온체인 지표가 원본에 올라왔는지 **한 번** 묻는다.

왜 따로 기다려야 하나 — 원본(CoinMetrics)은 가격을 UTC 자정 직후에 내지만
실현시총(CapMrktCurUSD/CapMVRVCur)은 2~5.5시간 뒤에 낸다(실측 +2.09h~+5.47h).
그 사이에 갱신이 돌면 가격만 들어와 그 날은 헤드라인에서 빠지고(지표 9개가 다
있는 날만 헤드라인이 된다), 화면은 하루 전 날짜에 머문다. 다음 갱신이 와야
따라잡는데, GitHub 의 예약 실행은 매시간으로 걸어도 실측 하루 4~5회뿐이고
**02:06~05:27 UTC 사이에는 한 번도 돌지 않았다** — 실현시총이 나오는 바로 그
시간대다. 그래서 매일 아침 몇 시간씩 '데이터 2일 전'이 떴다.

예약은 못 믿지만 workflow_dispatch 는 12초 안에 뜬다(실측). 그래서 갱신이
헤드라인을 보류하면 이 판정을 되풀이하는 대기 작업(await-onchain.yml)을 직접
부르고, 그 작업이 원본에 올라오는 순간 갱신을 다시 부른다.

    python3 tools/await_onchain.py --site viz/site/index.html

종료코드 — 워크플로가 이것만 보고 움직인다.

    0  지금 갱신하면 헤드라인이 앞으로 간다(원본에 더 새로운 완전한 날이 있다)
    1  아직이다 — 원본에도 그 날 지표가 다 없다
    3  기다릴 것이 없다 — 보류된 날이 없다(헤드라인 = 자료 마지막 날)
    2  판정 불가 — 페이지를 못 읽었거나 원본에 못 물었다
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import site_asof  # noqa: E402

READY, WAIT, UNKNOWN, NOTHING_HELD = 0, 1, 2, 3


def decide(current: Optional[str], latest: Optional[str],
           source_complete: Optional[str]) -> int:
    """세 날짜(ISO 문자열)로 판정한다. 같은 형식이라 문자열 비교가 곧 날짜 비교다.

    '보류된 날이 있나'를 먼저 본다. 없으면 원본이 무엇을 말하든 기다릴 것이
    없다 — 다음 날 가격은 정기 갱신과 감시자의 몫이고, 이 작업까지 그걸 쫓으면
    하루 종일 돌게 된다.
    """
    if not current or not latest:
        return UNKNOWN
    if current >= latest:
        return NOTHING_HELD
    if not source_complete:
        return UNKNOWN
    # 보류된 날(latest) 전체가 아니라 '헤드라인보다 새로운 완전한 날'이면 충분하다.
    # 그 날까지만 와도 헤드라인이 앞으로 간다.
    return READY if source_complete > current else WAIT


def ask_source() -> Optional[str]:
    import site_stale
    return site_stale.ask_source()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--site", default="viz/site/index.html",
                    help="구운 index.html ('-' 면 표준입력)")
    ap.add_argument("--source-latest", default="ask",
                    help="원본의 완전한 마지막 날. 'ask' 면 원본에 묻는다(기본)")
    args = ap.parse_args(argv)

    try:
        html = sys.stdin.read() if args.site == "-" else \
            Path(args.site).read_text(encoding="utf-8")
        current = site_asof.as_of(html, "current")
        latest = site_asof.as_of(html, "latest")
    except OSError as exc:
        print(f"페이지를 못 읽었습니다: {exc}")
        print("판정: 판정 불가")
        return UNKNOWN
    print(f"페이지  헤드라인 {current or '?'} · 자료 마지막 {latest or '?'}")

    if current and latest and current >= latest:
        print("판정: 기다릴 것 없음 — 보류된 날이 없습니다")
        return NOTHING_HELD

    src = ask_source() if args.source_latest == "ask" else args.source_latest
    print(f"원본    지표 넷이 다 있는 마지막 날 {src or '?'}")
    rc = decide(current, latest, src)
    print("판정: " + {
        READY: "지금 갱신하면 헤드라인이 앞으로 갑니다",
        WAIT: "아직 — 원본에도 그 날 온체인 지표가 없습니다",
        UNKNOWN: "판정 불가",
        NOTHING_HELD: "기다릴 것 없음",
    }[rc])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
