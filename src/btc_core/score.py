"""정규화된 지표 → 계열 점수 → BCS / LRS / 합의 판정.

핵심 설계 두 가지.

1. 거시(LRS)는 BCS 에 섞지 않는다.
   유동성이 풀린다는 사실은 "지금 비트코인이 비싸다/싸다"와 다른 종류의 정보다.
   섞으면 완화 국면에서 과열 신호가 희석되고, 긴축 국면에서 바닥 신호가
   과장된다. 원문 5장도 거시를 "신호를 믿을지 판단하는 배경"으로 규정한다.
   그래서 축을 둘로 나누고, LRS 는 실행 '크기'만 조절하게 했다.

2. 계산 재료가 같은 지표는 합산하지 않는다.
   MVRV Z-Score 와 NUPL 은 둘 다 (시총 − 실현시총)에서 나온다. 둘이 같은
   신호를 낸다고 두 번 세면 안 된다(원문 6.1). 그래서 밸류에이션 계열의
   집계 방식은 평균이 아니라 max_abs — 더 극단인 쪽 하나만 채택한다.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from .config import StrategyConfig
from .models import Consensus, FamilyScore, Reading
from .normalize import blend, categorical, clamp, percentile_rank, piecewise

PRICE_FAMILY = "price"


def score_indicator(
    cfg: StrategyConfig,
    key: str,
    raw: Optional[float | str],
    *,
    history: Optional[Sequence[float]] = None,
    adaptive_weight: float = 0.35,
    spec: Optional[Mapping] = None,
) -> Reading:
    """원시값 하나를 [-1, +1] 로 옮긴다."""
    spec = spec if spec is not None else cfg.indicator(key)
    label = spec.get("label", key)
    family = spec.get("family", "")
    source = spec.get("source", "manual")

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return Reading(key, label, family, None, None, source, note="결측")

    mode = spec.get("input_mode", "anchors")

    if mode == "categorical":
        try:
            score = categorical(str(raw), spec["states"], name=key)
        except Exception as exc:
            return Reading(key, label, family, raw, None, source, note=f"입력 오류: {exc}")
        return Reading(key, label, family, str(raw), score, source)

    try:
        value = float(raw)
    except (TypeError, ValueError):
        return Reading(key, label, family, raw, None, source, note="숫자로 해석할 수 없음")

    if spec.get("invert"):
        value = -value

    fixed = piecewise(value, spec["anchors"], name=key)
    note = ""
    score = fixed

    # 절대 수치는 사이클마다 낮아진다(원문 3.2, 7.1). 과거 분포가 충분히
    # 있으면 퍼센타일 척도를 섞어서 고정 앵커의 노후화를 완충한다.
    if history and len(history) >= 90:
        adaptive = percentile_rank(value, history)
        score = blend(fixed, adaptive, adaptive_weight)
        if abs(fixed - adaptive) > 0.45:
            note = (
                f"고정 앵커({fixed:+.2f})와 과거분포({adaptive:+.2f})가 크게 갈립니다 "
                f"— 앵커 재조정 검토"
            )

    return Reading(key, label, family, float(raw) if not isinstance(raw, str) else raw,
                   clamp(score), source, note=note)


def _aggregate(scores: list[float], mode: str) -> float:
    if not scores:
        raise ValueError("빈 목록은 집계할 수 없습니다.")
    if mode == "mean":
        return sum(scores) / len(scores)
    if mode == "max_abs":
        return max(scores, key=abs)
    if mode == "min":
        return min(scores)
    if mode == "max":
        return max(scores)
    raise ValueError(f"알 수 없는 집계 방식: {mode}")


def compute_bcs(
    cfg: StrategyConfig,
    readings: Mapping[str, Reading],
) -> tuple[Optional[float], tuple[FamilyScore, ...], float]:
    """계열 집계 후 가중합. 결측 계열이 있으면 가중치를 재정규화한다.

    반환: (BCS, 계열별 결과, 커버리지 0~1)
    """
    min_members = int(cfg.coverage.get("min_family_members", 1))

    staged: list[tuple[str, Mapping, list[Reading], Optional[float]]] = []
    for fam_key, fam in cfg.bcs_families.items():
        members = [readings[m] for m in fam["members"] if m in readings]
        usable = [r for r in members if r.available]
        score: Optional[float] = None
        if len(usable) >= min_members:
            score = clamp(_aggregate([r.score for r in usable], fam.get("aggregate", "mean")))
        staged.append((fam_key, fam, members, score))

    live_weight = sum(float(f["weight"]) for _, f, _, s in staged if s is not None)
    total_weight = sum(float(f["weight"]) for _, f, _, _ in staged)
    coverage = live_weight / total_weight if total_weight else 0.0

    families: list[FamilyScore] = []
    bcs = 0.0
    for fam_key, fam, members, score in staged:
        weight = float(fam["weight"])
        eff = (weight / live_weight * 100.0) if (score is not None and live_weight) else 0.0
        if score is not None:
            bcs += score * eff
        families.append(
            FamilyScore(
                key=fam_key,
                label=fam.get("label", fam_key),
                weight=weight,
                effective_weight=eff,
                score=score,
                members=tuple(members),
                aggregate=fam.get("aggregate", "mean"),
            )
        )

    return (round(bcs, 2) if live_weight else None), tuple(families), coverage


def compute_lrs(
    cfg: StrategyConfig,
    raw_values: Mapping[str, Optional[float | str]],
) -> tuple[Optional[float], tuple[Reading, ...], str]:
    """거시 축. BCS 와 같은 방식이되 계열 없이 구성요소 가중합이다."""
    readings: list[Reading] = []
    live_weight = 0.0
    acc = 0.0
    for key, spec in cfg.lrs_components.items():
        r = score_indicator(cfg, key, raw_values.get(key), spec=spec)
        r = Reading(r.key, r.label, "macro", r.raw, r.score, r.source, r.note)
        readings.append(r)
        if r.available:
            w = float(spec["weight"])
            live_weight += w
            acc += r.score * w

    if not live_weight:
        return None, tuple(readings), "unknown"

    lrs = round(acc / live_weight * 100.0, 2)
    band = cfg.lrs_band_for(lrs)
    return lrs, tuple(readings), band.get("key", "unknown")


def evaluate_consensus(
    cfg: StrategyConfig,
    families: Sequence[FamilyScore],
    bcs: Optional[float],
) -> Consensus:
    """원문 6.1 을 기계화한 게이트.

    계산 원리가 다른 계열이 최소 3개, 그중 가격 계열이 아닌 것이 최소 2개
    같은 방향을 가리켜야 실행을 허용한다. 가격 이동평균 4종이 한꺼번에
    빨개지는 흔한 착시를 이 조건이 걸러낸다.
    """
    rules = cfg.consensus
    min_abs = float(rules.get("min_family_abs", 0.30))

    if bcs is None:
        return Consensus(False, "neutral", (), (), "BCS 산출 불가 — 데이터 부족")

    direction = "overheated" if bcs > 0 else "undervalued" if bcs < 0 else "neutral"
    if direction == "neutral":
        return Consensus(False, "neutral", (), (), "BCS 중립 — 방향 없음")

    want_positive = direction == "overheated"
    available = [f for f in families if f.available]
    agreeing = [
        f for f in available
        if abs(f.score) >= min_abs
        and ((f.score > 0) if want_positive else (f.score < 0))
    ]
    non_price = [f for f in agreeing if f.key != PRICE_FAMILY]

    keys = tuple(f.key for f in agreeing)
    np_keys = tuple(f.key for f in non_price)

    need_total, need_non_price, scaled = _requirements(cfg, rules, available)

    if len(agreeing) < need_total:
        return Consensus(
            False, direction, keys, np_keys,
            f"같은 방향 계열 {len(agreeing)}개 < 필요 {need_total}개{scaled} — 합의 미달, 관망",
        )
    if len(non_price) < need_non_price:
        return Consensus(
            False, direction, keys, np_keys,
            f"비가격 계열 {len(non_price)}개 < 필요 {need_non_price}개{scaled} — "
            f"가격 이동평균에만 의존한 신호, 관망",
        )
    return Consensus(
        True, direction, keys, np_keys,
        f"계열 {len(agreeing)}개 합의(비가격 {len(non_price)}개){scaled} — 실행 조건 충족",
    )


def _requirements(cfg, rules, available: Sequence[FamilyScore]) -> tuple[int, int, str]:
    """필요 동의 계열 수. 결측이 있으면 남은 계열 수에 비례해 낮춘다.

    설정값(4계열 중 3개, 그중 비가격 2개)은 모든 계열이 살아 있을 때의 기준이다.
    이걸 그대로 두면 계열이 3개만 남았을 때 만장일치를 요구하게 되는데,
    **데이터가 적을수록 기준이 엄해지는 것은 거꾸로다.** 결측은 이미 커버리지
    하한(min_coverage_for_action)이 따로 막고 있으므로, 여기서는 비율만 지킨다.
    """
    need_total = int(rules.get("min_agreeing_families", 3))
    need_non_price = int(rules.get("min_non_price_agreeing", 2))
    if not rules.get("scale_with_available", True):
        return need_total, need_non_price, ""

    total = len(cfg.bcs_families)
    total_non_price = sum(1 for k in cfg.bcs_families if k != PRICE_FAMILY)
    n = len(available)
    n_non_price = sum(1 for f in available if f.key != PRICE_FAMILY)
    if n >= total:
        return need_total, need_non_price, ""

    floor_total = int(rules.get("min_agreeing_floor", 2))
    scaled_total = max(floor_total, round(need_total * n / total)) if total else need_total
    scaled_np = (
        max(1, round(need_non_price * n_non_price / total_non_price))
        if total_non_price else need_non_price
    )
    return (
        min(scaled_total, n),
        min(scaled_np, n_non_price),
        f" [계열 {n}/{total} 기준 완화]",
    )
