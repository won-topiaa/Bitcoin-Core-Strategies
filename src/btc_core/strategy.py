"""점수 → 실행 계획.

원문 6.3 이 이 파일의 전부다. "극단 구간 진입은 분할 실행을 시작할 시점이지,
전량을 처분하거나 매수할 시점이 아니다."

그래서 여기서 나오는 것은 매매 지시가 아니라 **다음 한 계단**이다.
사다리는 순서대로만 오르내리고, 같은 계단을 신호 흔들림으로 두 번 밟지
않도록 히스테리시스가 걸려 있다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .config import StrategyConfig
from .models import (
    Action, BottomCondition, BottomProfile, BuyZone, Consensus, FamilyScore,
    FloorLevel, Plan,
)


# ---------------------------------------------------------------------------
# 실행 이력 — 어떤 계단을 언제 밟았는지
# ---------------------------------------------------------------------------

@dataclass
class ExecutedStep:
    ladder: str                 # "distribute" | "accumulate"
    trigger: float
    executed_on: date
    bcs_at_execution: float
    size_pct: float
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ladder": self.ladder,
            "trigger": self.trigger,
            "executed_on": self.executed_on.isoformat(),
            "bcs_at_execution": self.bcs_at_execution,
            "size_pct": self.size_pct,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ExecutedStep":
        return cls(
            ladder=d["ladder"],
            trigger=float(d["trigger"]),
            executed_on=date.fromisoformat(d["executed_on"]),
            bcs_at_execution=float(d["bcs_at_execution"]),
            size_pct=float(d["size_pct"]),
            note=d.get("note", ""),
        )


@dataclass
class ExecutionState:
    """사다리 진행 상황. JSON 한 파일로 들고 다닌다."""

    steps: list[ExecutedStep] = field(default_factory=list)
    bcs_history: list[tuple[date, float]] = field(default_factory=list)

    # -- 조회 --------------------------------------------------------------
    def executed(self, ladder: str, trigger: float) -> Optional[ExecutedStep]:
        for s in reversed(self.steps):
            if s.ladder == ladder and abs(s.trigger - trigger) < 1e-9:
                return s
        return None

    def last_execution_date(self) -> Optional[date]:
        if not self.steps:
            return None
        return max(s.executed_on for s in self.steps)

    def cumulative(self, ladder: str, since: Optional[date] = None) -> float:
        return sum(
            s.size_pct for s in self.steps
            if s.ladder == ladder and (since is None or s.executed_on >= since)
        )

    def last_visit(self, *, at_or_below: Optional[float] = None,
                   at_or_above: Optional[float] = None) -> Optional[date]:
        """BCS 가 지정한 수준을 마지막으로 통과한 날. 사이클 경계를 잡는 데 쓴다."""
        found = None
        for d, v in sorted(self.bcs_history):
            if at_or_below is not None and v <= at_or_below:
                found = d
            if at_or_above is not None and v >= at_or_above:
                found = d
        return found

    def min_bcs_since(self, since: date) -> Optional[float]:
        vals = [v for d, v in self.bcs_history if d >= since]
        return min(vals) if vals else None

    def max_bcs_since(self, since: date) -> Optional[float]:
        vals = [v for d, v in self.bcs_history if d >= since]
        return max(vals) if vals else None

    def recent_bcs(self, days: int, as_of: date) -> list[float]:
        """as_of 를 포함한 최근 관측 BCS 값들 (최신순 아님, 날짜순)."""
        return [v for d, v in sorted(self.bcs_history) if (as_of - d).days < days and d <= as_of]

    def record_bcs(self, on: date, value: float) -> None:
        self.bcs_history = [(d, v) for d, v in self.bcs_history if d != on]
        self.bcs_history.append((on, value))
        self.bcs_history.sort()

    # -- 저장/로드 ----------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.as_dict() for s in self.steps],
            "bcs_history": [[d.isoformat(), v] for d, v in self.bcs_history],
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "ExecutionState":
        p = Path(path)
        if not p.exists():
            return cls()
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(
            steps=[ExecutedStep.from_dict(s) for s in data.get("steps", [])],
            bcs_history=[(date.fromisoformat(d), float(v)) for d, v in data.get("bcs_history", [])],
        )


# ---------------------------------------------------------------------------
# LRS 조정계수
# ---------------------------------------------------------------------------

def lrs_multiplier(cfg: StrategyConfig, action_kind: str, lrs: Optional[float]) -> float:
    """유동성 레짐이 트랜치 크기를 바꾼다. 방향은 절대 바꾸지 않는다."""
    mod = cfg.lrs_modifier
    if not mod.get("enabled") or lrs is None:
        return 1.0
    lo, hi = mod.get("bounds", [0.75, 1.25])
    for rule in mod.get("rules", []):
        if rule.get("action") != action_kind:
            continue
        if "lrs_min" in rule and lrs >= float(rule["lrs_min"]):
            return max(float(lo), min(float(hi), float(rule["multiplier"])))
        if "lrs_max" in rule and lrs <= float(rule["lrs_max"]):
            return max(float(lo), min(float(hi), float(rule["multiplier"])))
    return 1.0


# ---------------------------------------------------------------------------
# 사다리
# ---------------------------------------------------------------------------

def _confirmed(cfg: StrategyConfig, state: ExecutionState, as_of: date,
               trigger: float, want_above: bool) -> tuple[bool, str]:
    """임계 돌파가 confirm_days 만큼 **유지됐는지** 확인한다.

    세는 것은 실행 횟수가 아니라 **기간**이다. 예전에는 "최근 3일 안에 기록이
    3개 있고 전부 임계를 넘었는가"였는데, 그러면 매일 돌리지 않는 사람은
    영원히 확정되지 않는다. 운영 문서는 주 1회를 권하는데 주 1회로 돌리면
    최근 3일 안의 기록이 항상 1개뿐이라, BCS 가 1년 내내 −55 여도 매집
    사다리가 한 번도 열리지 않았다.

    그래서 "연속으로 임계를 유지한 구간이 confirm_days 만큼 뻗어 있는가"로
    바꿨다. 막으려던 것(하루짜리 스파이크)은 그대로 막힌다 — 일주일 간격의
    두 관측이 모두 임계를 넘으려면 스파이크로는 안 되기 때문이다.

    다만 기간만 보면 **희소한 기록이 유지의 증거로 둔갑한다.** 17개월 전
    기록 하나와 오늘 기록 하나로 "531일 유지" 가 되어 버리는데, 그 사이에
    사이클이 통째로 하나 지나갔을 수 있다. 그래서 구간 안의 관측 간격이
    confirm_max_gap_days 를 넘으면 거기서 구간을 끊는다.
    """
    need = int(cfg.hysteresis.get("confirm_days", 0))
    if need <= 0:
        return True, ""
    max_gap = int(cfg.hysteresis.get("confirm_max_gap_days", 35))

    holds = lambda v: (v >= trigger) if want_above else (v <= trigger)
    history = [(d, v) for d, v in sorted(state.bcs_history) if d <= as_of]
    if not history:
        return False, "확정 대기 — BCS 기록이 없습니다"

    # 최신부터 거슬러 올라가며 임계가 깨지지 않은 구간을 찾는다.
    # 관측이 너무 띄엄띄엄하면 그 지점에서 끊는다 — 사이의 공백은 증거가 아니다.
    run: list[tuple[date, float]] = []
    for d, v in reversed(history):
        if not holds(v):
            break
        if run and (run[-1][0] - d).days > max_gap:
            break
        run.append((d, v))
    if not run:
        return False, "확정 대기 — 가장 최근 기록이 임계를 넘지 못했습니다"
    if (as_of - run[0][0]).days > max_gap:
        return False, (
            f"확정 대기 — 가장 최근 기록이 {(as_of - run[0][0]).days}일 전입니다 "
            f"(최대 {max_gap}일)"
        )

    span = (run[0][0] - run[-1][0]).days
    if span < need - 1:
        return False, (
            f"확정 대기 — 임계 유지 {span + 1}/{need}일 "
            f"(관측 {len(run)}개, 기록이 더 필요합니다)"
        )
    return True, ""


def _rearmed(cfg: StrategyConfig, state: ExecutionState, step: ExecutedStep,
             trigger: float, want_above: bool) -> bool:
    """이미 밟은 계단이 다시 살아났는지.

    재무장은 **사이클을 넘나드는 장치**다. 한 사이클 안에서 같은 계단을 다시
    밟게 하려는 것이 아니다. 그래서 조건은 "임계에서 조금 되돌아왔는가"가
    아니라 **"반대편 구간까지 갔다 왔는가"** 여야 한다.

    16년 실측에서 확인된 것: 임계 근처 몇 점의 되돌림을 재무장으로 인정하면
    BCS의 일상적 변동만으로 계단이 2주마다 무한히 재발동한다. 2017년 강세장
    한 번에 +45 계단이 4번, 매집 사다리는 누적 795%까지 실행됐다.
    분배 사다리를 판 만큼 다시 살 코인이 없는데도 계속 파는 셈이다.
    """
    opposite = float(cfg.hysteresis.get("rearm_opposite_bcs", 0))
    if opposite > 0:
        if want_above:
            low = state.min_bcs_since(step.executed_on)
            return low is not None and low <= -opposite
        high = state.max_bcs_since(step.executed_on)
        return high is not None and high >= opposite

    # 구버전 설정 호환 — 단순 되돌림 폭. 위 이유로 권장하지 않는다.
    margin = float(cfg.hysteresis.get("reset_margin", 0))
    if margin <= 0:
        return False
    if want_above:
        low = state.min_bcs_since(step.executed_on)
        return low is not None and low <= trigger - margin
    high = state.max_bcs_since(step.executed_on)
    return high is not None and high >= trigger + margin


def cycle_start(cfg: StrategyConfig, state: ExecutionState, ladder: str) -> Optional[date]:
    """이번 사이클의 시작점 — BCS 가 마지막으로 반대편 구간을 방문한 날.

    한도 계산과 화면에 찍는 누적치가 **같은 경계**를 써야 한다. 한쪽은
    사이클 기준인데 다른 쪽이 전 기간 누적이면, 한도에 여유가 있는데도
    "이미 200% 분배했다"고 표시되어 약속이 깨진 것처럼 읽힌다.
    """
    opposite = float(cfg.hysteresis.get("rearm_opposite_bcs", 0))
    if opposite <= 0:
        return None
    if ladder == "distribute":
        return state.last_visit(at_or_below=-opposite)
    return state.last_visit(at_or_above=opposite)


def _budget_exceeded(cfg: StrategyConfig, state: ExecutionState,
                     ladder: str, size: float) -> Optional[str]:
    """이번 사이클에 남은 한도를 넘는지.

    설정 검증은 사다리 **계단의 합**이 한도를 넘지 않는지만 본다. 그런데
    재무장으로 같은 계단이 다시 살아나면 실제 실행 합계는 그 한도를 넘을 수
    있다. 16년 실측에서 2017 사이클 분배 누적이 70%까지 가서 코어 40% 를
    침범했다. "코어는 어떤 신호에도 팔지 않는다"는 약속을 지키려면 설정
    검증만으로는 부족하고 실행 시점에 막아야 한다.

    사이클 경계는 BCS 가 마지막으로 반대편 구간을 방문한 시점으로 잡는다.
    재무장 규칙과 같은 기준이라 둘이 어긋나지 않는다.
    """
    since = cycle_start(cfg, state, ladder)
    if ladder == "distribute":
        cap = 100.0 - float(cfg.ladders.get("distribute", {}).get("core_hold_pct", 0))
        label = "분배"
    else:
        cap = 100.0
        label = "매집"

    used = state.cumulative(ladder, since=since)
    if used + size > cap + 1e-9:
        scope = "이번 사이클" if since else "누적"
        return (
            f"{scope} {label} 한도 소진 — 이미 {used:.0f}%, 한도 {cap:.0f}%"
            + (" (코어 지분 보호)" if ladder == "distribute" else "")
        )
    return None


def next_ladder_step(
    cfg: StrategyConfig,
    ladder: str,
    bcs: float,
    lrs: Optional[float],
    state: ExecutionState,
    as_of: date,
    *,
    blocked_by: Optional[str] = None,
) -> Optional[Action]:
    """지금 밟을 수 있는 사다리 계단 하나. 없으면 None.

    계단은 순서대로만 밟는다. BCS 가 갑자기 뛰어 여러 계단이 동시에
    조건을 만족해도 가장 낮은(=아직 안 밟은 가장 앞) 계단부터 하나씩이다.
    """
    spec = cfg.ladders.get(ladder, {})
    steps = spec.get("steps", [])
    if not steps:
        return None

    want_above = ladder == "distribute"
    size_key = "pct_of_holdings" if want_above else "pct_of_reserve"

    for idx, raw in enumerate(steps, start=1):
        trigger = float(raw["trigger_bcs"])
        reached = bcs >= trigger if want_above else bcs <= trigger
        if not reached:
            continue

        done = state.executed(ladder, trigger)
        if done and not _rearmed(cfg, state, done, trigger, want_above):
            continue    # 이미 밟았고 아직 재무장 안 됨 → 다음 계단으로

        base = float(raw[size_key])
        mult = lrs_multiplier(cfg, ladder, lrs)
        size = round(base * mult, 2)

        reason = blocked_by
        if reason is None:
            reason = _budget_exceeded(cfg, state, ladder, size)
        if reason is None:
            ok, why = _confirmed(cfg, state, as_of, trigger, want_above)
            if not ok:
                reason = why
        if reason is None:
            gap = int(cfg.hysteresis.get("min_days_between_steps", 0))
            last = state.last_execution_date()
            if last is not None and (as_of - last).days < gap:
                remaining = gap - (as_of - last).days
                reason = f"직전 실행 후 {gap}일 간격 미충족 — {remaining}일 남음"

        unit = "보유 BTC" if want_above else "예비 현금"
        label = (
            f"{'분배' if want_above else '매집'} {idx}단 "
            f"(BCS {trigger:+.0f} 돌파) — {unit}의 {size:.1f}%"
        )
        if abs(mult - 1.0) > 1e-9:
            label += f"  [기본 {base:.0f}% × 유동성 계수 {mult:.2f}]"

        return Action(
            kind=ladder,                      # "distribute" | "accumulate"
            label=label,
            size_pct=size,
            trigger=trigger,
            lrs_multiplier=mult,
            blocked_by=reason,
        )

    return None


# ---------------------------------------------------------------------------
# 바닥선과 분할 매수 구간
# ---------------------------------------------------------------------------

def build_floors(
    cfg: StrategyConfig,
    price: Optional[float],
    floor_prices: Mapping[str, Optional[float]],
) -> tuple[tuple[FloorLevel, ...], Optional[float], tuple[BuyZone, ...]]:
    """세 바닥선의 가중평균을 기준 바닥으로 삼고 매수 구간을 깐다."""
    lines_cfg = cfg.floors.get("lines", [])
    levels: list[FloorLevel] = []
    live_weight = 0.0
    acc = 0.0
    for spec in lines_cfg:
        key = spec["key"]
        p = floor_prices.get(key)
        p = float(p) if p not in (None, "") else None
        w = float(spec["weight"])
        levels.append(FloorLevel(key, spec.get("label", key), p, w))
        if p is not None and p > 0:
            live_weight += w
            acc += p * w

    reference = round(acc / live_weight, 2) if live_weight else None
    if reference is None:
        return tuple(levels), None, ()

    zones: list[BuyZone] = []
    for z in cfg.floors.get("buy_zones", []):
        zp = reference * float(z["ratio_to_floor"])
        dist = round((zp / price - 1.0) * 100.0, 1) if price else None
        zones.append(
            BuyZone(
                label=z.get("label", ""),
                price=round(zp, 2),
                pct_of_reserve=float(z["pct_of_reserve"]),
                distance_pct=dist,
                reached=bool(price is not None and price <= zp),
            )
        )
    return tuple(levels), reference, tuple(zones)


# ---------------------------------------------------------------------------
# 저점 프로파일
# ---------------------------------------------------------------------------

_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def build_bottom_profile(cfg: StrategyConfig, values: Mapping[str, Optional[float]]) -> Optional[BottomProfile]:
    """사이클 저점의 공통 조건 대비 현재 상태.

    점수에 들어가지 않는다. 조건 임계값이 과거 저점을 보고 정해진 것이라
    정밀도가 과대평가돼 있고, 표본도 세 사이클뿐이다. 그래도 "BCS 는 싸다고
    하는데 역사적 바닥 조건은 몇 개나 맞나"는 실전에서 서로 다른 질문이라
    함께 보여줄 가치가 있다.
    """
    spec = cfg.bottom_profile
    if not spec.get("enabled"):
        return None

    conditions: list[BottomCondition] = []
    for c in spec.get("conditions", []):
        raw = values.get(c["key"])
        val = float(raw) if raw is not None else None
        op = _OPS.get(c.get("op", "<"))
        met = bool(val is not None and op and op(val, float(c["threshold"])))
        conditions.append(
            BottomCondition(
                key=c["key"], label=c.get("label", c["key"]), op=c.get("op", "<"),
                threshold=float(c["threshold"]), unit=c.get("unit", ""),
                value=val, met=met, observed=c.get("observed", ""),
            )
        )

    hits = sum(1 for c in conditions if c.met)
    label, detail = "", ""
    for r in sorted(spec.get("readings", []), key=lambda x: -int(x["min_hits"])):
        if hits >= int(r["min_hits"]):
            label, detail = r.get("label", ""), r.get("detail", "")
            break
    return BottomProfile(conditions=tuple(conditions), label=label, detail=detail)


# ---------------------------------------------------------------------------
# 계획 조립
# ---------------------------------------------------------------------------

def build_plan(
    cfg: StrategyConfig,
    *,
    bcs: Optional[float],
    lrs: Optional[float],
    coverage: float,
    families: Sequence[FamilyScore],
    consensus: Optional[Consensus],
    price: Optional[float],
    floor_prices: Mapping[str, Optional[float]],
    state: ExecutionState,
    as_of: date,
    bottom_inputs: Optional[Mapping[str, Optional[float]]] = None,
) -> Plan:
    notes: list[str] = []
    profile = build_bottom_profile(cfg, bottom_inputs or {})

    if bcs is None:
        levels, ref, zones = build_floors(cfg, price, floor_prices)
        return Plan(
            band_key="unknown",
            band_label="판정 불가",
            stance="데이터 부족 — 지표부터 채우세요",
            dca_multiplier=1.0,
            actions=(Action("hold", "관망 — BCS 산출 불가", 0.0, None,
                            blocked_by="입력 데이터 부족"),),
            floors=levels,
            reference_floor=ref,
            buy_zones=zones,
            bottom_profile=profile,
            notes=("사용 가능한 계열이 없습니다. config 의 지표 중 최소 한 계열은 채워야 합니다.",),
        )

    band = cfg.band_for(bcs)
    band_key = band["key"]
    dca_mult = float(cfg.ladders["dca_multiplier"][band_key])

    # 실행을 막는 사유들 — 우선순위 순
    blocked: Optional[str] = None
    min_cov = float(cfg.coverage.get("min_coverage_for_action", 0.0))
    if coverage < min_cov:
        blocked = f"커버리지 {coverage:.0%} < 최소 {min_cov:.0%} — 사다리 실행 금지"
        notes.append(blocked)
    elif consensus is not None and not consensus.passed:
        blocked = consensus.reason

    actions: list[Action] = []

    # 정기 DCA 는 합의 게이트와 무관하게 항상 살아 있다.
    # 사다리는 '추가' 판단이고, DCA 는 판단을 미루는 장치다.
    actions.append(
        Action(
            kind="dca",
            label=f"정기 DCA — 평소 적립액 × {dca_mult:.1f}",
            size_pct=dca_mult * 100.0,
            trigger=None,
        )
    )

    ladder = "distribute" if bcs > 0 else "accumulate"
    step = next_ladder_step(cfg, ladder, bcs, lrs, state, as_of, blocked_by=blocked)
    if step is not None:
        actions.append(step)
    else:
        since = cycle_start(cfg, state, ladder)
        cum = state.cumulative(ladder, since=since)
        if cum > 0:
            scope = "이번 사이클" if since else "누적"
            notes.append(
                f"{'분배' if ladder == 'distribute' else '매집'} 사다리 {scope} {cum:.0f}% 실행됨 — "
                f"현재 BCS 에서 밟을 새 계단 없음"
            )

    if not any(a.kind in ("distribute", "accumulate") for a in actions):
        actions.append(Action("hold", "사다리 대기 — 다음 계단 임계 미도달", 0.0, None))

    # 코어 지분 안내 — 한도와 같은 사이클 경계를 써야 숫자가 서로 어긋나지 않는다
    core = float(cfg.ladders.get("distribute", {}).get("core_hold_pct", 0))
    if core:
        since = cycle_start(cfg, state, "distribute")
        sold = state.cumulative("distribute", since=since)
        scope = "이번 사이클" if since else "누적"
        notes.append(f"코어 보유 {core:.0f}% 는 어떤 신호에도 매도하지 않음 ({scope} 분배 {sold:.0f}%)")

    # 계열이 서로 갈리는 상황은 그 자체로 정보다
    live = [f for f in families if f.available]
    if live:
        pos = [f.label for f in live if f.score > 0.3]
        neg = [f.label for f in live if f.score < -0.3]
        if pos and neg:
            notes.append(f"계열 충돌 — 과열: {', '.join(pos)} / 저평가: {', '.join(neg)}. 판단 보류가 정답일 확률이 높습니다.")

    if lrs is not None:
        lb = cfg.lrs_band_for(lrs)
        notes.append(f"유동성 레짐: {lb.get('label')} (LRS {lrs:+.0f}) — 신호의 방향이 아니라 실행 속도에 반영됨")

    levels, ref, zones = build_floors(cfg, price, floor_prices)
    if ref and price:
        downside = (ref / price - 1.0) * 100.0
        notes.append(f"기준 바닥 {ref:,.0f} — 현재가 대비 {downside:+.1f}%")

    if profile is not None and profile.evaluable and bcs is not None and bcs < 0:
        # BCS 가 싸다고 말할 때, 역사적 바닥 조건은 몇 개나 맞는지 함께 본다.
        # 둘은 다른 질문이고 갈릴 때가 있다.
        notes.append(
            f"저점 프로파일 {profile.hits}/{profile.total} — {profile.label} (점수에는 반영되지 않음)"
        )

    return Plan(
        band_key=band_key,
        band_label=band.get("label", band_key),
        stance=band.get("stance", ""),
        dca_multiplier=dca_mult,
        actions=tuple(actions),
        floors=levels,
        reference_floor=ref,
        buy_zones=zones,
        bottom_profile=profile,
        notes=tuple(notes),
    )


def commit_action(state: ExecutionState, action: Action, bcs: float, on: Optional[date] = None,
                  note: str = "") -> ExecutionState:
    """실제로 실행한 계단을 이력에 기록한다."""
    if action.kind not in ("distribute", "accumulate"):
        raise ValueError("사다리 계단만 기록할 수 있습니다.")
    if not action.executable:
        raise ValueError(f"실행이 막힌 계단입니다: {action.blocked_by}")
    if action.trigger is None:
        raise ValueError("트리거가 없는 계단입니다.")
    state.steps.append(
        ExecutedStep(
            ladder=action.kind,
            trigger=action.trigger,
            executed_on=on or datetime.now().date(),
            bcs_at_execution=bcs,
            size_pct=action.size_pct,
            note=note,
        )
    )
    return state
