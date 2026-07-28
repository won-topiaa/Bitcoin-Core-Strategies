"""명령줄 인터페이스.

    btc-core fetch   --save data/market.csv     # 시계열 받아 저장
    btc-core score   --csv data/market.csv      # 점수와 실행 계획
    btc-core init                               # 수동 입력 템플릿 생성
    btc-core commit  --ladder distribute        # 실행한 계단 기록
    btc-core history                            # 지금까지 밟은 계단
    btc-core explain mvrv_z                     # 지표 하나의 앵커 표
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from .config import ConfigError, load_config
from .datasources import FetchError, load_manual
from .datasources.csv_source import load_csv_bundle, save_csv_bundle
from .datasources.manual import write_template
from .engine import evaluate
from .report import gauge, render_console, render_json, render_markdown
from .strategy import ExecutionState, commit_action, next_ladder_step

DEFAULT_STATE = Path("data/state.json")
DEFAULT_MANUAL = Path("data/manual_input.yaml")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="btc-core",
        description="BCS/LRS — 나만의 비트코인 핵심 지표 및 전략",
    )
    p.add_argument("--config", default=None, help="strategy.yaml 경로")
    sub = p.add_subparsers(dest="command", required=True)

    # fetch
    f = sub.add_parser("fetch", help="CoinMetrics 에서 시계열을 받는다")
    f.add_argument("--years", type=float, default=8.0)
    f.add_argument("--save", default="data/market.csv", help="CSV 저장 경로")
    f.add_argument("--timeout", type=int, default=60)

    # score
    s = sub.add_parser("score", help="점수와 실행 계획을 낸다")
    s.add_argument("--csv", default=None, help="로컬 CSV 사용 (없으면 원격 조회)")
    s.add_argument("--manual", default=str(DEFAULT_MANUAL))
    s.add_argument("--state", default=str(DEFAULT_STATE))
    s.add_argument("--as-of", default=None, help="기준일 YYYY-MM-DD")
    s.add_argument("--format", choices=("console", "markdown", "json"), default="console")
    s.add_argument("--out", default=None, help="파일로 저장")
    s.add_argument("--adaptive-weight", type=float, default=0.35,
                   help="퍼센타일 척도 혼합 비율 (0=고정 앵커만)")
    s.add_argument("--no-record", action="store_true", help="BCS 이력에 기록하지 않는다")
    s.add_argument("--years", type=float, default=8.0)

    # init
    i = sub.add_parser("init", help="수동 입력 템플릿을 만든다")
    i.add_argument("--path", default=str(DEFAULT_MANUAL))
    i.add_argument("--force", action="store_true")

    # commit
    c = sub.add_parser("commit", help="실제로 실행한 사다리 계단을 기록한다")
    c.add_argument("--ladder", choices=("distribute", "accumulate"), required=True)
    c.add_argument("--state", default=str(DEFAULT_STATE))
    c.add_argument("--csv", default=None)
    c.add_argument("--manual", default=str(DEFAULT_MANUAL))
    c.add_argument("--on", default=None, help="실행일 YYYY-MM-DD (기본: 오늘)")
    c.add_argument("--note", default="")
    c.add_argument("--force", action="store_true", help="보류 사유를 무시하고 기록")

    # history
    h = sub.add_parser("history", help="실행 이력을 본다")
    h.add_argument("--state", default=str(DEFAULT_STATE))

    # explain
    e = sub.add_parser("explain", help="지표의 정규화 앵커를 표로 본다")
    e.add_argument("indicator", nargs="?", default=None)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    try:
        return _dispatch(args, cfg)
    except FetchError as exc:
        print(f"데이터 오류: {exc}", file=sys.stderr)
        return 3
    except (ValueError, ConfigError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


def _dispatch(args, cfg) -> int:
    if args.command == "fetch":
        return _cmd_fetch(args)
    if args.command == "score":
        return _cmd_score(args, cfg)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "commit":
        return _cmd_commit(args, cfg)
    if args.command == "history":
        return _cmd_history(args)
    if args.command == "explain":
        return _cmd_explain(args, cfg)
    return 1


def _load_bundle(csv_path: Optional[str], years: float = 8.0, timeout: int = 60):
    if csv_path:
        return load_csv_bundle(csv_path)
    from .datasources import coinmetrics
    return coinmetrics.fetch(years=years, timeout=timeout)


def _cmd_fetch(args) -> int:
    from .datasources import coinmetrics
    bundle = coinmetrics.fetch(years=args.years, timeout=args.timeout)
    print(bundle.describe())
    for w in bundle.warnings:
        print(f"  ! {w}")
    path = save_csv_bundle(bundle, args.save)
    print(f"저장: {path}")
    return 0


def _cmd_score(args, cfg) -> int:
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    bundle = None
    try:
        bundle = _load_bundle(args.csv, years=args.years)
    except FetchError as exc:
        print(f"! 시계열을 불러오지 못했습니다: {exc}", file=sys.stderr)
        print("  수동 입력만으로 계속 진행합니다.\n", file=sys.stderr)

    manual = load_manual(args.manual, reference=as_of or date.today())
    state = ExecutionState.load(args.state)

    snap, state = evaluate(
        cfg,
        bundle=bundle,
        manual=manual,
        state=state,
        as_of=as_of,
        adaptive_weight=args.adaptive_weight,
        record=not args.no_record,
    )
    if not args.no_record:
        state.save(args.state)

    if args.format == "console":
        out = render_console(snap, cfg)
    elif args.format == "markdown":
        out = render_markdown(snap, cfg)
    else:
        out = render_json(snap)

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(out, encoding="utf-8")
        print(f"저장: {p}")
    else:
        print(out)
    return 0


def _cmd_init(args) -> int:
    p = Path(args.path)
    if p.exists() and not args.force:
        print(f"이미 있습니다: {p}  (덮어쓰려면 --force)", file=sys.stderr)
        return 1
    written = write_template(p)
    print(f"생성: {written}")
    print("BM Pro 차트를 보고 값을 채운 뒤  btc-core score  를 실행하세요.")
    return 0


def _cmd_commit(args, cfg) -> int:
    on = date.fromisoformat(args.on) if args.on else date.today()
    bundle = None
    try:
        bundle = _load_bundle(args.csv)
    except FetchError:
        pass

    manual = load_manual(args.manual, reference=on)
    state = ExecutionState.load(args.state)
    snap, state = evaluate(cfg, bundle=bundle, manual=manual, state=state, as_of=on)

    if snap.bcs is None:
        print("BCS 를 산출할 수 없어 기록하지 않습니다.", file=sys.stderr)
        return 1

    action = next_ladder_step(cfg, args.ladder, snap.bcs, snap.lrs, state, on)
    if action is None:
        print(f"현재 BCS {snap.bcs:+.1f} 에서 밟을 {args.ladder} 계단이 없습니다.", file=sys.stderr)
        return 1
    if not action.executable and not args.force:
        print(f"보류 중인 계단입니다: {action.blocked_by}", file=sys.stderr)
        print("정말 기록하려면 --force", file=sys.stderr)
        return 1

    if args.force and not action.executable:
        from .models import Action
        action = Action(action.kind, action.label, action.size_pct,
                        action.trigger, action.lrs_multiplier, blocked_by=None)

    commit_action(state, action, snap.bcs, on=on, note=args.note)
    state.save(args.state)
    print(f"기록: {action.label}")
    print(f"      BCS {snap.bcs:+.1f} / {on}")
    print(f"      누적 {state.cumulative(args.ladder):.0f}%")
    return 0


def _cmd_history(args) -> int:
    state = ExecutionState.load(args.state)
    if not state.steps:
        print("실행 이력이 없습니다.")
    else:
        print(f"{'날짜':<12} {'사다리':<12} {'트리거':>8} {'크기':>8}  {'BCS':>8}  메모")
        print("─" * 72)
        for s in sorted(state.steps, key=lambda x: x.executed_on):
            print(f"{s.executed_on.isoformat():<12} {s.ladder:<12} {s.trigger:>+8.0f} "
                  f"{s.size_pct:>7.1f}% {s.bcs_at_execution:>+8.1f}  {s.note}")
        print("─" * 72)
        for ladder in ("distribute", "accumulate"):
            cum = state.cumulative(ladder)
            if cum:
                print(f"누적 {ladder}: {cum:.1f}%")

    if state.bcs_history:
        recent = state.bcs_history[-14:]
        print("\n최근 BCS 기록")
        for d, v in recent:
            print(f"  {d.isoformat()}  {v:+7.1f}  {gauge(v / 100)}")
    return 0


def _cmd_explain(args, cfg) -> int:
    keys = [args.indicator] if args.indicator else list(cfg.indicators)
    for key in keys:
        if key not in cfg.indicators:
            print(f"알 수 없는 지표: {key}", file=sys.stderr)
            print(f"가능한 값: {', '.join(cfg.indicators)}", file=sys.stderr)
            return 1
        spec = cfg.indicators[key]
        print(f"\n{spec.get('label', key)}  [{key}]")
        print(f"  계열: {spec.get('family')}   소스: {spec.get('source')}")
        if spec.get("input_mode") == "categorical":
            print("  상태 → 점수")
            for st, sc in spec["states"].items():
                print(f"    {st:<16} {sc:+.2f}  {gauge(sc)}")
        else:
            print("  원시값 → 점수")
            for raw, sc in spec["anchors"]:
                print(f"    {raw:>10,.4f}    {sc:+.2f}  {gauge(sc)}")
    print()
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
