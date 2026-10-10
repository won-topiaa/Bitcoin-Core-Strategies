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


def cron_hours(field: str) -> int:
    """cron 의 '시' 필드가 하루에 몇 번 걸리는가. '*'=24, '*/3'=8, '2,14'=2."""
    total = 0
    for part in field.split(","):
        if part == "*":
            total += 24
        elif part.startswith("*/"):
            total += len(range(0, 24, int(part[2:])))
        elif "-" in part:
            lo, hi = (int(x) for x in part.split("-"))
            total += hi - lo + 1
        else:
            total += 1
    return total


def runs_per_day(crons: list[str]) -> int:
    return sum(cron_hours(c.split()[1]) for c in crons)


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
    """부르는 것과 뜨는 것, 뜨는 것과 **성공하는 것**은 다르다.

    5차 사고는 '불렀는데 안 떴다', 2026-10-10 은 '떴는데 실패했다'였다. 예전
    확인은 실행이 생겼는지만 봐서 후자를 통과시켰다. 이제 성공까지 보고, 실패하면
    한 번 다시 돌린다(tools/await_deploy.py, tests/test_await_deploy.py).
    """
    doc = load(path)
    steps = steps_of(doc)
    verify = [s for s in steps if "tools/await_deploy.py" in str(s.get("run", ""))]
    assert verify, (
        f"{path.name}: 배포를 부르기만 하고 성공했는지 확인하지 않습니다. "
        "'불렀는데 안 떴다'와 '떴는데 실패했다'가 둘 다 화면을 멈춘 이유였습니다.")
    run = str(verify[0].get("run", ""))
    assert '--since "${STARTED}"' in run, f"{path.name}: 부른 시각 이후의 배포만 세야 합니다"
    assert "pushed" in str(verify[0].get("if", "")), f"{path.name}: 밀었을 때만 확인해야 합니다"
    call = next(i for i, s in enumerate(steps) if "gh workflow run pages.yml" in str(s.get("run", "")))
    assert "STARTED=" in str(steps[call].get("run", "")), "부르기 직전 시각을 남기지 않습니다"
    assert call < steps.index(verify[0])
    job = next(iter(doc["jobs"].values()))
    import await_deploy
    assert int(job["timeout-minutes"]) * 60 > 480 + 5 * 60, (
        f"{path.name}: 작업 시간 제한이 배포 확인(최대 8분)을 담을 만큼 길지 않습니다")
    assert await_deploy.wait.__kwdefaults__["timeout"] == 480
    assert await_deploy.wait.__kwdefaults__["retries"] == 1


def test_pages_does_not_use_configure_pages():
    """configure-pages(enablement:true)는 상태 조회가 한 번 실패하면 Pages 를 새로
    만들려다 403 으로 죽는다 — 2026-10-10 배포 실패의 원인이다."""
    for s in steps_of(load(PAGES)):
        assert "configure-pages" not in str(s.get("uses", "")), (
            "pages.yml 에 configure-pages 가 돌아왔습니다 — 일시적 조회 실패가 배포 실패가 됩니다")
    uses = [str(s.get("uses", "")) for s in steps_of(load(PAGES))]
    assert any(u.startswith("actions/upload-pages-artifact@") for u in uses)
    assert any(u.startswith("actions/deploy-pages@") for u in uses)


def test_the_data_refresh_runs_more_than_once_a_day():
    """하루 한 번으로는 매일 아침 몇 시간씩 '이틀 전'이 뜬다.

    두 가지가 겹쳐서다. ① GitHub 이 예약을 상습적으로 1~2시간 미룬다(실측
    94·96·110·119분). ② 원본이 일일 종가라 가장 신선해도 '어제'이고, UTC 자정에
    나이가 하루 늘어난다. 그래서 예전에는 한국 시간 09:00~13:20 동안 매일
    '이틀 전'으로 보였다. 주기를 짧게 잡아야 원본이 그날치를 내는 즉시 따라잡고,
    예약 하나가 유실돼도 하루를 잃지 않는다.
    """
    on = load(DATA).get("on") or load(DATA).get(True)
    crons = [c["cron"] for c in (on.get("schedule") or [])]
    assert crons, "refresh-data 에 예약이 없습니다"
    # 하한을 12 로 잡은 근거: 실측 성공률이 5/8 = 62.5% 다. N 번 균등 시도하면
    # 실제 간격은 대략 (24/N)/0.625 시간이 된다. 세 시간마다(N=8)면 4.8시간이라
    # 매일 아침 창이 그대로 남고 — 실제로 남았다 — 12회면 3.2시간, 매시간이면
    # 1.6시간이다. 그래서 8 은 통과시키지 않는다(이미 현장에서 실패한 값이다).
    assert runs_per_day(crons) >= 12, (
        f"갱신이 하루 {runs_per_day(crons)}회뿐입니다({crons}). GitHub 은 예약을 "
        "여덟 번 중 세 번꼴로 **아예 건너뛰고** 나머지도 1~4시간 미룬다(실측: "
        "09-01~03 하루 8회 예정에 5회 실행). 유실을 막을 수 없으니 시도 횟수로 "
        "벌어야 한다 — 세 시간마다는 이미 부족한 것이 확인됐다.")

    # 감시자의 '너무 오래 안 돌았다' 문턱이 그 주기와 앞뒤가 맞아야 한다.
    import site_stale as ss
    assert ss.MAX_REFRESH_GAP_HOURS <= 24


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


def test_the_watchdog_asks_the_source_whether_we_are_behind():
    """나이(사흘)로만 보면 '원본은 새 날짜를 냈는데 우리만 안 실은' 상태를 못 잡는다.

    그 상태가 매일 아침 반복됐다 — 원본에 09-03 이 있고 화면은 09-02 인데
    나이는 2일이라 아무것도 안 울렸다. 원본에 직접 묻는 것이 '최신인가'의
    올바른 정의이고, 그 판정은 갱신이 성공하면 수렴한다.
    """
    text = run_text(load(DOG))
    assert "--source-latest" in text, (
        "감시자가 원본에 묻지 않습니다 — 나이 검사만으로는 원본보다 뒤처진 상태를 "
        "절대 못 잡습니다(사흘이 지나야 울립니다).")


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


# --------------------------------------------------------------------------
# 4. 감시자 자체가 살아 있는가 — 6차 사고(2026-09-04 ~ 10-10)
# --------------------------------------------------------------------------
def _dep_install_index(steps: list[dict]) -> int | None:
    for i, s in enumerate(steps):
        if "pip install -r requirements.txt" in str(s.get("run", "")):
            return i
    return None


def test_the_watchdog_installs_what_its_judgement_imports():
    """원본 질의는 btc_core 를 들이고, btc_core 는 PyYAML 이 없으면 SystemExit 한다.

    감시자에 의존성 설치가 없어 36일 동안 매번 죽었고, 그 종료코드 1 이 '뒤처짐'
    으로 읽혀 무조건 재빌드와 헛알림 108건을 냈다. 설치가 판정보다 **먼저** 와야 한다.
    """
    steps = steps_of(load(DOG))
    dep = _dep_install_index(steps)
    assert dep is not None, "감시자가 requirements.txt 를 설치하지 않습니다 — 원본 질의가 죽습니다"
    judge = next(i for i, s in enumerate(steps) if "tools/site_stale.py" in str(s.get("run", "")))
    assert dep < judge, "의존성 설치가 판정 뒤에 있습니다"


def test_the_watchdog_tells_unjudgeable_from_behind():
    """0=최신 1=뒤처짐 2=판정 불가. 2 를 1 과 섞으면 고장 난 감시가 '뒤처짐'을 외친다."""
    steps = steps_of(load(DOG))
    judge = next(s for s in steps if "tools/site_stale.py" in str(s.get("run", "")))
    run = str(judge.get("run", ""))
    assert "set +e" in run, "종료코드를 읽기 전에 셸이 먼저 죽습니다(set -e)"
    assert re.search(r'"\$RC"\s*-eq\s*2', run), "판정 불가(2)를 따로 다루지 않습니다"
    assert "--summary" in run, "판정 결과(stale/behind_days)를 출력으로 내보내지 않습니다"


@pytest.mark.parametrize("path", [DOG], ids=lambda p: p.name)
def test_freshness_is_judged_on_the_headline_date(path):
    """span 끝(latest)은 실현시총이 늦어 헤드라인만 뒤처진 상태를 '최신'으로 읽는다.

    2026-10-10 에 배포된 페이지는 헤드라인 10-08, 저장소는 10-09 였는데 둘 다
    span 끝이 10-09 라 배포 어긋남 검사가 '같다'고 했다.
    """
    text = run_text(load(path))
    assert "site_asof.py --field current" in text, (
        f"{path.name}: 헤드라인 기준일(current)로 재지 않습니다")
    live = next(s for s in steps_of(load(path)) if "live.html" in str(s.get("run", "")))
    assert "--field current /tmp/live.html" in str(live.get("run", "")), (
        "배포 어긋남 검사가 배포된 페이지의 헤드라인을 보지 않습니다")


def test_the_watchdog_only_pages_a_human_for_an_obvious_failure():
    """정상적인 아침(원본보다 하루 뒤처짐)마다 이슈가 울리면 아무도 안 본다.

    예전 조건 '정기 갱신이 3시간 안에 성공했는데도 뒤처짐'은 실현시총이 아직 안
    나온 매일 아침에 참이다. 이틀 이상 뒤처짐·데이터 사흘 낡음·감시 실패·배포 실패
    로만 부른다.
    """
    steps = steps_of(load(DOG))
    verdict = next(s for s in steps if s.get("id") == "verdict")
    run = str(verdict.get("run", ""))
    assert "10800" not in run and "3 * 3600" not in run, "3시간 휴리스틱이 남아 있습니다"
    assert re.search(r"BEHIND_DAYS.*-ge\s*2", run), "이틀 이상 뒤처짐을 문턱으로 쓰지 않습니다"
    assert "DATA_STALE" in run and "FAILED" in run
    assert "behind_days" in str((verdict.get("env") or {}).get("BEHIND_DAYS", ""))
    assert str(verdict.get("if", "")).startswith("always()"), (
        "판정이 실패한 실행에서는 알림 판정이 건너뛰어집니다")


def test_the_rollback_guard_watches_the_headline_too():
    """자료 마지막 날은 그대로인데 헤드라인만 물러나는 경우도 배포하지 않는다."""
    steps = steps_of(load(DATA))
    guard = [s for s in steps if "후퇴" in str(s.get("run", ""))]
    assert guard, "refresh-data 에 후퇴 방지 단계가 없습니다"
    run = str(guard[0].get("run", ""))
    assert run.count("--field current") >= 2, "헤드라인 기준일을 새것·옛것 둘 다 읽지 않습니다"
    assert re.search(r'"\$NEWC"\s*<\s*"\$OLDC"', run), "헤드라인 후퇴를 비교하지 않습니다"


# --------------------------------------------------------------------------
# 5. 헤드라인 보류 → 원본 대기 → 갱신 (매일 아침 '2일 전'의 원인)
# --------------------------------------------------------------------------
POLL = WF / "await-onchain.yml"


def _poller_dispatch_step(doc: dict) -> dict:
    found = [s for s in steps_of(doc) if "gh workflow run await-onchain.yml" in str(s.get("run", ""))]
    assert found, "refresh-data 가 헤드라인 보류 때 원본 대기 작업을 부르지 않습니다"
    return found[0]


def test_a_held_headline_arms_the_onchain_poller():
    """실현시총이 늦어 헤드라인이 보류되면, 다음 예약(02~05 UTC 엔 거의 안 돈다)을
    기다리지 않고 원본에 올라오는 순간 다시 돈다."""
    step = _poller_dispatch_step(load(DATA))
    run = str(step.get("run", ""))
    assert "--field current" in run, "헤드라인 기준일을 읽지 않습니다"
    assert re.search(r'"\$CUR"\s*<\s*"\$LAT"', run), "보류 여부(헤드라인 < 자료 마지막)를 보지 않습니다"


def test_the_poller_and_the_refresh_cannot_call_each_other_forever():
    """대기 작업이 부른 갱신은 대기 작업을 다시 부르지 않는다.

    보류 원인이 원본 지연이 아니면 원본은 '이미 있다'고 답한다. 표식이 없으면
    갱신 → 대기(즉시 준비됨) → 갱신 → … 이 몇 분마다 돈다.
    """
    data = load(DATA)
    on = data.get("on") or data.get(True)
    inputs = (on.get("workflow_dispatch") or {}).get("inputs") or {}
    assert "from_poller" in inputs, "refresh-data 에 from_poller 입력이 없습니다"
    cond = str(_poller_dispatch_step(data).get("if", ""))
    assert "from_poller != true" in cond, f"대기 작업 호출이 from_poller 로 막혀 있지 않습니다: {cond!r}"
    assert "dry_run != true" in cond

    poll = run_text(load(POLL))
    assert re.search(r"gh workflow run refresh-data\.yml[^\n]*-f from_poller=true", poll), (
        "대기 작업이 갱신을 부를 때 from_poller=true 를 붙이지 않습니다 — 고리가 됩니다")


def test_the_poller_is_dispatched_before_the_deploy_check():
    """배포 확인이 실패하면 뒤 단계가 건너뛰어진다 — 대기는 그 앞에 걸려야 한다."""
    steps = steps_of(load(DATA))
    poll = next(i for i, s in enumerate(steps)
                if "gh workflow run await-onchain.yml" in str(s.get("run", "")))
    verify = next(i for i, s in enumerate(steps) if "tools/await_deploy.py" in str(s.get("run", "")))
    commit = next(i for i, s in enumerate(steps_of(load(DATA))) if s.get("id") == "commit")
    assert commit < poll < verify


def test_the_poller_is_bounded_and_on_demand_only():
    doc = load(POLL)
    on = doc.get("on") or doc.get(True)
    assert set(on) == {"workflow_dispatch"}, f"대기 작업은 불릴 때만 돌아야 합니다: {list(on)}"
    assert (doc.get("permissions") or {}).get("actions") == "write"
    assert doc["concurrency"]["group"] == "await-onchain"
    job = next(iter(doc["jobs"].values()))
    assert 0 < int(job["timeout-minutes"]) <= 360, "GitHub 러너 한도(6시간)를 넘습니다"
    run = run_text(doc)
    assert "DEADLINE" in run and "sleep" in run, "대기 루프에 끝이 없습니다"
    m = re.search(r"\+\s*(\d+)\s*\*\s*60", run)
    assert m and int(m.group(1)) < int(job["timeout-minutes"]), (
        "루프 기한이 작업 시간 제한보다 길어 스스로 끝내기 전에 잘립니다")
    assert int(m.group(1)) >= 300, "원본 지연 최대 실측(5.47h)을 못 덮습니다"
    assert _dep_install_index(steps_of(doc)) is not None, (
        "원본 질의가 btc_core 를 들이는데 의존성을 설치하지 않습니다")
    assert "tools/await_onchain.py" in run
