"""배포 성공 확인(tools/await_deploy.py)의 경계.

2026-10-10 — 배포 실행은 떴는데 configure-pages 가 실패해 화면이 하루 전
헤드라인에 남았다. 예전 확인은 '실행이 생겼나'만 봐서 그걸 통과시켰다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import await_deploy as ad  # noqa: E402

SINCE = "2026-10-10T07:00:00Z"


def run_row(i, status="completed", conclusion="success", t="2026-10-10T07:00:05Z"):
    return {"databaseId": i, "status": status, "conclusion": conclusion, "createdAt": t}


class Fake:
    """gh 대역. ``frames`` 는 'run list' 를 부를 때마다 차례로 내줄 목록이다."""

    def __init__(self, frames, fail_rerun=False):
        self.frames = list(frames)
        self.calls: list[list[str]] = []
        self.fail_rerun = fail_rerun
        self.now = 0.0

    def run(self, args):
        self.calls.append(args)
        if args[:2] == ["run", "rerun"]:
            if self.fail_rerun:
                raise subprocess.CalledProcessError(1, "gh")
            return ""
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if isinstance(frame, Exception):
            raise frame
        return json.dumps(frame)

    def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now

    @property
    def reruns(self):
        return [c for c in self.calls if c[:2] == ["run", "rerun"]]


def go(fake, **kw):
    return ad.wait(SINCE, run=fake.run, sleep=fake.sleep, clock=fake.clock, **kw)


def test_judge():
    assert ad.judge([]) == "absent"
    assert ad.judge([run_row(1)]) == "success"
    assert ad.judge([run_row(1, conclusion="failure")]) == "failed"
    assert ad.judge([run_row(1, status="in_progress", conclusion=None)]) == "pending"
    # 우리 것이 밀려 취소돼도 뒤의 것이 성공하면 성공이다
    assert ad.judge([run_row(1, conclusion="cancelled"), run_row(2)]) == "success"


def test_runs_before_the_call_do_not_count():
    """부르기 전에 성공한 배포는 우리 커밋을 안 올렸다."""
    old = run_row(9, t="2026-10-10T06:59:59Z")
    fake = Fake([[old]])
    assert go(fake) == 1
    assert not fake.reruns


def test_waits_through_a_queue_and_succeeds():
    fake = Fake([[], [run_row(1, "queued", None)], [run_row(1, "in_progress", None)], [run_row(1)]])
    assert go(fake) == 0
    assert not fake.reruns


def test_a_failed_deploy_is_retried_once_and_can_recover():
    """2026-10-10 그대로 — 일시적 실패, 재실행 성공."""
    fail = [run_row(1, conclusion="failure")]
    fake = Fake([fail, [run_row(1, "queued", None)], [run_row(1)]])
    assert go(fake) == 0
    assert fake.reruns == [["run", "rerun", "1"]]


def test_a_stale_conclusion_right_after_the_rerun_is_not_read_as_a_second_failure():
    """재실행 요청 직후 목록은 잠깐 옛 결론을 보여 준다 — 그걸로 포기하면 안 된다."""
    fail = [run_row(1, conclusion="failure")]
    fake = Fake([fail, fail, fail, [run_row(1, "in_progress", None)], [run_row(1)]])
    assert go(fake, interval=15) == 0
    assert len(fake.reruns) == 1


def test_failing_twice_fails_the_step():
    fail = [run_row(1, conclusion="failure")]
    fake = Fake([fail, [run_row(1, "in_progress", None)], fail])
    assert go(fake) == 1
    assert len(fake.reruns) == 1, "재시도는 한 번뿐이어야 합니다"
    # 재실행이 돈 것이 보인 뒤의 실패는 바로 믿는다 — 유예 시간을 다 채우지 않는다
    assert fake.now < ad.RERUN_GRACE, f"{fake.now}초 — 이미 확인된 실패를 붙들고 있습니다"


def test_a_deploy_that_never_appears_fails_the_step(capsys):
    fake = Fake([[]])
    assert go(fake) == 1
    assert "뜨지 않았습니다" in capsys.readouterr().out
    assert fake.now <= ad.APPEAR_SECONDS + 30


def test_a_deploy_that_never_finishes_fails_the_step():
    fake = Fake([[run_row(1, "queued", None)]])
    assert go(fake, timeout=120) == 1


def test_a_listing_hiccup_is_not_a_failure():
    fake = Fake([subprocess.CalledProcessError(1, "gh"), ValueError("bad json"), [run_row(1)]])
    assert go(fake) == 0


def test_a_rerun_that_cannot_be_requested_fails_loudly():
    fake = Fake([[run_row(1, conclusion="failure")]], fail_rerun=True)
    assert go(fake) == 1


def test_superseded_and_cancelled_then_newer_succeeds():
    """concurrency 가 대기 중인 우리 실행을 더 새 것으로 갈아 끼운 경우."""
    frames = [[run_row(1, conclusion="cancelled"), run_row(2, "in_progress", None, "2026-10-10T07:00:09Z")],
              [run_row(1, conclusion="cancelled"), run_row(2, t="2026-10-10T07:00:09Z")]]
    fake = Fake(frames)
    assert go(fake) == 0
    assert not fake.reruns


def test_our_deploy_succeeded_even_if_a_later_one_failed():
    """우리 커밋을 올린 배포가 성공했으면, 그 뒤의 다른 배포가 실패해도 우리 일은 됐다."""
    frames = [[run_row(1), run_row(2, conclusion="failure", t="2026-10-10T07:00:30Z")]]
    fake = Fake(frames)
    assert go(fake) == 0
    assert not fake.reruns


# --------------------------------------------------------------------------
# 2026-10 검토에서 확인된 경계
# --------------------------------------------------------------------------
def test_a_run_created_in_the_same_second_counts():
    """since 를 찍은 그 초 안에 실행이 생기면 createdAt == since 다."""
    fake = Fake([[run_row(1, t=SINCE)]])
    assert go(fake) == 0


def test_a_skipped_workflow_run_deploy_is_not_a_failure():
    """앞 갱신이 실패·취소돼 job 을 건너뛴 workflow_run 실행은 배포를 시도한 적이 없다.
    그걸 실패로 세면 단 한 번의 재시도를 거기 쓴다."""
    skipped = run_row(9, conclusion="skipped", t="2026-10-10T07:00:20Z")
    frames = [[run_row(1, "in_progress", None), skipped], [run_row(1), skipped]]
    fake = Fake(frames)
    assert go(fake) == 0
    assert not fake.reruns


def test_the_retry_goes_to_the_run_that_actually_failed():
    """마지막 행이 취소된 실행(대기 중 밀림)이면 그게 아니라 실패한 실행을 다시 돌린다."""
    rows = [run_row(1, conclusion="failure"), run_row(2, conclusion="cancelled", t="2026-10-10T07:00:30Z")]
    fake = Fake([rows, [run_row(1, "queued", None), rows[1]], [run_row(1), rows[1]]])
    assert go(fake) == 0
    assert fake.reruns == [["run", "rerun", "1"]]


def test_a_rerun_already_started_by_someone_else_is_waited_on():
    """뉴스 갱신과 데이터 갱신이 같은 실패를 동시에 다시 돌리면 둘째 요청은 거절된다."""
    fail = [run_row(1, conclusion="failure")]
    running = [run_row(1, "in_progress", None)]
    fake = Fake([fail, running, [run_row(1)]], fail_rerun=True)
    assert go(fake) == 0


def test_a_newer_skipped_run_does_not_steal_the_retry():
    """실패한 우리 배포 뒤에 건너뛴 workflow_run 실행이 붙으면, 재시도는 우리 것에 가야 한다."""
    rows = [run_row(1, conclusion="failure"), run_row(9, conclusion="skipped", t="2026-10-10T07:00:40Z")]
    fake = Fake([rows, [run_row(1, "queued", None), rows[1]], [run_row(1), rows[1]]])
    assert go(fake) == 0
    assert fake.reruns == [["run", "rerun", "1"]]
