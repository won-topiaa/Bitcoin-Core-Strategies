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
    """저점 프로파일 조건 하나."""

    key: str
    label: str
    op: str
    threshold: float
    unit: str
    value: Optional[float]
    met: bool
    observed: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "op": self.op,
            "threshold": self.threshold, "unit": self.unit,
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
            "notes": list(self.notes),
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "price": self.price,
            "bcs": self.bcs,
            "lrs": self.lrs,
            "lrs_band": self.lrs_band,
            "coverage": self.coverage,
            "families": [f.as_dict() for f in self.families],
            "lrs_readings": [r.as_dict() for r in self.lrs_readings],
            "consensus": self.consensus.as_dict() if self.consensus else None,
            "plan": self.plan.as_dict() if self.plan else None,
            "warnings": list(self.warnings),
        }
