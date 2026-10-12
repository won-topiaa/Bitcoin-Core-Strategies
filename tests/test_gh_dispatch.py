"""다른 워크플로 호출의 재시도(tools/gh_dispatch.sh).

2026-10-12 13:06 — 뉴스 갱신의 배포 호출이 HTTP 500 을 받아 실패로 끝나고 실패
이슈(#4)가 열렸다. 그 요청은 실제로는 받아들여져 31초 뒤 배포가 떴다.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "gh_dispatch.sh"
WF = ROOT / ".github" / "workflows"


def fake_gh(tmp_path: Path, replies: list[tuple[int, str]]) -> Path:
    """호출마다 replies 를 차례로 내는 가짜 gh. 부른 인자는 calls.txt 에 남긴다."""
    state = tmp_path / "n.txt"
    state.write_text("0")
    lines = ["#!/usr/bin/env bash", f'n=$(cat "{state}")', f'echo "$@" >> "{tmp_path}/calls.txt"',
             f'echo $((n + 1)) > "{state}"', 'case "$n" in']
    for i, (rc, msg) in enumerate(replies):
        lines.append(f'  {i}) echo "{msg}"; exit {rc} ;;')
    lines += [f'  *) echo "{replies[-1][1]}"; exit {replies[-1][0]} ;;', "esac"]
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text("\n".join(lines) + "\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return gh.parent


def run(tmp_path, replies, *args):
    bindir = fake_gh(tmp_path, replies)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", GITHUB_REF_NAME="main",
               GH_DISPATCH_BACKOFF="0")
    r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
    calls = (tmp_path / "calls.txt").read_text().splitlines() if (tmp_path / "calls.txt").exists() else []
    return r, calls


def test_a_transient_500_is_retried(tmp_path):
    r, calls = run(tmp_path, [(1, "could not create workflow dispatch event: HTTP 500: Failed to run workflow dispatch"),
                              (0, "https://github.com/x/y/actions/runs/1")], "pages.yml")
    assert r.returncode == 0, r.stderr
    assert calls == ["workflow run pages.yml --ref main"] * 2


def test_a_4xx_is_not_retried(tmp_path):
    """권한 없음·워크플로 꺼짐은 다시 해도 같다 — 바로 실패해야 알림이 빨리 뜬다."""
    r, calls = run(tmp_path, [(1, "HTTP 422: Workflow does not have 'workflow_dispatch' trigger")], "pages.yml")
    assert r.returncode == 1
    assert len(calls) == 1


def test_three_transient_failures_fail(tmp_path):
    r, calls = run(tmp_path, [(1, "HTTP 502: Bad Gateway")], "pages.yml")
    assert r.returncode == 1
    assert len(calls) == 3


def test_extra_arguments_are_passed_through(tmp_path):
    r, calls = run(tmp_path, [(0, "ok")], "refresh-data.yml", "-f", "from_poller=true")
    assert r.returncode == 0
    assert calls == ["workflow run refresh-data.yml --ref main -f from_poller=true"]


def test_every_workflow_dispatch_goes_through_the_helper():
    """한 군데라도 맨 `gh workflow run` 이 남으면 그 자리는 일시 오류 하나에 진다."""
    bare = []
    for p in sorted(WF.glob("*.yml")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "gh workflow run" in line and not line.lstrip().startswith("#"):
                bare.append(f"{p.name}:{i}")
    assert not bare, f"재시도 없이 부르는 곳: {bare}"


@pytest.mark.parametrize("name", ["refresh-data.yml", "refresh-news.yml"])
def test_a_failed_deploy_call_does_not_skip_the_deploy_check(name):
    """500 을 받았어도 실제로 배포가 떴을 수 있다 — 판정은 배포 확인 단계가 한다."""
    import yaml
    doc = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    steps = [s for j in doc["jobs"].values() for s in j["steps"]]
    call = next(s for s in steps if "gh_dispatch.sh pages.yml" in str(s.get("run", "")))
    assert "::warning::" in str(call["run"]) and "||" in str(call["run"]), (
        f"{name}: 배포 호출 실패가 배포 확인을 건너뛰게 만듭니다")
