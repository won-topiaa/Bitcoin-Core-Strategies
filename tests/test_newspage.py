"""뉴스 페이지(06) — 외부 텍스트를 굽는 유일한 페이지라 지킬 것이 다르다.

다른 페이지의 숫자는 우리가 계산한 값이지만, 기사 제목·요약·URL 은 **남이 쓴
텍스트**다. 그 텍스트가 HTML 로 실행되면 안 되고(XSS), 스크립트 태그를 끊어도
안 되고("</script>"), 뉴스가 바뀌었다고 다른 다섯 장이 다시 구워져도 안 된다
(그러면 하루 네 번 전체 재배포가 된다).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

import build_viz          # conftest 가 tools/ 를 경로에 넣는다
import inject_news

ROOT = Path(__file__).resolve().parents[1]


def _csv() -> str:
    p = ROOT / "data" / "market.csv"
    if not p.exists():
        pytest.skip("data/market.csv 없음")
    return str(p)


def _write_news(tmp: Path, items: list[dict]) -> Path:
    p = tmp / "news.json"
    p.write_text(json.dumps({
        "fetched": "2026-08-02T15:00:00+00:00",
        "sources_ok": ["A"], "sources_failed": [],
        "items": items,
    }, ensure_ascii=False), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# 이스케이프 — 실행되면 안 되는 것들
# --------------------------------------------------------------------------
def test_a_script_closing_tag_in_a_title_cannot_break_out():
    """제목의 "</script>" 가 그대로 구워지면 브라우저가 거기서 스크립트를 닫고,
    이어지는 기사 텍스트가 **HTML 로 실행된다.**

    메커니즘이 아니라 **성질**을 단언한다 — 출력에 "<" 가 아예 없으면 파서가
    태그·주석을 시작할 방법이 없다. (처음엔 "</"→"<\\/" 치환이었는데 적대적
    검증이 "<!--<script" 로 뚫어 \\u003c 전면 이스케이프로 바꿨다.)"""
    with tempfile.TemporaryDirectory() as tmp:
        news = _write_news(Path(tmp), [
            {"title": "</script><img src=x onerror=alert(1)>", "url": "https://a.example/",
             "source": "t", "published": "2026-08-02T00:00:00+00:00", "summary": "", "score": 1},
        ])
        js = inject_news.news_js(news)
        assert "<" not in js and ">" not in js, "꺾쇠가 살아서 나갔습니다"
        # 원래 문자열로 복원되는지 — 이스케이프가 데이터를 바꾸면 그것대로 문제다
        assert "</script>" in json.loads(js)["items"][0]["title"]


def test_nan_in_news_data_degrades_to_null_not_a_broken_page():
    """NaN 은 유효한 JSON 이 아니다 — JSON.parse 가 죽으면 페이지 스크립트 전체가
    백지가 된다. 그런 데이터는 null 로 강등해 '기사 없음' 화면이 뜨게 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "news.json"
        p.write_text('{"items": [{"score": NaN}]}', encoding="utf-8")
        assert inject_news.news_js(p) == "null"


def test_missing_or_corrupt_news_yields_null():
    assert inject_news.news_js("/does/not/exist.json") == "null"
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "news.json"
        p.write_text("{깨진 json", encoding="utf-8")
        assert inject_news.news_js(p) == "null"


# --------------------------------------------------------------------------
# 주입 — 재빌드 없이 그 구간만
# --------------------------------------------------------------------------
def test_replace_region_swaps_only_between_the_markers():
    html = f"앞부분 {inject_news.MARK}이전데이터{inject_news.MARK} 뒷부분"
    out = inject_news.replace_region(html, "새데이터")
    assert out == f"앞부분 {inject_news.MARK}새데이터{inject_news.MARK} 뒷부분"
    assert inject_news.replace_region("마커가 없다", "x") is None


def test_news_text_containing_a_template_token_does_not_abort_the_build():
    """남이 쓴 기사 제목에 우연히 '__NEWS_URL__' 같은 자리표시자 토큰이 들어 있어도
    하루 전체 재빌드가 죽으면 안 된다. 자리표시자 검사는 신뢰되는 골격에 대해
    뉴스 주입 '전에' 돌아야 한다(적대적 검증이 잡은 결함)."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        news = _write_news(t, [
            {"title": "Bitcoin __NEWS_URL__ __PAGE__ __NAV__ token in a headline",
             "url": "https://a.example/x", "source": "t",
             "published": "2026-08-02T00:00:00+00:00", "summary": "__RULES_URL__", "score": 1},
        ])
        paths = {p.name: p for p in build_viz.build(_csv(), t, news_json=str(news))}
        page = paths["news.html"].read_text(encoding="utf-8")
        # 골격의 자리표시자는 전부 치환됐고(빌드가 안 죽었고), 기사 텍스트는 살아 있다
        assert "주입된" not in page  # sanity
        assert "token in a headline" in page
        # 다른 다섯 장도 정상 생성됐다
        assert len(paths) == len(build_viz.PAGES)


def test_the_baked_news_page_keeps_its_markers_for_later_injection():
    """마커가 빌드 결과에 남아야 refresh-news.yml 이 재빌드 없이 주입할 수 있다.
    마커를 지우면 뉴스 갱신 경로 전체가 조용히 죽는다."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = {p.name: p for p in build_viz.build(_csv(), Path(tmp))}
        news_html = paths["news.html"].read_text(encoding="utf-8")
        assert news_html.count(inject_news.MARK) == 2
        # 다른 장에는 마커가 없어야 한다 — 있으면 주입이 여섯 장을 다 바꾼다
        for name, p in paths.items():
            if name != "news.html":
                assert inject_news.MARK not in p.read_text(encoding="utf-8"), name


def test_news_updates_do_not_touch_the_other_five_pages():
    """뉴스가 바뀌면 news.html 만 바뀌어야 한다. 공용 페이로드에 실리면 여섯 장이
    전부 바뀌어 하루 네 번 전체 재배포가 된다 — 이 분리가 설계의 핵심이다."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        news = _write_news(t, [
            {"title": "기사 하나", "url": "https://a.example/1",
             "source": "t", "published": "2026-08-02T00:00:00+00:00", "summary": "s", "score": 1},
        ])
        a = {p.name: p.read_bytes()
             for p in build_viz.build(_csv(), t / "a", news_json=str(news))}
        news2 = _write_news(t, [
            {"title": "다른 기사", "url": "https://a.example/2",
             "source": "t", "published": "2026-08-02T01:00:00+00:00", "summary": "s2", "score": 2},
        ])
        b = {p.name: p.read_bytes()
             for p in build_viz.build(_csv(), t / "b", news_json=str(news2))}
        assert a["news.html"] != b["news.html"], "뉴스가 바뀌었는데 페이지가 그대로입니다"
        same = [n for n in a if n != "news.html" and a[n] == b[n]]
        diff = [n for n in a if n != "news.html" and a[n] != b[n]]
        assert not diff, f"뉴스만 바꿨는데 같이 바뀐 장: {diff}"
        assert len(same) == len(build_viz.PAGES) - 1


def test_inject_cli_round_trips_on_a_baked_page():
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        paths = {p.name: p for p in build_viz.build(_csv(), t)}
        page = paths["news.html"]
        news = _write_news(t, [
            {"title": "주입된 기사", "url": "https://a.example/x",
             "source": "t", "published": "2026-08-02T00:00:00+00:00", "summary": "", "score": 1},
        ])
        r = subprocess.run(["python3", str(ROOT / "tools" / "inject_news.py"),
                            "--page", str(page), "--news", str(news)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "주입된 기사" in page.read_text(encoding="utf-8")
        # 다시 null 로 — 없는 파일을 주면 '기사 없음' 상태로 돌아간다
        r = subprocess.run(["python3", str(ROOT / "tools" / "inject_news.py"),
                            "--page", str(page), "--news", str(t / "없는파일.json")],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "주입된 기사" not in page.read_text(encoding="utf-8")


def test_a_comment_open_plus_script_cannot_swallow_the_page():
    """"</script>" 탈출만 막았더니 적대적 검증이 "<!--<script" 로 뚫었다 —
    닫는 태그 없이도 HTML 파서를 script-data-double-escaped 상태로 밀어 넣어
    이 스크립트가 페이지 나머지를 통째로 삼킨다(전면 다운). "<" 자체를
    \\u003c 로 바꾸면 그 계열 전부가 닫힌다."""
    with tempfile.TemporaryDirectory() as tmp:
        news = _write_news(Path(tmp), [
            {"title": "<!--<script/ swallow", "url": "https://a.example/",
             "source": "t", "published": "2026-08-02T00:00:00+00:00", "summary": "", "score": 1},
        ])
        js = inject_news.news_js(news)
        assert "<" not in js and ">" not in js
        assert json.loads(js)["items"][0]["title"] == "<!--<script/ swallow"


def test_the_injection_marker_cannot_survive_inside_article_text():
    """기사 제목에 마커 문자열이 통째로 들어 있으면, 다음 주입이 잘못된 구간을
    잘라 페이지가 회복 불가로 망가진다(적대적 검증의 재현). 별표 이스케이프로
    데이터 안에서는 마커가 성립할 수 없고, 혹시 다른 경로로 마커가 늘면
    replace_region 이 손대지 않고 거부한다."""
    with tempfile.TemporaryDirectory() as tmp:
        news = _write_news(Path(tmp), [
            {"title": f"Bitcoin {inject_news.MARK} collision", "url": "https://a.example/",
             "source": "t", "published": "2026-08-02T00:00:00+00:00", "summary": "", "score": 1},
        ])
        js = inject_news.news_js(news)
        assert inject_news.MARK not in js
        assert json.loads(js)["items"][0]["title"] == f"Bitcoin {inject_news.MARK} collision"
        # 그리고 마커가 2개가 아닌 페이지는 거부된다 — 개수 강제
        assert inject_news.replace_region(
            f"{inject_news.MARK}a{inject_news.MARK}b{inject_news.MARK}", "x") is None
        assert inject_news.replace_region(f"{inject_news.MARK}only-one", "x") is None
