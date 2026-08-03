#!/usr/bin/env python3
"""구워 둔 페이지에서 데이터 기준일(as-of) 하나만 꺼낸다.

이 저장소는 데이터가 없는 환경(에이전트 샌드박스)에서도 소스를 고치면 사이트를
다시 굽는다. 그런데 그 환경엔 최신 market.csv 가 없어서, 손으로 구우면 **며칠 전
데이터**가 페이지에 박힌다. 그 낡은 페이지를 커밋해 배포하면 화면의 기준일이
**뒤로 간다** — 사용자에겐 "데이터 갱신이 멈춘" 것으로 보인다(실제로 한 번
그랬다).

그래서 배포 워크플로가 '더 오래된 데이터로 덮지 않기'를 강제하려면, 지금 구운
페이지와 이미 배포된 페이지가 각각 며칠 자 데이터인지 기계가 읽을 수 있어야
한다. 그 값은 페이지에 굽는 JSON 페이로드 안 ``"span":[처음, 마지막]`` 의
마지막 날짜다 — build_viz 가 ``payload["current"]["d"]`` 로 넣는다.

여기서는 그 마지막 날짜만 정규식으로 꺼낸다. 마커 사이 데이터가 아무리 커도
전체 JSON 을 파싱하지 않는다(그리고 파싱하려 해도 다른 뉴스 마커가 섞여 있어
간단치 않다).

    python3 tools/site_asof.py viz/site/index.html   # 파일에서
    ... | python3 tools/site_asof.py -               # 표준입력에서 (git show 용)

기준일을 찾으면 그 날짜(YYYY-MM-DD)를 한 줄 찍고 0, 못 찾으면 아무것도 안 찍고
1 로 끝난다. 워크플로가 그 종료코드와 문자열 비교로 후퇴를 판정한다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

# build_viz 는 separators=(",", ":") 로 굽지만(공백 없음), 혹시 형식이 바뀌어도
# 견디도록 공백을 허용한다. 두 번째 포획이 마지막(=최신) 날짜다.
_SPAN = re.compile(
    r'"span"\s*:\s*\[\s*"(\d{4}-\d{2}-\d{2})"\s*,\s*"(\d{4}-\d{2}-\d{2})"\s*\]'
)


def as_of(html: str) -> Optional[str]:
    """페이지 HTML 문자열에서 데이터 마지막 날짜를 꺼낸다(없으면 None)."""
    m = _SPAN.search(html)
    return m.group(2) if m else None


def as_of_file(path) -> Optional[str]:
    """파일 경로에서 기준일을 꺼낸다. 파일이 없거나 못 읽으면 None."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return as_of(p.read_text(encoding="utf-8"))
    except OSError:
        return None


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    src = argv[0] if argv else "-"
    if src == "-":
        html = sys.stdin.read()
    else:
        p = Path(src)
        if not p.exists():
            return 1
        html = p.read_text(encoding="utf-8")
    d = as_of(html)
    if not d:
        return 1
    print(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
