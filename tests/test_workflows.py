"""자동 갱신 사슬의 **배선**을 못 박는다 — 파이썬이 아니라 YAML 이 틀린 자리.

## 왜 이 파일이 있나

이 저장소가 화면을 갱신하는 길은 네 마디짜리 사슬이다.

    데이터 갱신 → (커밋) → Pages 배포 → 화면
                ↑
              감시자 (뒤처지면 되살린다)

지금까지 멈춘 다섯 번 중 **파이썬이 틀려서 멈춘 적은 한 번도 없다.** 매번
마디를 잇는 배선이 문제였고, 배선은 테스트가 없어서 아무도 못 봤다.

    1차  낡은 빌드가 신선한 빌드를 덮었다        — pages.yml 의 push 트리거
    2차  토큰 push 가 배포를 트리거 못 했다       — 이벤트 규칙
    3차  연속 푸시로 실행이 큐에서 전부 취소됐다   — concurrency
    4차  얕은 클론이 소스 SHA 를 tip 으로 찍었다   — checkout 의 fetch-depth
    5차  dispatch 실행이 배포를 트리거 못 했다     — 다시 이벤트 규칙

4차와 5차는 **같은 날 같이** 일어나서, 감시자가 3시간마다 되살리기를 부르고도
화면을 못 고치는 상태가 사흘 갔다. 그래서 배선의 전제를 여기에 적어 둔다.
여기 있는 검사는 전부 '이 줄이 없으면 사슬이 조용히 끊긴다'는 것들이다.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"

DATA = WF / "refresh-data.yml"
NEWS = WF / "refresh-news.yml"
PAGES = WF / "pages.yml"
DOG = WF / "watchdog.yml"

# 사이트를 굽거나(소스 SHA 를 찍는다) 뒤처짐을 판정하는(소스 SHA 를 다시 계산한다)
# 워크플로. 둘은 **같은 값을 계산해야** 하므로 같은 이력 깊이를 봐야 한다.
NEEDS_FULL_HISTORY = ("tools/build_viz.py", "tools/site_stale.py")


def load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def steps_of(doc: dict) -> list[dict]:
    return [s for job in doc["jobs"].values() for s in job.get("steps", [])]


def run_text(doc: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in steps_of(doc))


def all_workflows() -> list[Path]:
    return sorted(WF.glob("*.yml"))


# --------------------------------------------------------------------------
# 1. 얕은 클론 — 4차 사고
# --------------------------------------------------------------------------
def test_source_sha_refuses_to_answer_on_a_shallow_clone():
    """`git log -1 -- <경로>` 는 얕은 클론에서 경로를 못 걸러 낸다.

    이력에 커밋이 하나뿐이면 그 커밋이 모든 파일을 '추가'한 것처럼 보여서, 어떤
    경로를 줘도 tip 이 나온다. 그래서 굽는 쪽(얕음)은 '데이터 갱신' 커밋을 소스
    SHA 로 찍고 감시자(깊음)는 진짜 소스 커밋을 계산해, 둘이 영원히 어긋났다.

    틀린 답 대신 None 을 내야 한다 — 그래야 source_changed 가 판정을 보류하고
    헛알람도 무한 재빌드도 안 생긴다.
    """
    import site_stale as ss

    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "shallow"
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--no-local", str(ROOT), str(dst)],
            capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"얕은 클론을 만들지 못했습니다: {r.stderr[-200:]}")

        import os
        cwd = os.getcwd()
        try:
            os.chdir(dst)
            assert ss.is_shallow() is True, "얕은 클론을 얕다고 못 알아봤습니다"
            assert ss.source_sha() is None, (
                "얕은 클론에서 소스 SHA 를 답했습니다 — 그 값은 tip 이라 틀립니다")
        finally:
            os.chdir(cwd)


def test_source_sha_answers_on_a_full_clone():
    """반대쪽 — 온전한 이력에서는 답해야 한다. 안 그러면 검사가 늘 꺼져 있다."""
    import site_stale as ss
    if ss.is_shallow():
        pytest.skip("이 작업 트리 자체가 얕은 클론입니다")
    sha = ss.source_sha()
    assert sha and re.fullmatch(r"[0-9a-f]{40}", sha), f"이상한 SHA: {sha!r}"


@pytest.mark.parametrize("path", [DATA, DOG], ids=lambda p: p.name)
def test_workflows_that_compute_the_source_sha_fetch_full_history(path):
    """소스 SHA 를 계산하는 워크플로는 전부 fetch-depth: 0 이어야 한다.

    한쪽만 깊게 받으면 두 값이 서로 다른 이력을 보고 계산돼 영원히 안 맞는다.
    """
    doc = load(path)
    if not any(t in run_text(doc) for t in NEEDS_FULL_HISTORY):
        pytest.skip(f"{path.name} 은 소스 SHA 를 계산하지 않습니다")
    checkouts = [s for s in steps_of(doc) if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkouts, f"{path.name}: checkout 단계가 없습니다"
    for s in checkouts:
        depth = (s.get("with") or {}).get("fetch-depth")
        assert str(depth) == "0", (
            f"{path.name}: checkout 에 fetch-depth: 0 이 없습니다(현재 {depth!r}). "
            "얕은 클론이면 소스 SHA 표식이 tip 커밋을 찍어 감시자가 자기 꼬리를 뭅니다.")


# --------------------------------------------------------------------------
# 2. 배포가 이어지는가 — 5차 사고
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", [DATA, NEWS], ids=lambda p: p.name)
def test_workflows_that_commit_must_call_the_deploy_themselves(path):
    """커밋하는 워크플로는 배포를 **직접** 불러야 한다.

    workflow_run 으로만 이어 두면, 감시자가 GITHUB_TOKEN 으로 부른
    workflow_dispatch 실행에서는 배포가 아예 안 뜬다(토큰이 낸 이벤트는 새 실행을
    만들지 못하고, 그 규칙의 예외는 workflow_dispatch/repository_dispatch 뿐이다).
    실측으로 dispatch 성공 16건 중 배포 0건이었다 — 저장소에는 새 기준일이
    들어와 있는데 화면은 옛것으로 남았다.
    """
    doc = load(path)
    text = run_text(doc)
    assert "gh workflow run pages.yml" in text, (
        f"{path.name}: 푸시 뒤 `gh workflow run pages.yml` 호출이 없습니다. "
        "workflow_run 만으로는 dispatch 로 시작한 실행에서 배포가 뜨지 않습니다.")


@pytest.mark.parametrize("path", [DATA, NEWS], ids=lambda p: p.name)
def test_the_deploy_call_only_fires_when_something_was_pushed(path):
    """아무것도 안 밀었는데 배포를 부르면 같은 것을 하루에 여러 번 올린다."""
    doc = load(path)
    dispatch = [s for s in steps_of(doc) if "gh workflow run pages.yml" in str(s.get("run", ""))]
    assert dispatch, f"{path.name}: 배포 호출 단계가 없습니다"
    for s in dispatch:
        cond = str(s.get("if", ""))
        assert "pushed" in cond, (
            f"{path.name}: 배포 호출이 '밀었을 때만' 이라는 조건 없이 걸려 있습니다({cond!r})")
    assert 'pushed=true' in run_text(doc), f"{path.name}: pushed=true 를 내보내는 곳이 없습니다"
    assert 'pushed=false' in run_text(doc), f"{path.name}: pushed=false 를 내보내는 곳이 없습니다"


@pytest.mark.parametrize("path", [DATA, NEWS], ids=lambda p: p.name)
def test_the_deploy_call_is_verified_not_assumed(path):
    """부르는 것과 뜨는 것은 다르다 — 5차 사고가 정확히 '불렀는데 안 떴다'였다.

    화면 내용까지 확인하면 CDN 캐시 때문에 헛알람이 나므로, **배포 실행이
    생겼는지**만 본다. 그건 흔들리지 않는 신호다.
    """
    doc = load(path)
    verify = [s for s in steps_of(doc)
              if "gh run list --workflow pages.yml" in str(s.get("run", ""))]
    assert verify, (
        f"{path.name}: 배포를 부르기만 하고 떴는지 확인하지 않습니다. "
        "'불렀는데 안 떴다'가 바로 화면이 사흘 멈춘 이유였습니다.")
    assert "::error::" in str(verify[0].get("run", "")), (
        f"{path.name}: 배포가 안 떴을 때 실패시키지 않습니다 — 조용히 넘어갑니다")


def test_pages_accepts_a_direct_call():
    """직접 부르려면 pages.yml 이 workflow_dispatch 를 받아야 한다."""
    doc = load(PAGES)
    on = doc.get("on") or doc.get(True)          # YAML 이 on: 을 True 로 읽는 경우
    assert "workflow_dispatch" in on, "pages.yml 이 workflow_dispatch 를 받지 않습니다"


def test_pages_still_keeps_the_workflow_run_belt():
    """직접 호출이 막히는 날을 위한 두 번째 겹은 남겨 둔다."""
    doc = load(PAGES)
    on = doc.get("on") or doc.get(True)
    wr = on.get("workflow_run") or {}
    assert set(wr.get("workflows") or []) == {"데이터 갱신", "뉴스 갱신"}, (
        f"pages.yml 의 workflow_run 감시 대상이 달라졌습니다: {wr.get('workflows')!r}")


def test_a_deploy_in_flight_is_never_cancelled():
    """배포를 중간에 끊으면 어중간한 상태로 남을 수 있다.

    이제 workflow_run 과 직접 호출이 함께 뜰 수 있어 겹칠 일이 늘었다.
    """
    doc = load(PAGES)
    assert doc["concurrency"]["cancel-in-progress"] is False, (
        "pages.yml 이 진행 중인 배포를 취소합니다 — 배포 워크플로에는 false 여야 합니다")


@pytest.mark.parametrize("path", all_workflows(), ids=lambda p: p.name)
def test_calling_another_workflow_needs_actions_write(path):
    """`gh workflow run` 은 actions: write 없이는 조용히 403 으로 죽는다."""
    doc = load(path)
    if "gh workflow run" not in run_text(doc):
        pytest.skip(f"{path.name} 은 다른 워크플로를 부르지 않습니다")
    perms = doc.get("permissions") or {}
    assert perms.get("actions") == "write", (
        f"{path.name}: gh workflow run 을 쓰는데 permissions.actions 가 "
        f"{perms.get('actions')!r} 입니다")


# --------------------------------------------------------------------------
# 3. 사슬 전체가 이어져 있는가
# --------------------------------------------------------------------------
def test_only_the_fresh_data_runner_can_deploy():
    """1차 사고 — 데이터 없는 환경에서 구운 낡은 페이지가 곧바로 배포되던 경로.

    pages.yml 에 push 트리거가 다시 생기면 그 사고가 그대로 돌아온다.
    """
    on = load(PAGES).get("on") or load(PAGES).get(True)
    assert "push" not in on, (
        "pages.yml 에 push 트리거가 생겼습니다 — 낡은 viz/site 가 곧바로 배포됩니다")


def test_the_watchdog_matches_the_remedy_to_the_symptom():
    """저장소는 최신인데 배포만 밀렸을 때 다시 굽는 것은 듣지 않는다.

    결과가 같아 커밋이 안 생기고, 커밋이 없으면 배포도 없다. 그 경우에는
    배포만 다시 불러야 한다.
    """
    steps = steps_of(load(DOG))
    calls = {}
    for s in steps:
        run = str(s.get("run", ""))
        for target in ("refresh-data.yml", "pages.yml"):
            if f"gh workflow run {target}" in run:
                calls[target] = str(s.get("if", ""))
    assert "refresh-data.yml" in calls, "감시자가 데이터 갱신을 부르지 않습니다"
    assert "pages.yml" in calls, (
        "감시자가 '배포만 밀린' 경우에 배포를 직접 부르지 않습니다 — "
        "다시 구워도 커밋이 안 생겨 화면이 안 바뀝니다")
    assert "drift" in calls["pages.yml"], (
        f"배포 재호출이 배포 어긋남 조건에 걸려 있지 않습니다: {calls['pages.yml']!r}")


def test_the_watchdog_can_open_an_issue_and_read_the_deployed_page():
    """되살리기가 듣지 않을 때 사람을 부를 수 있어야 한다."""
    doc = load(DOG)
    perms = doc.get("permissions") or {}
    assert perms.get("issues") == "write", "감시자가 이슈를 열 권한이 없습니다"
    assert perms.get("pages") == "read", "감시자가 배포 주소를 읽을 권한이 없습니다"


def test_the_stale_alarm_is_not_wired_to_step_failure():
    """뒤처짐은 스텝을 실패시키지 않는다 — `if: failure()` 로는 영원히 안 울린다."""
    steps = steps_of(load(DOG))
    notify = [s for s in steps if "이슈" in str(s.get("name", ""))]
    assert notify, "감시자에 알림 단계가 없습니다"
    for s in notify:
        cond = str(s.get("if", ""))
        assert cond and cond != "failure()", (
            f"알림이 `if: {cond}` 로 걸려 있습니다 — 뒤처짐만으로는 절대 안 울립니다")
