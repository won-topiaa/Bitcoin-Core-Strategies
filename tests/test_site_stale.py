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
# 2b. 원본보다 뒤처졌는가 — '최신인가'의 올바른 정의
# --------------------------------------------------------------------------
def test_being_behind_the_source_is_flagged_even_when_the_age_looks_fine():
    """실제로 매일 아침 벌어지던 상태를 그대로 재현한다.

    원본에 09-03 이 있는데 화면은 09-02. 나이는 2일이라 나이 검사(사흘)는
    조용하다. 그 사이 GitHub 은 예약 실행을 여덟 번 중 세 번씩 건너뛰고 있었다.
    """
    assert not ss.data_is_stale("2026-09-02", date(2026, 9, 4))[0], (
        "나이 검사만으로는 이 상태가 안 잡힌다는 전제가 깨졌습니다")
    stale, why = ss.behind_source("2026-09-02", "2026-09-03")
    assert stale and "아직 안 실렸습니다" in why


def test_matching_the_source_is_the_definition_of_up_to_date():
    assert not ss.behind_source("2026-09-03", "2026-09-03")[0]


def test_the_source_check_converges():
    """갱신이 성공해 원본과 같아지면 더는 울리지 않아야 한다.

    수렴하지 않는 판정은 감시자를 3시간마다 헛돌게 만든다 — 소스 SHA 를 얕은
    클론에서 잘못 읽던 때가 정확히 그랬다.
    """
    asof = "2026-09-02"
    assert ss.behind_source(asof, "2026-09-03")[0]      # 뒤처짐 → 되살린다
    asof = "2026-09-03"                                  # 갱신 성공
    assert not ss.behind_source(asof, "2026-09-03")[0]   # 조용해진다


def test_we_are_never_called_behind_when_we_are_ahead():
    """원본이 잠깐 뒤로 갈 수 있다(부분 재집계). 그걸로 알람을 울리지 않는다."""
    assert not ss.behind_source("2026-09-03", "2026-09-02")[0]


def test_an_unreachable_source_does_not_raise_a_false_alarm():
    """원본을 못 물어봤다고 뒤처짐이라 우기면 감시자가 무한히 헛돈다."""
    assert not ss.behind_source("2026-09-02", None)[0]
    assert not ss.behind_source(None, "2026-09-03")[0]
    assert not ss.behind_source(None, None)[0]


def test_cli_flags_being_behind_the_source():
    assert ss.main(["--asof", "2026-09-02", "--today", "2026-09-04",
                    "--source-latest", "2026-09-03",
                    "--site", "does-not-exist.html"]) == 1
    assert ss.main(["--asof", "2026-09-03", "--today", "2026-09-04",
                    "--source-latest", "2026-09-03",
                    "--site", "does-not-exist.html"]) == 0


# --------------------------------------------------------------------------
# 3. 정기 갱신이 아예 안 돌고 있는가 — 데이터 나이가 사흘이 되기 전에 잡는다
# --------------------------------------------------------------------------
NOW = 1_800_000_000


def test_a_recent_successful_refresh_is_normal():
    assert not ss.refresh_is_overdue(NOW - 3600, NOW)[0]


def test_a_missed_run_is_flagged_long_before_the_data_looks_old():
    """세 시간마다 도는 갱신이 연속으로 걸러지면 문턱을 넘는다. 그때 잡아야
    데이터가 사흘 낡기를 기다리지 않는다."""
    gap = ss.MAX_REFRESH_GAP_HOURS + 1
    stale, why = ss.refresh_is_overdue(NOW - gap * 3600, NOW)
    assert stale and f"{gap}.0시간" in why


def test_the_refresh_boundary_is_max_hours():
    h = ss.MAX_REFRESH_GAP_HOURS
    assert not ss.refresh_is_overdue(NOW - h * 3600, NOW)[0]         # 정확히 문턱
    assert ss.refresh_is_overdue(NOW - h * 3600 - 60, NOW)[0]


def test_the_gap_threshold_leaves_room_for_github_delaying_the_cron():
    """예약이 1~2시간 밀리는 것은 정상이다. 문턱이 주기에 너무 붙으면 감시자가
    정상 지연마다 되살리기를 부른다 — 예전에 3시간마다 헛돌던 그 모양이다."""
    assert ss.MAX_REFRESH_GAP_HOURS >= 3 * 2, (
        f"주기(3시간) 대비 문턱 {ss.MAX_REFRESH_GAP_HOURS}시간은 너무 빡빡합니다")
    assert ss.MAX_REFRESH_GAP_HOURS <= 24, (
        "문턱이 하루를 넘으면 하루치 유실을 못 잡습니다")


def test_no_successful_run_at_all_is_stale():
    """한 번도 성공한 적이 없다는 것은 가장 나쁜 상태다 — 조용히 넘기면 안 된다."""
    assert ss.refresh_is_overdue(None, NOW)[0]


def test_a_future_timestamp_does_not_cry_wolf():
    """러너 시계가 어긋나 미래로 기록되는 일이 있다. 그걸로 알람을 울리지 않는다.

    **판정(False)만 봐서는 이 가드를 지킬 수 없다** — 가드를 빼도 음수 시간은
    어차피 문턱을 못 넘어 False 가 나온다(돌연변이 검사에서 이 테스트가
    통째로 살아남았다). 그러면 로그에 '마지막 성공한 갱신 -1.0시간 전 — 정상'
    같은 말이 찍힌다. 사람이 읽는 것은 그 줄이므로 메시지까지 못 박는다.
    """
    stale, why = ss.refresh_is_overdue(NOW + 3600, NOW)
    assert not stale
    assert "미래" in why and "판정하지 않습니다" in why, why
    assert "-" not in why, f"음수 시간이 그대로 찍힙니다: {why}"


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
