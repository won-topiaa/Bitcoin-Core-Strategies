"""온체인 대기 판정(tools/await_onchain.py)의 경계.

이 판정이 틀리면 두 방향으로 망가진다. '준비됨'을 너무 일찍 말하면 갱신과 대기가
서로를 부르는 고리가 되고(워크플로의 from_poller 가 마지막 방어선이다), 너무 늦게
말하면 매일 아침 '2일 전'이 그대로 남는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import await_onchain as ao  # noqa: E402


def page(current: str, first: str, latest: str) -> str:
    return ('<script>const D={"span":["%s","%s"],"current":{"d":"%s","bcs":1}};</script>'
            % (first, latest, current))


def test_nothing_held_means_nothing_to_wait_for():
    """헤드라인 = 자료 마지막 날이면 원본이 다음 날을 냈어도 기다리지 않는다.

    그걸 쫓으면 이 작업이 하루 종일 돈다 — 다음 날 가격은 정기 갱신의 몫이다.
    """
    assert ao.decide("2026-10-09", "2026-10-09", "2026-10-10") == ao.NOTHING_HELD


def test_held_and_source_not_there_yet_means_wait():
    assert ao.decide("2026-10-08", "2026-10-09", "2026-10-08") == ao.WAIT


def test_held_and_source_has_the_day_means_ready():
    assert ao.decide("2026-10-08", "2026-10-09", "2026-10-09") == ao.READY


def test_a_newer_complete_day_than_the_headline_is_enough():
    """보류된 날이 둘이어도(10-08, 10-09) 하나만 와도 헤드라인이 앞으로 간다."""
    assert ao.decide("2026-10-07", "2026-10-09", "2026-10-08") == ao.READY


def test_unknown_inputs_are_unjudgeable_not_ready():
    """못 물었는데 '준비됨'이라 하면 갱신을 헛부르고, '아직'이라 하면 원인을 숨긴다."""
    # 원본에 못 물은 것은 원본 장애다 — 대기 작업의 고장(UNKNOWN)과 따로 센다
    assert ao.decide("2026-10-08", "2026-10-09", None) == ao.SOURCE_DOWN
    assert ao.decide(None, "2026-10-09", "2026-10-09") == ao.UNKNOWN
    assert ao.decide("2026-10-08", None, "2026-10-09") == ao.UNKNOWN


def test_the_exit_codes_are_distinct():
    codes = {ao.READY, ao.WAIT, ao.UNKNOWN, ao.NOTHING_HELD, ao.SOURCE_DOWN}
    assert len(codes) == 5
    assert ao.READY == 0, "워크플로가 0 을 '갱신을 불러라'로 읽는다"
    # 1 은 파이썬이 잡히지 않은 예외로 죽을 때의 코드다. '아직'이 1 이면 죽은 도구가
    # '아직'으로 읽혀 320분을 헛돌고 초록불로 끝난다.
    assert 1 not in codes


def test_cli_reads_both_dates_from_the_page(tmp_path, capsys):
    f = tmp_path / "index.html"
    f.write_text(page("2026-10-08", "2010-07-18", "2026-10-09"), encoding="utf-8")
    assert ao.main(["--site", str(f), "--source-latest", "2026-10-08"]) == ao.WAIT
    assert ao.main(["--site", str(f), "--source-latest", "2026-10-09"]) == ao.READY
    out = capsys.readouterr().out
    assert "헤드라인 2026-10-08" in out and "자료 마지막 2026-10-09" in out


def test_cli_does_not_ask_the_source_when_nothing_is_held(tmp_path, monkeypatch):
    """보류가 없으면 원본에 묻지도 않는다 — 원본이 막힌 날에도 깔끔히 끝난다."""
    f = tmp_path / "index.html"
    f.write_text(page("2026-10-09", "2010-07-18", "2026-10-09"), encoding="utf-8")

    def boom():
        raise AssertionError("묻지 않아야 합니다")
    monkeypatch.setattr(ao, "ask_source", boom)
    assert ao.main(["--site", str(f)]) == ao.NOTHING_HELD


def test_cli_asks_the_source_when_held(tmp_path, monkeypatch):
    f = tmp_path / "index.html"
    f.write_text(page("2026-10-08", "2010-07-18", "2026-10-09"), encoding="utf-8")
    monkeypatch.setattr(ao, "ask_source", lambda: "2026-10-09")
    assert ao.main(["--site", str(f)]) == ao.READY
    monkeypatch.setattr(ao, "ask_source", lambda: None)
    assert ao.main(["--site", str(f)]) == ao.SOURCE_DOWN


def test_cli_a_missing_page_is_unjudgeable(tmp_path):
    assert ao.main(["--site", str(tmp_path / "nope.html"),
                    "--source-latest", "2026-10-09"]) == ao.UNKNOWN


def test_the_real_page_parses():
    """지금 저장소의 페이지에서 두 날짜를 실제로 읽을 수 있어야 한다."""
    import site_asof
    html = (ROOT / "viz" / "site" / "index.html").read_text(encoding="utf-8")
    assert site_asof.as_of(html, "current") and site_asof.as_of(html, "latest")


def test_a_crash_is_not_read_as_wait(tmp_path):
    """도구가 예외로 죽으면 종료코드 1 — 워크플로는 그걸 '판정 불가'로 세야 한다."""
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "await_onchain.py"), "--site", "/dev/null/x"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == ao.UNKNOWN
    import yaml
    doc = yaml.safe_load((ROOT / ".github/workflows/await-onchain.yml").read_text(encoding="utf-8"))
    run = "\n".join(str(s.get("run", "")) for j in doc["jobs"].values() for s in j["steps"])
    assert f"      {ao.WAIT})" in run or f"{ao.WAIT})" in run, "워크플로가 '아직'(WAIT) 코드를 다루지 않습니다"
    assert f"{ao.SOURCE_DOWN})" in run, "워크플로가 원본 장애를 따로 다루지 않습니다"
    assert "\n              1)" not in run, "1 을 '아직'으로 읽습니다 — 죽은 도구가 헛돕니다"
