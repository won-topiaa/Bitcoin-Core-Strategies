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
from .datasources.coinmetrics import DEFAULT_YEARS
from .datasources.macro import load_macro_signals
from .datasources.csv_source import load_csv_bundle, save_csv_bundle
from .datasources.manual import write_template
from .engine import evaluate
from .report import gauge, render_console, render_json, render_markdown
from .strategy import ExecutionState, commit_action, next_ladder_step

DEFAULT_STATE = Path("data/state.json")
DEFAULT_MANUAL = Path("data/manual_input.yaml")
DEFAULT_MACRO = Path("data/macro.csv")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="btc-core",
        description="BCS/LRS — 나만의 비트코인 핵심 지표 및 전략",
    )
    p.add_argument("--config", default=None, help="strategy.yaml 경로")
    sub = p.add_subparsers(dest="command", required=True)

    # fetch
    f = sub.add_parser("fetch", help="CoinMetrics 에서 시계열을 받는다")
    f.add_argument("--years", type=float, default=DEFAULT_YEARS)
    f.add_argument("--save", default="data/market.csv", help="CSV 저장 경로")
    f.add_argument("--timeout", type=int, default=60)

    # score
    s = sub.add_parser("score", help="점수와 실행 계획을 낸다")
    s.add_argument("--csv", default=None, help="로컬 CSV 사용 (없으면 원격 조회)")
    s.add_argument("--manual", default=str(DEFAULT_MANUAL))
    s.add_argument("--macro", default=str(DEFAULT_MACRO),
                   help="거시 CSV (중국 M2 임펄스 등 LRS 자동값). tools/fetch_macro.py 로 받는다")
    s.add_argument("--state", default=str(DEFAULT_STATE))
    s.add_argument("--as-of", default=None, help="기준일 YYYY-MM-DD")
    s.add_argument("--format", choices=("console", "markdown", "json"), default="console")
    s.add_argument("--out", default=None, help="파일로 저장")
    s.add_argument("--adaptive-weight", type=float, default=None,
                   help="퍼센타일 혼합 비율 (0=앵커만, 1=퍼센타일만). "
                        "생략하면 config 의 bcs.normalization.adaptive_weight")
    s.add_argument("--no-record", action="store_true", help="BCS 이력에 기록하지 않는다")
    s.add_argument("--no-derive", action="store_true",
                   help="빠진 유통량·발행량·시가총액을 반감기 스케줄로 채우지 않는다 "
                        "(실측만 쓰고 없으면 결측으로 남긴다)")
    s.add_argument("--years", type=float, default=DEFAULT_YEARS)

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
    c.add_argument("--macro", default=str(DEFAULT_MACRO))
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


def _load_bundle(csv_path: Optional[str], years: float = DEFAULT_YEARS,
                 timeout: int = 60, derive: bool = True):
    """시계열을 불러오고, 빠진 것 중 계산 가능한 것은 채운다.

    유통량·발행량·시가총액은 반감기 스케줄로 계산된다. 그래서 소스가 가격과
    실현시총만 줘도 지표 9개가 전부 산출된다 (datasources/derive.py).
    """
    if csv_path:
        bundle = load_csv_bundle(csv_path)
    else:
        from .datasources import coinmetrics
        bundle = coinmetrics.fetch(years=years, timeout=timeout)
    if derive:
        from .datasources.derive import backfill_bundle
        bundle = backfill_bundle(bundle)
    return bundle


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
        bundle = _load_bundle(args.csv, years=args.years,
                              derive=not args.no_derive)
    except FetchError as exc:
        print(f"! 시계열을 불러오지 못했습니다: {exc}", file=sys.stderr)
        print("  수동 입력만으로 계속 진행합니다.\n", file=sys.stderr)

    manual = load_manual(args.manual, reference=as_of or date.today())
    macro_signals = load_macro_signals(args.macro, reference=as_of)
    state = ExecutionState.load(args.state)

    snap, state = evaluate(
        cfg,
        bundle=bundle,
        manual=manual,
        state=state,
        as_of=as_of,
        adaptive_weight=args.adaptive_weight,
        macro_signals=macro_signals,
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
    macro_signals = load_macro_signals(args.macro, reference=on)
    state = ExecutionState.load(args.state)
    snap, state = evaluate(cfg, bundle=bundle, manual=manual, state=state, as_of=on,
                           macro_signals=macro_signals)

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
        # --force 는 확정 대기·간격 같은 '타이밍' 제약을 넘기라는 뜻이다.
        # 사이클 예산 한도는 타이밍이 아니라 약속이므로, 넘길 때 눈에 띄게 알린다.
        # (그래도 기록은 한다 — 실제로 판 것을 장부에서 빼면 이후 계산이 전부 틀어진다)
        if "한도 소진" in (action.blocked_by or ""):
            print("!! 경고: 이번 사이클 실행 한도를 넘겨 기록합니다.", file=sys.stderr)
            print(f"   {action.blocked_by}", file=sys.stderr)
            if action.kind == "distribute":
                core = float(cfg.ladders["distribute"]["core_hold_pct"])
                print(f"   코어 보유 {core:.0f}% 를 침범합니다 — 문서가 약속한 불가침 지분입니다.",
                      file=sys.stderr)
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
        if spec.get("explain"):
            print(f"  {spec['explain']}")
        fam = cfg.bcs_families.get(spec.get("family"), {})
        print(f"  계열: {fam.get('label', spec.get('family'))}   "
              f"소스: {spec.get('source')}")
        if spec.get("explain_long"):
            import textwrap as _tw
            print()
            for line in _tw.wrap(spec["explain_long"], width=72):
                print(f"  {line}")
            print()
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
