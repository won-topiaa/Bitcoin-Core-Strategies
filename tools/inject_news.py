#!/usr/bin/env python3
"""구워 둔 news.html 에 새 기사 데이터만 갈아 끼운다 — 전체 재빌드 없이.

    python3 tools/inject_news.py --page viz/site/news.html --news data/news.json

뉴스는 하루 네 번 갱신되는데 그때마다 사이트 전체를 다시 구우면 (1) 러너가
data/market.csv 를 다시 받아야 하고(.gitignore 라 체크아웃에 없다) (2) 여섯 장이
전부 바뀌어 매번 전체 재배포가 된다. 그래서 기사 데이터는 공용 페이로드(__BCS__)와
분리된 마커 사이에 살고, 이 도구가 그 구간만 원자적으로 바꾼다.

빌드(tools/build_viz.py)도 처음 구울 때 같은 헬퍼(replace_region)를 쓴다 — 이스케이프
규칙이 두 곳에 두 벌 있으면 한쪽만 고쳐진다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

MARK = "/*__BCS_NEWSDATA__*/"


def news_js(path: str | Path) -> str:
    """news.json → 페이지에 넣을 JS 리터럴. 없거나 깨졌으면 "null".

    **"</" 를 이스케이프한다.** 기사 제목·요약은 외부 입력이라, 수집기가 태그를
    벗겨도 문자열 "</script>" 가 살아남으면 브라우저가 그 지점에서 스크립트 태그를
    닫아 버린다. JSON 은 "<\\/" 를 "</" 와 같게 읽으므로 의미는 변하지 않는다.
    """
    p = Path(path)
    if not p.exists():
        return "null"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return "null"
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).replace("</", "<\\/")
    except ValueError:          # NaN/inf — 무효 JSON 을 페이지에 싣지 않는다
        return "null"


def replace_region(html: str, js: str) -> Optional[str]:
    """마커 두 개 사이를 js 로 바꾼 HTML. 마커가 없으면 None."""
    a = html.find(MARK)
    b = html.find(MARK, a + len(MARK)) if a >= 0 else -1
    if a < 0 or b < 0:
        return None
    return html[:a + len(MARK)] + js + html[b:]


def inject(page: str | Path, news: str | Path) -> bool:
    p = Path(page)
    out = replace_region(p.read_text(encoding="utf-8"), news_js(news))
    if out is None:
        return False
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, p)          # 원자적 — 배포 도중 반쯤 쓰인 페이지가 없게
    return True


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="news.html 에 기사 데이터 주입")
    ap.add_argument("--page", default="viz/site/news.html")
    ap.add_argument("--news", default="data/news.json")
    args = ap.parse_args(argv)
    if not Path(args.page).exists():
        print(f"페이지가 없습니다: {args.page} — 먼저 tools/build_viz.py 로 구우세요",
              file=sys.stderr)
        return 1
    if not inject(args.page, args.news):
        print("마커(/*__BCS_NEWSDATA__*/)를 찾지 못했습니다 — 페이지 형식이 바뀌었나요?",
              file=sys.stderr)
        return 1
    print(f"주입: {args.page} ← {args.news}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
