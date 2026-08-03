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

    **"<" 와 ">" 를 전부 유니코드 이스케이프한다.** 처음엔 "</" 만 바꿨는데,
    적대적 검증이 그걸 뚫었다 — 제목 "<!--<script" 는 닫는 태그 없이도 HTML
    파서를 script-data-double-escaped 상태로 밀어 넣어, 이 스크립트가 뒤따르는
    페이지 전체를 삼키게 만든다(전면 다운). "<" 자체를 \\u003c 로 바꾸면 파서
    관점에서 태그·주석 시작이 아예 존재하지 않아 그 계열 전부가 닫힌다.
    JSON.parse 가 원문으로 복원하므로 데이터 의미는 변하지 않는다.

    같은 이유로 "/*" 와 "*/" 도 이스케이프한다 — 기사 제목에 주입 마커
    문자열(/*__BCS_NEWSDATA__*/)이 통째로 들어 있으면 다음 주입이 잘못된
    구간을 자른다. 별표를 \\u002a 로 바꾸면 마커가 데이터 안에서 성립할 수 없다.
    """
    p = Path(path)
    if not p.exists():
        return "null"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return "null"
    try:
        js = json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                        allow_nan=False)
    except ValueError:          # NaN/inf — 무효 JSON 을 페이지에 싣지 않는다
        return "null"
    return (js.replace("<", "\\u003c").replace(">", "\\u003e")
              .replace("/*", "/\\u002a").replace("*/", "\\u002a/"))


def replace_region(html: str, js: str) -> Optional[str]:
    """마커 두 개 사이를 js 로 바꾼 HTML. **마커가 정확히 2개가 아니면 None.**

    '앞에서 두 개'를 고르면, 어떤 경로로든 마커가 3개가 된 페이지에서 잘못된
    구간을 자르고 — 주입할 때마다 마커가 늘어나 회복이 불가능해진다. 이제
    news_js 가 별표를 이스케이프해 데이터 안엔 마커가 성립할 수 없지만,
    방어는 겹으로: 개수가 안 맞으면 손대지 않고 실패를 알린다."""
    if html.count(MARK) != 2:
        return None
    a = html.find(MARK)
    b = html.find(MARK, a + len(MARK))
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
