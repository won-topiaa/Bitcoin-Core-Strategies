#!/usr/bin/env bash
# 다른 워크플로를 부른다 — GitHub API 의 **일시 오류(5xx·연결 끊김)** 에 한 번 지지 않게.
#
#     bash tools/gh_dispatch.sh pages.yml
#     bash tools/gh_dispatch.sh refresh-data.yml -f from_poller=true
#
# 왜 — 2026-10-12 13:06 에 뉴스 갱신의 배포 호출이 `HTTP 500: Failed to run workflow
# dispatch` 를 받아 실패로 끝나고 실패 이슈(#4)가 열렸다. 그런데 그 요청은 실제로는
# 받아들여져 31초 뒤 배포가 떴고 성공했다. 일시 오류 하나가 '고장' 알림이 됐다.
#
# 그래서 5xx·네트워크 오류는 쉬었다가 다시 부른다. 4xx(권한 없음·워크플로 꺼짐·
# 잘못된 입력)는 다시 해도 같으므로 바로 실패한다.
#
# ⚠️ 다시 부르면 같은 실행이 두 번 뜰 수 있다(위 사례처럼 500 이어도 받아들여졌을
#    수 있다). 이 저장소에서 부르는 대상 — 배포·데이터 갱신·온체인 대기 — 은 모두
#    두 번 떠도 안전하다: 배포는 같은 tip 을 올리고, 갱신·대기는 concurrency 가 앞의
#    것을 취소한다. 그런 워크플로에만 이 도구를 써야 한다.
#
# 시도 횟수와 간격은 GH_DISPATCH_TRIES(기본 3)·GH_DISPATCH_BACKOFF(기본 10초, 시도마다
# 곱한다)로 바꿀 수 있다 — 테스트가 쓴다(tests/test_gh_dispatch.py).
set -u
if [ $# -lt 1 ]; then
  echo "사용법: $0 <workflow.yml> [gh workflow run 인자...]" >&2
  exit 2
fi
wf="$1"; shift
ref="${GITHUB_REF_NAME:?GITHUB_REF_NAME 이 없습니다}"
tries="${GH_DISPATCH_TRIES:-3}"
backoff="${GH_DISPATCH_BACKOFF:-10}"

for ((i = 1; i <= tries; i++)); do
  if out=$(gh workflow run "$wf" --ref "$ref" "$@" 2>&1); then
    [ -n "$out" ] && echo "$out"
    exit 0
  fi
  echo "$out" >&2
  if ! grep -qiE 'HTTP 5[0-9][0-9]|timed? ?out|connection|EOF|i/o|temporar' <<<"$out"; then
    echo "::error::$wf 호출이 거절됐습니다(일시 오류 아님) — 다시 시도하지 않습니다." >&2
    exit 1
  fi
  if [ "$i" -lt "$tries" ]; then
    echo "$wf 호출이 일시 오류로 실패했습니다 — $((i * backoff))초 뒤 다시 부릅니다 ($i/$tries)." >&2
    sleep $((i * backoff))
  fi
done
echo "::error::$wf 호출이 $tries 번 모두 일시 오류로 실패했습니다." >&2
exit 1
