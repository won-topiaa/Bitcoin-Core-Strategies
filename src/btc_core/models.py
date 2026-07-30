"""도메인 모델. 전부 불변 데이터클래스."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Optional


Direction = Literal["overheated", "undervalued", "neutral"]


@dataclass(frozen=True)
class Reading:
    """지표 하나의 관측값과 그 정규화 결과."""

    key: str
    label: str
    family: str
    raw: Optional[float | str]
    score: Optional[float]          # [-1, +1], 결측이면 None
    source: str                     # "auto" | "manual"
    note: str = ""

    @property
    def available(self) -> bool:
        return self.score is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "family": self.family,
            "raw": self.raw,
            "score": self.score,
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True)
class FamilyScore:
    """계열 하나의 집계 결과."""

    key: str
    label: str
    weight: float                   # 설정상의 원래 가중치
    effective_weight: float         # 결측 재정규화 후 실제 가중치
    score: Optional[float]          # [-1, +1]
    members: tuple[Reading, ...] = ()
    aggregate: str = "mean"

    @property
    def available(self) -> bool:
        return self.score is not None

    @property
    def direction(self) -> Direction:
        if self.score is None:
            return "neutral"
        if self.score > 0:
            return "overheated"
        if self.score < 0:
            return "undervalued"
        return "neutral"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "effective_weight": self.effective_weight,
            "score": self.score,
            "aggregate": self.aggregate,
            "members": [m.as_dict() for m in self.members],
        }


@dataclass(frozen=True)
class Consensus:
    """합의 게이트 판정 결과 (원문 6.1 의 기계화)."""

    passed: bool
    direction: Direction
    agreeing: tuple[str, ...]
    non_price_agreeing: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "direction": self.direction,
            "agreeing": list(self.agreeing),
            "non_price_agreeing": list(self.non_price_agreeing),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FloorLevel:
    key: str
    label: str
    price: Optional[float]
    weight: float

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "price": self.price, "weight": self.weight}


@dataclass(frozen=True)
class BuyZone:
    label: str
    price: float
    pct_of_reserve: float
    # 현재가 대비 (-30.0 = 30% 아래). 현재가를 모르면 None —
    # 0.0 으로 채우면 "지금 가격과 같다"로 읽혀서 정반대 의미가 된다.
    distance_pct: Optional[float]
    reached: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "price": self.price,
            "pct_of_reserve": self.pct_of_reserve,
            "distance_pct": self.distance_pct,
            "reached": self.reached,
        }


@dataclass(frozen=True)
class BottomCondition:
    """프로파일 조건 하나 (저점·고점 공용)."""

    key: str
    label: str
    op: str
    # between 연산자는 [하한, 상한] 두 값을 받는다
    threshold: float | tuple[float, float]
    unit: str
    value: Optional[float]
    met: bool
    observed: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "op": self.op,
            "threshold": (list(self.threshold) if isinstance(self.threshold, tuple)
                          else self.threshold),
            "unit": self.unit,
            "value": self.value, "met": self.met, "observed": self.observed,
        }


@dataclass(frozen=True)
class BottomProfile:
    """사이클 저점의 공통 조건 대비 현재 상태.

    **점수에 들어가지 않는다.** 임계값이 과거 저점을 보고 정해진 것이라
    순환논리 위험이 있어서, 바닥선과 같이 '보여주되 실행하지 않는' 취급이다.
    """

    conditions: tuple[BottomCondition, ...] = ()
    label: str = ""
    detail: str = ""

    @property
    def hits(self) -> int:
        return sum(1 for c in self.conditions if c.met)

    @property
    def total(self) -> int:
        return len(self.conditions)

    @property
    def evaluable(self) -> bool:
        return any(c.value is not None for c in self.conditions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits, "total": self.total,
            "label": self.label, "detail": self.detail,
            "conditions": [c.as_dict() for c in self.conditions],
        }


@dataclass(frozen=True)
class Action:
    """지금 실행할 한 단계."""

    kind: Literal["distribute", "accumulate", "dca", "hold"]
    label: str
    size_pct: float                 # 매도면 보유량 대비, 매수면 예비현금 대비, dca면 배수×100
    trigger: Optional[float]
    lrs_multiplier: float = 1.0
    blocked_by: Optional[str] = None

    @property
    def executable(self) -> bool:
        return self.blocked_by is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "size_pct": self.size_pct,
            "trigger": self.trigger,
            "lrs_multiplier": self.lrs_multiplier,
            "blocked_by": self.blocked_by,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class Plan:
    """스냅샷 하나에서 나온 실행 계획 전체."""

    band_key: str
    band_label: str
    stance: str
    dca_multiplier: float
    actions: tuple[Action, ...] = ()
    floors: tuple[FloorLevel, ...] = ()
    reference_floor: Optional[float] = None
    buy_zones: tuple[BuyZone, ...] = ()
    bottom_profile: Optional["BottomProfile"] = None
    top_profile: Optional["BottomProfile"] = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "band_key": self.band_key,
            "band_label": self.band_label,
            "stance": self.stance,
            "dca_multiplier": self.dca_multiplier,
            "actions": [a.as_dict() for a in self.actions],
            "floors": [f.as_dict() for f in self.floors],
            "reference_floor": self.reference_floor,
            "buy_zones": [z.as_dict() for z in self.buy_zones],
            "bottom_profile": self.bottom_profile.as_dict() if self.bottom_profile else None,
            "top_profile": self.top_profile.as_dict() if self.top_profile else None,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class BcsInterval:
    """BCS 를 점 하나가 아니라 구간으로 표현한 것.

    **새 파라미터가 하나도 없다.** 구간의 출처는 데이터 자체다 — 지표를 하나씩
    빼고 다시 계산한 값들(잭나이프)의 최소·최대다.

    이 구간이 답하는 질문은 이것이다. **"지표 하나가 틀렸거나 없었다면 판정이
    달라졌을까?"** 밸류에이션 계열이 `max_abs` 라 그 안의 지표 하나가 BCS 의
    40% 를 혼자 끌고 갈 수 있으므로, 이 질문은 형식적인 것이 아니다.

    `stable` 이 False 면 구간이 밴드 경계를 걸친다 — 그때는 점수 대신 "두 밴드
    사이"로 읽어야 한다.
    """

    point: float                    # 전체 지표로 계산한 BCS
    low: float
    high: float
    band_low: str                   # low 가 속한 밴드 키
    band_high: str
    dropped_low: str = ""           # 이 지표를 빼면 low 가 된다
    dropped_high: str = ""
    n_variants: int = 0             # 실제로 빼 본 지표 수

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def stable(self) -> bool:
        """구간 전체가 한 밴드 안에 있는가."""
        return self.band_low == self.band_high

    def as_dict(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "low": self.low,
            "high": self.high,
            "width": self.width,
            "band_low": self.band_low,
            "band_high": self.band_high,
            "stable": self.stable,
            "dropped_low": self.dropped_low,
            "dropped_high": self.dropped_high,
            "n_variants": self.n_variants,
        }


@dataclass(frozen=True)
class Snapshot:
    """특정 날짜의 전체 판정."""

    as_of: date
    price: Optional[float]
    bcs: Optional[float]                 # -100 ~ +100
    lrs: Optional[float]                 # -100 ~ +100
    lrs_band: str
    coverage: float                      # 0 ~ 1
    families: tuple[FamilyScore, ...] = ()
    lrs_readings: tuple[Reading, ...] = ()
    consensus: Optional[Consensus] = None
    plan: Optional[Plan] = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    bcs_interval: Optional[BcsInterval] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "price": self.price,
            "bcs": self.bcs,
            "bcs_interval": self.bcs_interval.as_dict() if self.bcs_interval else None,
            "lrs": self.lrs,
            "lrs_band": self.lrs_band,
            "coverage": self.coverage,
            "families": [f.as_dict() for f in self.families],
            "lrs_readings": [r.as_dict() for r in self.lrs_readings],
            "consensus": self.consensus.as_dict() if self.consensus else None,
            "plan": self.plan.as_dict() if self.plan else None,
            "warnings": list(self.warnings),
        }
