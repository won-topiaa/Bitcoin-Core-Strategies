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

import pytest

import build_viz          # conftest 가 tools/ 를 경로에 넣는다
import site_asof

ROOT = Path(__file__).resolve().parents[1]


def _csv() -> str:
    p = ROOT / "data" / "market.csv"
    if not p.exists():
        pytest.skip("data/market.csv 없음")
    return str(p)


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
