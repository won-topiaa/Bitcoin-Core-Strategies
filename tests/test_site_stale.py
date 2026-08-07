"""자동 갱신의 **결과**를 보는 감시자 — 판정 경계를 못 박는다.

이 저장소의 자동 갱신은 서로 다른 이유로 세 번 조용히 멈췄다. 원인을 하나씩
막는 것으로는 부족해서, 결과(배포된 사이트가 뒤처졌는가)를 직접 보는 검사를
두었다. 그 판정이 틀리면 감시자 자체가 무의미해진다.
"""

from __future__ import annotations

from datetime import date

import site_stale as ss          # conftest 가 tools/ 를 경로에 넣는다

TODAY = date(2026, 8, 7)


# --------------------------------------------------------------------------
# 1. 데이터가 낡았는가
# --------------------------------------------------------------------------
def test_fresh_data_passes():
    stale, why = ss.data_is_stale("2026-08-06", TODAY)
    assert not stale, why


def test_one_day_lag_is_normal_for_the_community_tier():
    """커뮤니티 티어는 전날 데이터를 채운다 — 하루 늦는 것은 정상이다."""
    assert not ss.data_is_stale("2026-08-06", TODAY)[0]
    assert not ss.data_is_stale("2026-08-05", TODAY)[0]


def test_beyond_the_threshold_is_flagged():
    stale, why = ss.data_is_stale("2026-08-03", TODAY)      # 4일 전
    assert stale and "갱신이 멈췄" in why


def test_the_boundary_is_exactly_max_age():
    """3일까지는 통과, 4일부터 뒤처짐 — 화면의 붉은 경고와 같은 기준이다."""
    assert not ss.data_is_stale("2026-08-04", TODAY)[0]     # 정확히 3일
    assert ss.data_is_stale("2026-08-03", TODAY)[0]         # 4일


def test_a_missing_or_broken_asof_is_treated_as_stale():
    """기준일을 못 읽었다는 것은 빌드가 깨졌다는 뜻이다 — 조용히 넘기면 안 된다."""
    assert ss.data_is_stale(None, TODAY)[0]
    assert ss.data_is_stale("", TODAY)[0]
    assert ss.data_is_stale("어제", TODAY)[0]


def test_a_future_asof_is_stale_not_fresh():
    """미래 날짜는 '아주 신선함'이 아니라 데이터 손상이다. 부호만 보고
    통과시키면 손상된 파일이 영원히 감시를 빠져나간다."""
    stale, why = ss.data_is_stale("2026-09-01", TODAY)
    assert stale and "미래" in why


# --------------------------------------------------------------------------
# 2. 소스가 구운 결과보다 새로운가 — 기준일만으로는 절대 안 잡히는 경우
# --------------------------------------------------------------------------
def test_source_newer_than_site_is_flagged():
    """실제로 있었던 사고 — 관련성 지도를 고친 커밋 4개가 재빌드되지 않아
    저장소는 맞는데 화면은 옛 값을 계속 보여 줬다. 그때 기준일은 멀쩡했다."""
    stale, why = ss.source_is_newer(1_700_000_600, 1_700_000_000)
    assert stale and "반영되지 않" in why


def test_site_newer_than_source_is_normal():
    assert not ss.source_is_newer(1_700_000_000, 1_700_000_600)[0]


def test_equal_timestamps_are_not_stale():
    """같은 커밋에서 소스와 사이트가 함께 바뀌는 것이 정상 경로다."""
    assert not ss.source_is_newer(1_700_000_000, 1_700_000_000)[0]


def test_missing_timestamps_do_not_raise_a_false_alarm():
    """git 을 못 읽는 환경에서 '뒤처짐'이라고 우기면 감시자를 아무도 안 믿게 된다."""
    assert not ss.source_is_newer(None, 1_700_000_000)[0]
    assert not ss.source_is_newer(1_700_000_000, None)[0]
    assert not ss.source_is_newer(None, None)[0]


# --------------------------------------------------------------------------
# 3. CLI — 워크플로가 종료코드로 판단한다
# --------------------------------------------------------------------------
def test_cli_exit_code_signals_staleness():
    assert ss.main(["--asof", "2026-08-06", "--today", "2026-08-07"]) in (0, 1)
    # 명백히 낡은 경우는 반드시 1 이어야 한다(소스 비교와 무관하게)
    assert ss.main(["--asof", "2026-01-01", "--today", "2026-08-07"]) == 1
