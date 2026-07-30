"""조립부 — 데이터에서 스냅샷 하나를 만들어 내는 전체 경로.

    시계열 + 수동입력  →  지표 원시값  →  정규화  →  계열  →  BCS/LRS
                                                              ↓
                                          합의 게이트  →  실행 계획
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from .config import StrategyConfig
from .datasources.base import DataBundle
from .datasources.manual import ManualInput
from .indicators import (
    bottom_profile_inputs, compute_all, ma200w_price, realized_price_level,
    top_profile_inputs,
)
from .models import Reading, Snapshot
from .score import compute_bcs, compute_lrs, evaluate_consensus, score_indicator
from .strategy import ExecutionState, build_plan


def evaluate(
    cfg: StrategyConfig,
    *,
    bundle: Optional[DataBundle] = None,
    manual: Optional[ManualInput] = None,
    state: Optional[ExecutionState] = None,
    as_of: Optional[date] = None,
    adaptive_weight: Optional[float] = None,   # None → cfg.adaptive_weight
    record: bool = True,
) -> tuple[Snapshot, ExecutionState]:
    """스냅샷 하나를 만든다. 상태(state)는 BCS 이력이 갱신되어 함께 반환된다."""
    manual = manual or ManualInput(as_of=None)
    state = state or ExecutionState()
    if adaptive_weight is None:
        adaptive_weight = cfg.adaptive_weight

    warnings: list[str] = list(manual.warnings)

    auto: dict[str, object] = {}
    histories: dict[str, list[float]] = {}
    price: Optional[float] = None
    auto_floor_ma200w: Optional[float] = None
    auto_floor_rp: Optional[float] = None
    data_date: Optional[date] = None
    bottom_inputs: dict[str, Optional[float]] = {}
    top_inputs: dict[str, Optional[float]] = {}

    if bundle is not None:
        warnings.extend(bundle.warnings)
        market = bundle.market.normalized()
        computed = compute_all(market)
        for key, iv in computed.items():
            if iv.value is not None:
                auto[key] = iv.value
            hist = iv.detail.get("history_4y")
            if hist:
                histories[key] = list(hist)
        price = market.price.last
        data_date = market.price.last_date
        auto_floor_ma200w = ma200w_price(market.price)
        auto_floor_rp = realized_price_level(market.realized_cap, market.supply)
        bottom_inputs = bottom_profile_inputs(market, computed)
        top_inputs = top_profile_inputs(market, computed, as_of=as_of or data_date)

    resolved_date = as_of or data_date or manual.as_of or date.today()

    # ---- 지표 점수화 -------------------------------------------------------
    readings: dict[str, Reading] = {}
    for key, spec in cfg.indicators.items():
        source = spec.get("source", "manual")
        raw = auto.get(key) if source == "auto" else manual.indicators.get(key)
        # 자동 소스가 비었으면 수동 입력이 대신 채울 수 있게 허용한다
        if raw is None and source == "auto":
            raw = manual.indicators.get(key)
        readings[key] = score_indicator(
            cfg, key, raw,
            history=histories.get(key),
            adaptive_weight=adaptive_weight,
            spec=spec,
        )

    for r in readings.values():
        if r.note and r.note != "결측":
            warnings.append(f"{r.label}: {r.note}")

    bcs, families, coverage = compute_bcs(cfg, readings)

    if bcs is not None and record:
        state.record_bcs(resolved_date, bcs)

    # ---- 거시 축 -----------------------------------------------------------
    lrs, lrs_readings, lrs_band = compute_lrs(cfg, manual.macro)

    # ---- 합의 -------------------------------------------------------------
    consensus = evaluate_consensus(cfg, families, bcs)

    # ---- 바닥선 -----------------------------------------------------------
    # 숫자로 안 읽히는 값을 조용히 버리면 바닥선 하나가 소리 없이 빠지고
    # 나머지 둘로 가중평균이 다시 매겨진다 — 기준 바닥이 통째로 달라진다.
    def _floor(key: str) -> Optional[float]:
        raw = manual.floors.get(key)
        v = _as_float(raw)
        if v is None and raw not in (None, ""):
            warnings.append(f"바닥선 {key}: {raw!r} 을 숫자로 읽지 못해 제외했습니다")
        elif v is not None and v <= 0:
            warnings.append(f"바닥선 {key}: {v} 는 가격이 될 수 없어 제외했습니다")
            return None
        return v

    floor_prices: dict[str, Optional[float]] = {
        "ma200w": auto_floor_ma200w,
        "realized_price": auto_floor_rp,
        "lth_rp": _floor("lth_rp"),
        "cvdd": _floor("cvdd"),
    }
    # 수동 입력이 자동 선을 직접 적었다면 그쪽을 존중한다
    for auto_key in ("ma200w", "realized_price"):
        if manual.floors.get(auto_key) is not None:
            floor_prices[auto_key] = _floor(auto_key)

    if price is None:
        price = _floor("price")

    plan = build_plan(
        cfg,
        bcs=bcs,
        lrs=lrs,
        coverage=coverage,
        families=families,
        consensus=consensus,
        price=price,
        floor_prices=floor_prices,
        state=state,
        as_of=resolved_date,
        bottom_inputs=bottom_inputs,
        top_inputs=top_inputs,
    )

    missing = [r.label for r in readings.values() if not r.available]
    if missing:
        warnings.append(f"결측 지표 {len(missing)}개: {', '.join(missing)}")
    if coverage < 1.0:
        warnings.append(f"계열 커버리지 {coverage:.0%} — 남은 계열로 가중치를 재정규화했습니다.")

    snapshot = Snapshot(
        as_of=resolved_date,
        price=price,
        bcs=bcs,
        lrs=lrs,
        lrs_band=lrs_band,
        coverage=coverage,
        families=families,
        lrs_readings=lrs_readings,
        consensus=consensus,
        plan=plan,
        warnings=tuple(dict.fromkeys(warnings)),   # 순서 유지 중복 제거
    )
    return snapshot, state


def _as_float(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
