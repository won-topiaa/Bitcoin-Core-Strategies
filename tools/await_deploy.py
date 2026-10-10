#!/usr/bin/env python3
"""부른 Pages 배포가 **성공으로 끝날 때까지** 지켜보고, 실패하면 한 번 다시 돌린다.

예전 확인은 '배포 실행이 생겼는가'까지만 봤다. 2026-10-10 에 그 실행이 생기긴
했는데 실패로 끝났고(configure-pages 가 Pages 상태 조회에 한 번 실패하자 Pages 를
새로 '만들려다' 403), 확인 단계는 통과했다. 저장소에는 10-09 헤드라인이 들어가
있는데 화면은 10-08 로 남았고, 감시자도 그걸 못 봤다(span 끝만 비교했다).

    python3 tools/await_deploy.py --since 2026-10-10T07:42:00Z

``--since`` 이후에 생긴 pages.yml 실행 중 **하나라도 성공**하면 끝이다. 우리가 부른
실행이 대기 중에 더 새로 불린 실행에 밀려 취소될 수 있는데(concurrency 는 대기
중인 것 중 최신 하나만 남긴다), 그 새 실행은 같은 tip 이나 더 새 tip 을 올리므로
그것의 성공도 우리 배포의 성공이다.

종료코드: 0 성공 · 1 실패(재시도까지 실패, 또는 실행이 안 떴다)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Callable, Optional

FIELDS = "databaseId,status,conclusion,createdAt"
# 부른 뒤 실행이 목록에 나타나기까지 기다리는 시간. dispatch 는 실측 12초 안에 뜬다.
APPEAR_SECONDS = 150
# 재실행을 요청한 뒤 옛 결론을 무시하는 시간.
RERUN_GRACE = 90

Runner = Callable[[list[str]], str]


def gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True,
                          text=True, timeout=60).stdout


def runs_since(since: str, run: Runner) -> list[dict]:
    out = run(["run", "list", "--workflow", "pages.yml", "--limit", "20",
               "--json", FIELDS])
    rows = json.loads(out or "[]")
    # createdAt 과 since 는 둘 다 '...Z' 로 끝나는 초 단위 ISO 라 문자열 비교가 곧 시각
    # 비교다. **>=** 인 이유: since 를 찍은 그 초 안에 실행이 생기면 createdAt == since 다.
    # skipped 는 빼다: workflow_run 으로 왔다가 앞 갱신이 실패·취소돼 job 이 건너뛴
    # 실행이라 배포를 시도한 적이 없다. 그걸 '실패'로 세면 단 한 번의 재시도를 거기
    # 써 버린다(2026-10 검토).
    return sorted((r for r in rows
                   if str(r.get("createdAt", "")) >= since and r.get("conclusion") != "skipped"),
                  key=lambda r: r.get("createdAt", ""))


def judge(rows: list[dict]) -> str:
    """'success' | 'failed' | 'pending' | 'absent'."""
    if not rows:
        return "absent"
    if any(r.get("conclusion") == "success" for r in rows):
        return "success"
    if all(r.get("status") == "completed" for r in rows):
        return "failed"
    return "pending"


def wait(since: str, *, timeout: float = 480, interval: float = 15,
         retries: int = 1, run: Runner = gh,
         sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> int:
    start = clock()
    left = retries
    rerun_at: Optional[float] = None   # 다시 돌린 시각. 그 직후엔 옛 결론이 보일 수 있다
    while True:
        try:
            rows = runs_since(since, run)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            print(f"배포 목록을 못 읽었습니다(다시 봅니다): {exc}")
            rows = None
        if rows is not None:
            state = judge(rows)
            if state == "success":
                ok = next(r for r in rows if r.get("conclusion") == "success")
                print(f"배포 성공 — 실행 {ok.get('databaseId')}")
                return 0
            if state == "pending":
                rerun_at = None        # 다시 돈 것이 보였다 — 이제 결론을 믿어도 된다
            if state == "failed" and rerun_at is not None and clock() - rerun_at < RERUN_GRACE:
                # 재실행을 요청한 직후 목록은 잠깐 옛 결론(failure)을 그대로 보여 준다.
                # 그걸 '재시도도 실패'로 읽으면 재실행이 돌기도 전에 포기한다.
                state = "pending"
            if state == "failed":
                # 다시 돌릴 것은 **실제로 배포를 시도했다가 실패한** 가장 새 실행이다.
                tried = [r for r in rows if r.get("conclusion") not in ("cancelled",)]
                last = (tried or rows)[-1]
                if left > 0:
                    left -= 1
                    print(f"배포 실행 {last.get('databaseId')} 이(가) "
                          f"{last.get('conclusion')} 로 끝났습니다 — 한 번 다시 돌립니다.")
                    try:
                        run(["run", "rerun", str(last.get("databaseId"))])
                    except (subprocess.SubprocessError, OSError) as exc:
                        # 뉴스 갱신과 데이터 갱신이 같은 실패를 동시에 다시 돌리면 둘째는
                        # '이미 도는 중'으로 거절된다. 그건 실패가 아니다 — 다시 보고
                        # 돌고 있으면 기다린다.
                        try:
                            again = judge(runs_since(since, run))
                        except (subprocess.SubprocessError, OSError, ValueError):
                            again = "failed"
                        if again not in ("pending", "success"):
                            print(f"::error::재실행을 못 했습니다: {exc}")
                            return 1
                        print(f"재실행 요청은 거절됐지만 이미 다시 돌고 있습니다({exc}).")
                    rerun_at = clock()
                    sleep(interval)
                    continue
                print(f"::error::배포가 다시 돌려도 실패했습니다(실행 {last.get('databaseId')}, "
                      f"{last.get('conclusion')}). 저장소에는 새 페이지가 커밋됐지만 "
                      "화면은 옛것으로 남습니다.")
                return 1
            if state == "absent" and clock() - start > APPEAR_SECONDS:
                print("::error::배포를 불렀는데 Pages 실행이 뜨지 않았습니다. 저장소에는 "
                      "새 기준일이 커밋됐지만 화면은 옛것으로 남습니다.")
                return 1
        if clock() - start > timeout:
            print(f"::error::배포가 {int(timeout)}초 안에 끝나지 않았습니다.")
            return 1
        sleep(interval)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", required=True, help="배포를 부르기 직전 시각(UTC ISO, ...Z)")
    ap.add_argument("--timeout", type=float, default=480)
    ap.add_argument("--interval", type=float, default=15)
    ap.add_argument("--retries", type=int, default=1)
    a = ap.parse_args(argv)
    return wait(a.since, timeout=a.timeout, interval=a.interval, retries=a.retries)


if __name__ == "__main__":
    sys.exit(main())
