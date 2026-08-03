#!/usr/bin/env python3
"""주식 종가를 받아 data/stocks.csv 로 — 지금은 MSTR(스트래티지) 하나.

    python3 tools/fetch_stock.py            # stooq → 실패 시 야후 폴백, data/stocks.csv 에 병합
    python3 tools/fetch_stock.py --check    # 받지 않고 기존 파일만 무결성 검문 (오프라인)

## 소스 — 주: stooq, 폴백: 야후 v8

  [stooq]  https://stooq.com/q/d/l/?s=mstr.us&i=d
           Date,Open,High,Low,Close,Volume 형식 CSV. **분할 조정 종가**라
           2024-08-07 의 MSTR 10:1 분할이 하루 −90% 로 튀지 않아야 정상이다.
           일일 요청 한도를 넘기면 200 응답에 CSV 아닌 안내문이 온다 —
           파싱이 빈손이 되고 야후 폴백으로 넘어간다.
  [야후]   https://query1.finance.yahoo.com/v8/finance/chart/MSTR?range=max&interval=1d
           JSON. close 는 분할 조정, adjclose 는 배당까지 조정 — adjclose 를
           우선한다(MSTR 는 무배당이라 실질 차이 없음).

## 이 환경에선 못 받는다 — CI 러너에서 돈다

이 에이전트 환경의 egress 는 fred.stlouisfed.org·raw.githubusercontent.com 만
열려 있고 stooq·야후는 프록시가 403 으로 막는다. fetch_macro 와 같은 방식으로
정책 403(PolicyBlocked)을 다른 실패와 구분해 보고하고 **기존 파일은 그대로 둔 채**
종료한다(정책 차단은 종료 0 — 도구 잘못이 아니라 환경의 정상 동작이다).
GitHub Actions 러너는 모든 호스트가 열려 있어 거기서 매일 받는다.
※ 야후 원서버가 봇을 403 으로 막는 경우도 같은 경로로 묶인다 — CI 에서 403 이
나면 프록시가 아니라 원서버 차단이니 UA 를 바꿔 볼 것.

## 무결성 검사

양수·유한이 아닌 값은 버리고 경고한다. 연속 관측 사이 |수익률| > 60% 는 **분할
미조정 의심**으로 경고한다 — 조정가라면 2024-08-07 분할이 안 튄다. 단 이 경고는
'의심'이지 '판정'이 아니다: 닷컴기 실제 급락(2000-03 회계 재작성 사태)이 참으로
걸릴 수 있다. 날짜 순증가·중복 없음은 구조적으로 보장된다(날짜 키 사전 +
merge_to_csv 의 정렬 쓰기).

병합·원자적 쓰기는 fetch_macro 의 것을 그대로 쓴다 — 한 번의 수집 실패가 기존
열을 지우지 못하고, 쓰다 죽어도 파일이 반쯤 망가지지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# 정책 403 구분·열 단위 병합·원자적 쓰기 — 전부 fetch_macro 와 같은 동작이어야
# 하므로 복제하지 않고 가져온다(고칠 곳도 한 곳이 된다).
from fetch_macro import PolicyBlocked, UA, _read_existing, merge_to_csv  # noqa: E402

# 우리 열 → (stooq 심볼, 야후 심볼). 종가만 받는다 — 분석(mstr_study)이 종가만 쓴다.
SYMBOLS = {
    "mstr": ("mstr.us", "MSTR"),
}
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}&i=d"
YAHOO_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "{sym}?range=max&interval=1d")

# 연속 관측 사이 |단순수익률| 이 이걸 넘으면 분할 미조정 의심. MSTR 10:1 분할이
# 미조정이면 하루 −90% 라 확실히 걸리고, 조정가의 큰 변동일(±20~30%)은 안 걸린다.
MAX_DAILY_MOVE = 0.60


def _get(url: str, timeout: int = 40) -> Optional[str]:
    """URL 하나. 정책 차단(403/407)이면 PolicyBlocked, 그 외 실패는 None.

    fetch_macro.fetch_series 와 같은 구분 — 프록시가 직접 403(HTTPError)을 주거나
    CONNECT 터널을 403 으로 거절(URLError, reason 에 "403")하거나 둘 다 잡는다."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        code = getattr(exc, "code", None)
        blocked = code in (403, 407) or "403" in str(getattr(exc, "reason", exc))
        if blocked:
            raise PolicyBlocked(url.split("/")[2]) from exc
        print(f"  ! {url.split('/')[2]}: {code or exc} — 건너뜀", file=sys.stderr)
        return None


def parse_stooq(text: str) -> Optional[dict[date, float]]:
    """stooq CSV(Date,Open,High,Low,Close,Volume) → {날짜: 종가}.

    CSV 가 아닌 본문(요청 한도 초과 안내 등)은 행이 안 잡혀 None 이 되고,
    호출부가 야후 폴백으로 넘어간다."""
    out: dict[date, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        raw = (row.get("Date") or "").strip()
        val = (row.get("Close") or "").strip()
        try:
            out[date.fromisoformat(raw[:10])] = float(val)
        except ValueError:
            continue
    return out or None


def parse_yahoo(text: str) -> Optional[dict[date, float]]:
    """야후 v8 chart JSON → {날짜: 종가}. adjclose 우선, 결측(null)은 건너뛴다."""
    try:
        obj = json.loads(text)
        res = obj["chart"]["result"][0]
        stamps = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    values = adj if adj else closes
    out: dict[date, float] = {}
    for t, v in zip(stamps, values):
        if v is None:
            continue
        try:
            d = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
            out[d] = float(v)
        except (ValueError, OSError, OverflowError):
            continue
    return out or None


def fetch_stooq(sym: str, timeout: int = 40) -> Optional[dict[date, float]]:
    text = _get(STOOQ_URL.format(sym=sym), timeout=timeout)
    return parse_stooq(text) if text else None


def fetch_yahoo(sym: str, timeout: int = 40) -> Optional[dict[date, float]]:
    text = _get(YAHOO_URL.format(sym=sym), timeout=timeout)
    return parse_yahoo(text) if text else None


def check_integrity(series: dict[date, float], col: str = "mstr",
                    max_move: float = MAX_DAILY_MOVE
                    ) -> tuple[dict[date, float], list[str]]:
    """(정제된 시계열, 경고 목록). 양수·유한이 아닌 값은 버리고, 연속 관측 사이
    |수익률| > max_move 는 분할 미조정 의심으로 경고한다(버리지는 않는다 —
    실제 급락일 수도 있으므로 판정은 사람이 한다). 날짜 순증가는 구조가 보장한다."""
    import math
    clean = {d: v for d, v in series.items() if math.isfinite(v) and v > 0}
    warns: list[str] = []
    dropped = len(series) - len(clean)
    if dropped:
        warns.append(f"{col}: 양수·유한이 아닌 값 {dropped}개 제거")
    ds = sorted(clean)
    for a, b in zip(ds, ds[1:]):
        r = clean[b] / clean[a] - 1.0
        if abs(r) > max_move:
            warns.append(
                f"{col}: {a} → {b} 수익률 {r:+.0%} — 분할 미조정 의심 "
                "(MSTR 는 2024-08-07 10:1 분할; stooq 조정가라면 안 튄다)")
    return clean, warns


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="주식 종가(stooq → 야후 폴백)를 data/stocks.csv 로")
    ap.add_argument("--out", default="data/stocks.csv")
    ap.add_argument("--check", action="store_true",
                    help="받지 않고 기존 파일만 무결성 검문 (오프라인)")
    ap.add_argument("--timeout", type=int, default=40)
    args = ap.parse_args(argv)

    if args.check:
        cols = _read_existing(Path(args.out))
        if not cols:
            print(f"검문할 파일이 없습니다: {args.out}")
            return 0
        n_warn = 0
        for col, series in sorted(cols.items()):
            _, warns = check_integrity(series, col)
            for w in warns:
                print("  ! " + w, file=sys.stderr)
            n_warn += len(warns)
            print(f"  {col:<8} {len(series)}행 ({min(series)} ~ {max(series)})"
                  f" — 경고 {len(warns)}건")
        return 1 if n_warn else 0        # CI 게이트로 쓸 수 있게 경고 시 1

    columns: dict[str, dict[date, float]] = {}
    policy: list[str] = []
    for col, (stq, yh) in SYMBOLS.items():
        series = None
        try:
            print(f"받는 중 {col} ← stooq {stq}")
            series = fetch_stooq(stq, timeout=args.timeout)
        except PolicyBlocked as blk:
            policy.append(f"stooq({blk})")
            print(f"  ! stooq 정책 차단(403/407): {blk}", file=sys.stderr)
        if not series:
            try:
                print(f"  폴백 {col} ← yahoo {yh}")
                series = fetch_yahoo(yh, timeout=args.timeout)
            except PolicyBlocked as blk:
                policy.append(f"yahoo({blk})")
                print(f"  ! 야후 정책 차단(403/407): {blk}", file=sys.stderr)
        if not series:
            continue
        clean, warns = check_integrity(series, col)
        for w in warns:
            print("  ! " + w, file=sys.stderr)
        if clean:
            columns[col] = clean
            print(f"  {col:<8} {len(clean)}행 ({min(clean)} ~ {max(clean)})")

    if not columns:
        # 완전 실패 — merge_to_csv 를 부르지 않으므로 기존 파일은 그대로다.
        if policy:
            print("\n  ! 프록시 egress 정책이 소스를 막았다(403): "
                  + ", ".join(policy) + "\n"
                  "    이 환경의 정상 동작이다 — 기존 파일은 그대로 둔다.\n"
                  "    GitHub Actions 러너(모든 호스트 허용)에서 돌리면 받아진다.",
                  file=sys.stderr)
            return 0
        print("받은 시리즈가 없습니다 — 기존 파일은 그대로 둔다.", file=sys.stderr)
        return 1

    merge_to_csv(columns, Path(args.out))
    print(f"\n저장: {args.out}  (열: {', '.join(columns)})")
    print("분석:  python3 tools/mstr_study.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
