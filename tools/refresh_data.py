#!/usr/bin/env python3
"""데이터 자동 갱신 — CoinMetrics 커뮤니티 API 에서 받아 CSV 를 증분으로 잇는다.

    python3 tools/refresh_data.py                  # 최근 400일 받아 병합
    python3 tools/refresh_data.py --dry-run        # 무엇이 바뀌는지만 출력
    python3 tools/refresh_data.py --build          # 병합 후 사이트까지 재생성
    python3 tools/refresh_data.py --check          # 받지 않고 신선도만 확인
    python3 tools/refresh_data.py --full           # 전 구간 다시 받기

이 도구의 목적은 **한 줄 잇는 것**이 아니라 **잘못 잇지 않는 것**이다.
data/market.csv 는 16년치 관측이고 이 시스템의 유일한 입력이다. 자동 갱신을
붙인다는 것은 사람이 안 보는 동안 그 파일을 덮어쓰는 코드를 두겠다는 뜻이라,
그 코드가 조용히 망가지는 경로를 먼저 막아야 한다.

    1. 겹침 구간 대조 — 이미 가진 날짜의 가격이 새로 받은 값과 맞는가.
       다른 자산·다른 단위·엉뚱한 응답은 전부 여기서 걸린다.
    2. 칸 단위 병합 — 이미 있는 값을 **빈 값으로 덮지 않는다.** 커뮤니티 티어는
       최신 하루의 실현시총이 비는데, 시계열 통째로 갈아 끼우면 어제 갖고 있던
       값이 오늘 사라진다.
    3. 원자적 교체 — 임시 파일에 쓰고, 그 파일을 다시 읽어 정상인지 확인한 뒤
       제자리에 옮긴다. 중간에 죽어도 원본은 그대로다.
    4. 되돌리기 — data/market.csv 는 .gitignore 에 있어서 **git 이 백업이 아니다.**
       그래서 교체 직전 파일을 ``market.csv.bak`` 으로 남긴다. 그리고 진짜 원본은
       API 다 — ``--full`` 로 전 구간을 1.7초에 다시 받으면 증분으로 이어 온
       파일과 **바이트 단위로 같은 파일**이 나온다(그 동일성은 테스트로 지킨다).

종료 코드: 0 정상(바뀌었든 안 바뀌었든) · 2 검증 실패(아무것도 안 씀)
· 3 받기 실패 · 1 --check 에서 데이터가 너무 오래됨.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from btc_core.datasources.base import FetchError  # noqa: E402
from btc_core.datasources.csv_source import (  # noqa: E402
    COLUMNS,
    bundle_cells,
    load_csv_bundle,
    read_cells,
    write_cells,
)

DEFAULT_CSV = "data/market.csv"
DEFAULT_DAYS = 400          # export_viz 가 최근 400일을 일 단위로 쓴다
MIN_OVERLAP = 5             # 겹침이 이보다 적으면 대조가 대조가 아니다
PRICE_TOL = 0.02            # 겹침 구간 가격 허용 오차 2%
MAX_DISAGREE = 3            # 허용 오차를 넘는 날이 이보다 많으면 중단
JUMP_ABORT = 1.6            # 하루 |Δln 가격| — 5배는 데이터 오류다
JUMP_WARN = 0.35            # 하루 42% — 실제로 있었던 일이라 경고만
STALE_DAYS = 3              # 이보다 오래되면 "오래됐다"고 말한다


# --------------------------------------------------------------------------
# 검증
# --------------------------------------------------------------------------
class RefuseToWrite(RuntimeError):
    """검증에서 걸렸다. 아무것도 쓰지 않는다."""


def check_fetched(new: dict[date, dict[str, float]], *,
                  today: Optional[date] = None) -> list[str]:
    """새로 받은 것 자체가 온전한가. 문제면 RefuseToWrite."""
    today = today or date.today()
    notes: list[str] = []
    if not new:
        raise RefuseToWrite("받은 데이터가 비어 있습니다.")

    priced = {d: c["price"] for d, c in new.items() if "price" in c}
    if not priced:
        raise RefuseToWrite("받은 데이터에 가격이 하나도 없습니다.")

    bad = [d for d, v in priced.items()
           if not math.isfinite(v) or v <= 0]
    if bad:
        raise RefuseToWrite(
            f"가격이 0 이하이거나 숫자가 아닌 날 {len(bad)}개 (예: {sorted(bad)[:3]})")

    ahead = [d for d in new if d > today + timedelta(days=1)]
    if ahead:
        raise RefuseToWrite(f"미래 날짜가 섞여 있습니다: {sorted(ahead)[:3]}")

    # 하루 사이 급변. 실제 비트코인의 최악 하루가 −49%(Δln −0.67) 라 그 근처를
    # 막으면 진짜 폭락일에 갱신이 멈춘다. 단위 오류(100배·1000배)만 막고
    # 나머지는 경고로 남긴다.
    ds = sorted(priced)
    for a, b in zip(ds, ds[1:]):
        if (b - a).days != 1:
            continue
        r = abs(math.log(priced[b] / priced[a]))
        if r > JUMP_ABORT:
            raise RefuseToWrite(
                f"{a} → {b} 가격이 {priced[a]:,.0f} → {priced[b]:,.0f} 로 "
                f"{math.exp(r):.1f}배 움직였습니다 — 단위가 바뀐 응답으로 봅니다")
        if r > JUMP_WARN:
            notes.append(f"{b} 가격이 하루에 {(math.exp(r) - 1) * 100:.0f}% 움직였습니다")
    return notes


def check_overlap(old: dict[date, dict[str, float]],
                  new: dict[date, dict[str, float]]) -> list[str]:
    """이미 가진 날짜의 가격이 새로 받은 값과 맞는가.

    **이 검사가 이 도구의 핵심이다.** 엉뚱한 자산, 센트 단위, 잘린 응답, 바뀐
    API 스펙 — 조용히 망가지는 경로는 대부분 여기서 걸린다. 잇는 부분만 보면
    새 값이 그럴듯한지 알 수 없지만, 겹치는 부분을 대조하면 알 수 있다.
    """
    have = sum(1 for c in old.values() if "price" in c)
    shared = sorted(d for d in new if d in old
                    and "price" in new[d] and "price" in old[d])
    notes: list[str] = []
    if not old:
        return ["기존 CSV 가 없어 겹침 대조를 건너뜁니다 — 첫 수집으로 봅니다"]
    if len(shared) < MIN_OVERLAP:
        # 기존 파일이 짧아서 겹칠 날이 애초에 없는 경우와, 창을 좁게 잡아
        # 겹침을 못 만든 경우는 다르다. 앞쪽에 "--days 를 늘리세요"라고 하면
        # 늘려도 안 되는 것을 늘리라고 말하는 셈이다.
        if have >= MIN_OVERLAP:
            raise RefuseToWrite(
                f"겹치는 날이 {len(shared)}일뿐입니다 (최소 {MIN_OVERLAP}일). "
                f"기존 파일에는 {have}일이 있으니 --days 를 늘려 겹침을 "
                "확보하세요 — 대조 없이 이어 붙이지 않습니다")
        if not shared:
            raise RefuseToWrite(
                f"기존 {have}일과 새로 받은 것이 날짜에서 하나도 겹치지 않습니다 "
                "— 같은 자산인지 확인하세요")
        notes.append(f"기존 파일이 {have}일뿐이라 겹침 {len(shared)}일로만 대조했습니다")

    off = []
    for d in shared:
        o, n = old[d]["price"], new[d]["price"]
        if o > 0 and abs(n / o - 1) > PRICE_TOL:
            off.append((d, o, n))
    # 겹침이 짧으면 절대 허용치도 줄여야 한다. 겹침 1일에 3일까지 허용하면
    # 그 1일이 20배 달라도 통과한다 — 대조를 한 것이 아니다.
    allow = min(MAX_DISAGREE, len(shared) // 3)
    if len(off) > allow:
        head = ", ".join(f"{d} {o:,.0f}→{n:,.0f}" for d, o, n in off[:3])
        raise RefuseToWrite(
            f"겹침 {len(shared)}일 중 {len(off)}일의 가격이 "
            f"{PRICE_TOL * 100:.0f}% 넘게 다릅니다 (허용 {allow}일, {head}). "
            "다른 자산이나 다른 단위를 받은 것으로 봅니다")

    notes.append(f"겹침 {len(shared)}일 대조 통과")
    if off:
        notes.append(f"그중 {len(off)}일은 값이 갱신됐습니다 "
                     f"({', '.join(str(d) for d, _, _ in off)})")
    return notes


# --------------------------------------------------------------------------
# 병합
# --------------------------------------------------------------------------
class Merge:
    """병합 결과와 무엇이 바뀌었는지."""

    def __init__(self) -> None:
        self.cells: dict[date, dict[str, float]] = {}
        self.new_dates: list[date] = []
        self.filled: dict[str, int] = {}      # 빈 칸을 채운 수
        self.changed: dict[str, int] = {}     # 값이 바뀐 수

    @property
    def touched(self) -> bool:
        return bool(self.new_dates or self.filled or self.changed)

    def summary(self) -> str:
        parts = []
        if self.new_dates:
            parts.append(f"새 날짜 {len(self.new_dates)}일 "
                         f"({self.new_dates[0]} ~ {self.new_dates[-1]})")
        if self.filled:
            parts.append("빈 칸 채움 " + ", ".join(
                f"{k} {v}" for k, v in sorted(self.filled.items())))
        if self.changed:
            parts.append("값 갱신 " + ", ".join(
                f"{k} {v}" for k, v in sorted(self.changed.items())))
        return " · ".join(parts) or "바뀐 것이 없습니다"


def merge(old: dict[date, dict[str, float]],
          new: dict[date, dict[str, float]]) -> Merge:
    """새 값을 얹는다. **이미 있는 값을 빈 값으로 덮지 않는다.**

    커뮤니티 티어는 최신 하루의 실현시총이 비어 있다. 시계열을 통째로 갈아
    끼우면 어제 받아 둔 값이 오늘 사라지고, 그러면 지표 커버리지가 날마다
    들쭉날쭉해진다. 그래서 열마다 "새 값이 있을 때만" 얹는다.
    """
    m = Merge()
    m.cells = {d: dict(c) for d, c in old.items()}
    fresh: set[date] = set()

    for d in sorted(new):
        if d not in m.cells:
            m.cells[d] = {}
            m.new_dates.append(d)
            fresh.add(d)
        row, incoming = m.cells[d], new[d]
        for col in COLUMNS:
            if col not in incoming:
                continue                       # 빈 값으로 덮지 않는다
            before = row.get(col)
            if before is None:
                row[col] = incoming[col]
                if d not in fresh:             # 새 날짜의 칸은 '채움'으로 세지 않는다
                    m.filled[col] = m.filled.get(col, 0) + 1
            elif before != incoming[col]:
                row[col] = incoming[col]
                m.changed[col] = m.changed.get(col, 0) + 1
    return m


def check_merged(old: dict[date, dict[str, float]], m: Merge) -> None:
    """병합 결과가 기존을 잃지 않았는가. 병합 규칙의 사후 확인이다."""
    lost_dates = set(old) - set(m.cells)
    if lost_dates:
        raise RefuseToWrite(f"날짜가 사라졌습니다: {sorted(lost_dates)[:3]}")
    for col in COLUMNS:
        before = sum(1 for c in old.values() if col in c)
        after = sum(1 for c in m.cells.values() if col in c)
        if after < before:
            raise RefuseToWrite(f"{col} 칸 수가 {before} → {after} 로 줄었습니다")
    ds = sorted(m.cells)
    if any(a >= b for a, b in zip(ds, ds[1:])):
        raise RefuseToWrite("날짜가 중복되거나 역행합니다")


# --------------------------------------------------------------------------
# 쓰기
# --------------------------------------------------------------------------
def install(cells: dict[date, dict[str, float]], path: Path) -> Optional[Path]:
    """임시 파일에 쓰고, **다시 읽어 정상인지 본 뒤** 제자리로 옮긴다.

    교체 직전 원본을 ``.bak`` 으로 남긴다. CSV 는 gitignore 에 있어서 git 으로
    되돌릴 수 없고, API 가 이상한 값을 주기 시작한 경우에는 다시 받는 것으로도
    복구가 안 된다 — 그때 필요한 것이 어제의 파일이다.
    """
    # 임시 이름에 PID 를 넣는다. 두 실행이 겹치면 같은 임시 파일을 서로 덮어쓰고,
    # 한쪽의 finally 가 다른 쪽이 쓰던 파일을 지운다.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    bak: Optional[Path] = None
    try:
        write_cells(cells, tmp)
        probe = load_csv_bundle(tmp)            # 파서를 통과하는가
        if not len(probe.market.price):
            raise RefuseToWrite("새로 쓴 파일에 가격이 없습니다")
        if path.exists():
            bak = path.with_name(path.name + ".bak")
            bak.write_bytes(path.read_bytes())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return bak


def staleness(cells: dict[date, dict[str, float]],
              *, today: Optional[date] = None) -> tuple[Optional[date], int]:
    """마지막 가격 날짜와 그것이 며칠 전인지."""
    today = today or date.today()
    priced = [d for d, c in cells.items() if "price" in c]
    if not priced:
        return None, 10**6
    last = max(priced)
    return last, (today - last).days


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def run(args) -> int:
    path = Path(args.csv)
    seen: dict = {}
    old = read_cells(path, out=seen)
    last, age = staleness(old)

    if old:
        print(f"현재 CSV  {path}  {min(old)} ~ {max(old)}  {len(old)}일")
        print(f"          마지막 가격 {last} ({age}일 전)")
        if age < 0:
            print("          ! 마지막 날짜가 미래입니다 — 파일이 손상됐을 수 있습니다")
    else:
        print(f"현재 CSV  {path} 없음 — 처음 받습니다")

    # 이 파일을 그대로 저장하면 잃는 것이 있는가. read_cells → write_cells 왕복은
    # **모르는 열을 열째로, 숫자로 안 읽히는 칸을 빈 칸으로 지운다.** 손으로 고쳐
    # 둔 것이 갱신 한 번에 날아가는 것이 이 도구가 막으려는 사고 바로 그것이다.
    # --check 는 쓰지 않으므로 알려만 주고 계속한다.
    lossy = bool(seen.get("unknown") or seen.get("unparsed"))
    if lossy:
        head = "\n중단 —" if not args.check else "\n경고 —"
        if seen.get("unknown"):
            print(f"{head} 이 도구가 모르는 열이 있습니다: {seen['unknown']}")
            print("  저장하면 그 열이 사라집니다. 열을 지우거나 "
                  "csv_source.COLUMNS 에 추가하세요.")
        if seen.get("unparsed"):
            n = len(seen["unparsed"])
            print(f"{head} 숫자로 읽히지 않는 칸이 {n}개 있습니다: "
                  f"{', '.join(seen['unparsed'][:3])}")
            print("  저장하면 빈 칸이 됩니다. 쉼표나 단위를 지우고 다시 실행하세요.")
        if not args.check:
            return 2

    if args.check:
        if age > args.max_age:
            print(f"\n오래됐습니다 — 마지막 데이터가 {age}일 전입니다 "
                  f"(허용 {args.max_age}일). 갱신이 필요합니다.")
            return 1
        print(f"\n신선합니다 — 허용 {args.max_age}일 안입니다.")
        return 0

    # --- 받기 ------------------------------------------------------------
    from btc_core.datasources import coinmetrics

    end = date.today()
    if args.full or not old:
        start = None
        span = f"전 구간(약 {coinmetrics.DEFAULT_YEARS}년)"
    else:
        start = min(last, end) - timedelta(days=args.days)
        span = f"{start} ~ {end}"
    print(f"\n받는 중 — CoinMetrics 커뮤니티 API, {span}")
    try:
        bundle = coinmetrics.fetch(start=start, end=end, timeout=args.timeout)
    except FetchError as exc:
        print(f"\n받기 실패: {exc}")
        return 3
    new = bundle_cells(bundle)
    npriced = sum(1 for c in new.values() if "price" in c)
    print(f"          {len(new)}일 수신 (가격 {npriced}일)")

    # --- 검증 ------------------------------------------------------------
    try:
        notes = check_fetched(new)
        notes += check_overlap(old, new)
        m = merge(old, new)
        check_merged(old, m)
    except RefuseToWrite as exc:
        print(f"\n검증 실패 — 쓰지 않았습니다.\n  {exc}")
        return 2
    for n in notes:
        print(f"          {n}")

    print(f"\n병합    {m.summary()}")
    if not m.touched:
        print("        파일을 건드리지 않았습니다.")
        return 0
    if args.dry_run:
        print("        --dry-run 이라 쓰지 않았습니다.")
        return 0

    try:
        bak = install(m.cells, path)
    except (RefuseToWrite, FetchError, OSError) as exc:
        # 새로 쓴 파일이 파서를 통과하지 못하는 경우도 FetchError 로 온다.
        print(f"\n쓰기 실패 — 원본은 그대로입니다.\n  {type(exc).__name__}: {exc}")
        return 2
    nlast, nage = staleness(m.cells)
    print(f"저장    {path}  {len(m.cells)}일  마지막 {nlast} ({nage}일 전)")
    if bak:
        print(f"        이전 파일은 {bak.name} 에 남겼습니다")
    if nage > STALE_DAYS:
        print(f"        ! 갱신했는데도 {nage}일 전이 최신입니다 — 원본이 늦거나 막혔습니다")

    if args.build:
        print("\n사이트 재생성")
        import build_viz

        # 거시(중국 M2·LRS) 패널을 빠뜨리지 않도록 macro CSV 를 넘긴다 — 안 넘기면
        # build_viz.py 직접 실행(기본 --macro data/macro.csv)과 결과가 달라진다.
        macro_csv = ROOT / "data" / "macro.csv"
        for p in build_viz.build(str(path), ROOT / args.out_dir,
                                 macro_csv=str(macro_csv) if macro_csv.exists() else None):
            print(f"        {p}  ({p.stat().st_size / 1024:.0f} KB)")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="CSV 를 CoinMetrics 로 증분 갱신")
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"마지막 날짜 기준 며칠 전부터 다시 받을지 (기본 {DEFAULT_DAYS})")
    ap.add_argument("--full", action="store_true", help="전 구간 다시 받기")
    ap.add_argument("--dry-run", action="store_true", help="바뀔 내용만 출력")
    ap.add_argument("--check", action="store_true",
                    help="받지 않고 현재 CSV 가 얼마나 오래됐는지만 확인")
    ap.add_argument("--max-age", type=int, default=STALE_DAYS,
                    help=f"--check 에서 허용할 지연 일수 (기본 {STALE_DAYS})")
    ap.add_argument("--build", action="store_true", help="갱신 후 사이트 재생성")
    ap.add_argument("--out-dir", default="viz/site")
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args(argv)
    if args.days < 1:
        ap.error("--days 는 1 이상이어야 합니다")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
