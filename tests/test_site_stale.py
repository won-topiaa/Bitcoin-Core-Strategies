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
# 2. 구운 소스와 지금 소스가 어긋났는가 — 기준일만으로는 절대 안 잡히는 경우
# --------------------------------------------------------------------------
A = "a" * 40
B = "b" * 40


def test_source_changed_is_flagged():
    """실제로 있었던 사고 — 관련성 지도를 고친 커밋 4개가 재빌드되지 않아
    저장소는 맞는데 화면은 옛 값을 계속 보여 줬다. 그때 기준일은 멀쩡했다."""
    stale, why = ss.source_changed(A, B)
    assert stale and "반영되지 않" in why


def test_same_source_is_normal():
    assert not ss.source_changed(A, A)[0]


def test_missing_stamp_does_not_raise_a_false_alarm():
    """git 을 못 읽거나 표식 없는 옛 빌드에서 '뒤처짐'이라고 우기면, 감시자가
    3시간마다 재빌드를 부르고도 상태가 안 변하는 무한 루프가 된다."""
    assert not ss.source_changed(None, A)[0]
    assert not ss.source_changed(A, None)[0]
    assert not ss.source_changed(None, None)[0]


def test_a_news_only_commit_cannot_mask_an_unbuilt_source_change():
    """예전 판정(커밋 시각 비교)이 무너진 지점을 그대로 재현한다.

    뉴스 갱신은 소스를 다시 굽지 않으면서 viz/site 만 건드린다. 시각으로 재면
    '사이트가 더 최신'이 되어 검사가 통째로 무력해졌다. SHA 로 재면 뉴스 커밋이
    표식을 바꾸지 못하므로 어긋남이 그대로 남는다.
    """
    stamped = A                      # 사이트는 옛 소스로 구워졌고
    current = B                      # 그 뒤 소스가 바뀌었으며
    # 뉴스 커밋이 아무리 여러 번 viz/site 를 건드려도 표식은 그대로다.
    assert ss.source_changed(stamped, current)[0]


def test_the_stamp_round_trips():
    """굽는 쪽과 읽는 쪽이 같은 함수를 쓴다 — 형식이 갈라지면 감시가 눈을 감는다."""
    page = "<html>...</html>\n" + ss.stamp_line(A) + "\n"
    assert ss.read_stamp(page) == A


def test_an_unknown_stamp_reads_as_no_stamp():
    """git 을 못 읽고 구운 페이지는 '모른다'로 남는다 — 어긋남으로 세지 않는다."""
    assert ss.read_stamp(ss.stamp_line(None)) is None
    assert not ss.source_changed(ss.read_stamp(ss.stamp_line(None)), A)[0]


def test_the_last_stamp_wins():
    """표식은 굽기의 맨 마지막에 붙는다. 본문에 같은 모양이 섞여 들어와도
    (뉴스 제목은 남이 쓴 글이다) 뒤에 오는 진짜가 이겨야 한다."""
    page = ss.stamp_line(B) + "\n<p>기사 제목</p>\n" + ss.stamp_line(A) + "\n"
    assert ss.read_stamp(page) == A


def test_no_stamp_at_all_is_none():
    assert ss.read_stamp("<html></html>") is None
    assert ss.read_stamp("") is None


# --------------------------------------------------------------------------
# 3. 정기 갱신이 아예 안 돌고 있는가 — 데이터 나이가 사흘이 되기 전에 잡는다
# --------------------------------------------------------------------------
NOW = 1_800_000_000


def test_a_recent_successful_refresh_is_normal():
    assert not ss.refresh_is_overdue(NOW - 3600, NOW)[0]


def test_a_missed_daily_run_is_flagged_the_same_day():
    """하루 한 번 도는 갱신이 한 번 걸러지면 26시간이 넘는다. 그때 잡아야
    데이터가 사흘 낡기를 기다리지 않는다."""
    stale, why = ss.refresh_is_overdue(NOW - 27 * 3600, NOW)
    assert stale and "27.0시간" in why


def test_the_refresh_boundary_is_max_hours():
    assert not ss.refresh_is_overdue(NOW - 26 * 3600, NOW)[0]        # 정확히 26시간
    assert ss.refresh_is_overdue(NOW - 26 * 3600 - 60, NOW)[0]


def test_no_successful_run_at_all_is_stale():
    """한 번도 성공한 적이 없다는 것은 가장 나쁜 상태다 — 조용히 넘기면 안 된다."""
    assert ss.refresh_is_overdue(None, NOW)[0]


def test_a_future_timestamp_does_not_cry_wolf():
    """러너 시계가 어긋나 미래로 기록되는 일이 있다. 그걸로 알람을 울리지 않는다."""
    assert not ss.refresh_is_overdue(NOW + 3600, NOW)[0]


# --------------------------------------------------------------------------
# 4. CLI — 워크플로가 종료코드로 판단한다
# --------------------------------------------------------------------------
def test_cli_exit_code_signals_staleness():
    assert ss.main(["--asof", "2026-08-06", "--today", "2026-08-07"]) in (0, 1)
    # 명백히 낡은 경우는 반드시 1 이어야 한다(소스 비교와 무관하게)
    assert ss.main(["--asof", "2026-01-01", "--today", "2026-08-07"]) == 1


def test_cli_flags_an_overdue_refresh_even_when_the_data_looks_fine():
    """기준일이 멀쩡해도 정기 실행이 멈춰 있으면 뒤처짐이다."""
    assert ss.main(["--asof", "2026-08-06", "--today", "2026-08-07",
                    "--last-refresh", str(NOW - 48 * 3600), "--now", str(NOW),
                    "--site", "does-not-exist.html"]) == 1


def test_cli_skips_the_run_check_when_no_history_is_given():
    """이력을 못 읽었다고 뒤처짐이라고 우기지 않는다."""
    assert ss.main(["--asof", "2026-08-06", "--today", "2026-08-07",
                    "--site", "does-not-exist.html"]) == 0
