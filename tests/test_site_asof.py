"""배포 후퇴 방지 가드의 핵심 — 구운 페이지에서 기준일을 꺼내는 부분.

데이터가 없는 환경에서 사이트를 손으로 구우면 낡은 날짜가 박힌다. 그걸 커밋해
배포하면 화면의 기준일이 뒤로 간다(한 번 실제로 그랬다). 배포 워크플로가 그
후퇴를 문자열 비교로 막는데, 비교의 재료인 '이 페이지는 며칠 자 데이터인가'가
정확해야 그 가드가 성립한다. 여기서 그 추출을 못 박는다.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import json

import pytest

import build_viz          # conftest 가 tools/ 를 경로에 넣는다
import site_asof

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
        "sources_ok": ["A"], "sources_failed": [], "items": items,
    }, ensure_ascii=False), encoding="utf-8")
    return p


def test_extracts_the_last_span_date_which_is_the_data_as_of():
    # build_viz 가 굽는 실제 형식(공백 없음)
    html = 'x<script>window.__BCS__={"base":"2010-07-18","span":["2010-07-18","2026-07-30"],"nDays":5857}</script>y'
    assert site_asof.as_of(html) == "2026-07-30"


def test_tolerates_incidental_whitespace_in_the_payload():
    html = '"span": [ "2013-01-01" , "2026-08-02" ]'
    assert site_asof.as_of(html) == "2026-08-02"


def test_missing_span_yields_none_not_a_crash():
    assert site_asof.as_of("기준일이 없는 페이지") is None
    assert site_asof.as_of("") is None


def test_missing_file_is_none_and_exit_1():
    assert site_asof.as_of_file("/does/not/exist.html") is None
    r = subprocess.run(["python3", str(ROOT / "tools" / "site_asof.py"),
                        "/does/not/exist.html"], capture_output=True, text=True)
    assert r.returncode == 1
    assert r.stdout.strip() == ""


def test_reads_the_asof_of_a_freshly_baked_page():
    """실제로 구운 페이지에서 값이 나와야 워크플로 가드가 동작한다."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = {p.name: p for p in build_viz.build(_csv(), Path(tmp))}
        got = site_asof.as_of_file(paths["index.html"])
        assert got is not None and got.count("-") == 2


def test_cli_reads_from_stdin_for_git_show_piping():
    """워크플로는 `git show HEAD:viz/site/index.html | site_asof.py -` 로 이전
    배포본의 기준일을 얻는다. 표준입력 경로가 살아 있어야 한다."""
    html = '"span":["2010-07-18","2026-08-01"]'
    r = subprocess.run(["python3", str(ROOT / "tools" / "site_asof.py"), "-"],
                       input=html, capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout.strip() == "2026-08-01"


def test_string_comparison_matches_chronological_order():
    """가드는 날짜를 문자열로 비교한다(YYYY-MM-DD 는 사전순=시간순). 그 전제가
    깨지면 후퇴 판정이 조용히 틀린다."""
    assert "2026-07-30" < "2026-08-02"
    assert "2026-08-09" < "2026-08-10"
    assert not ("2026-08-02" < "2026-07-30")


def test_a_poisoned_news_title_cannot_forge_the_asof_date():
    """as_of() 는 정규식으로 첫 "span" 을 찾는다 — 페이지 어딘가에 남이 쓴 텍스트가
    섞여 있고 그 텍스트가 우연히(혹은 악의적으로) `"span":["...","..."]` 모양이면
    가짜 날짜를 집을 위험이 있다. 지금 안전한 이유는 코드가 아니라 **배치**다 —
    뉴스 텍스트는 news.html 에만 살고(마커 격리), 워크플로는 index.html 만 읽는다
    (index.body.html 에는 뉴스 마커 자체가 없다). 이 테스트는 그 배치가 실제
    빌드에서 유지되는지 못 박는다 — 이게 깨지면 이 함수는 더 이상 안전하지 않다."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        poison = '"span":["9999-01-01","9999-12-31"] Bitcoin news'
        news = _write_news(t, [
            {"title": poison, "url": "https://a.example/x", "source": "t",
             "published": "2026-08-02T00:00:00+00:00", "summary": "", "score": 1},
        ])
        paths = {p.name: p for p in build_viz.build(_csv(), t, news_json=str(news))}
        index_html = paths["index.html"].read_text(encoding="utf-8")
        news_html = paths["news.html"].read_text(encoding="utf-8")
        # 전제 확인 — 독소가 실제로 사이트 어딘가(news.html)에는 들어갔어야 이
        # 테스트가 의미가 있다. news_js() 가 제목을 JSON 문자열로 굽느라 큰따옴표를
        # \" 로 이스케이프하므로 원문 그대로는 아니지만, 날짜 조각(9999-01-01)은
        # 이스케이프 대상이 아니라 그대로 살아남는다 — 그걸로 심어졌는지 확인한다.
        assert "9999-01-01" in news_html
        # 핵심 — index.html 은 뉴스 마커가 아예 없어 독소가 새어 들 수 없고,
        # 따라서 진짜 기준일이 나와야 한다(가짜 9999 가 아니라). "9999" 만으로는
        # 안 된다 — _script.html 의 오프스크린 텍스트 측정 트릭이 무관하게
        # x:-9999 를 쓴다. 독소 특유의 날짜 조각으로 좁힌다.
        assert "9999-01-01" not in index_html and "9999-12-31" not in index_html
        got = site_asof.as_of(index_html)
        assert got is not None and got.startswith("20")
