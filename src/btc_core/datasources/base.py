"""데이터 소스 공통 타입."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..indicators import MarketData
from ..series import Series


class FetchError(RuntimeError):
    """원격 데이터를 가져오지 못했을 때."""


@dataclass(frozen=True)
class DataBundle:
    """자동 수집된 시계열 묶음과 그 출처."""

    market: MarketData
    origin: str
    warnings: tuple[str, ...] = ()

    @property
    def price(self) -> Series:
        return self.market.price

    def describe(self) -> str:
        parts = [f"출처: {self.origin}", f"가격 {len(self.market.price)}일"]
        for name, s in (
            ("시총", self.market.market_cap),
            ("실현시총", self.market.realized_cap),
            ("발행량", self.market.issuance_btc),
            ("해시레이트", self.market.hashrate),
        ):
            if s is not None and len(s):
                parts.append(f"{name} {len(s)}일")
        return " / ".join(parts)


def coverage_warnings(market: MarketData) -> tuple[str, ...]:
    """계산에 필요한 최소 길이를 못 채운 시계열을 짚어준다."""
    out: list[str] = []
    n = len(market.price)
    if n < 350:
        out.append(f"가격 데이터 {n}일 — 350일 이동평균(Pi Cycle/GRM) 산출 불가")
    elif n < 730:
        out.append(f"가격 데이터 {n}일 — 2년 이동평균 산출 불가")
    elif n < 1400:
        out.append(f"가격 데이터 {n}일 — 200주 이동평균 산출 불가")

    if market.realized_cap is None:
        out.append("실현시총 없음 — MVRV Z-Score / NUPL 결측 (밸류에이션 계열 전체가 빠집니다)")
    if market.hashrate is None:
        out.append("해시레이트 없음 — Hash Ribbons 결측")
    if market.issuance_btc is None and market.supply is None:
        out.append("발행량/유통량 없음 — Puell Multiple 결측")

    gaps = market.price.gaps()
    if gaps:
        worst = max(gaps, key=lambda g: g[1])
        out.append(f"가격 시계열에 {len(gaps)}개 구멍 (최대 {worst[1]}일, {worst[0]} 이후)")
    return tuple(out)


def optional_series(pairs, name: str) -> Optional[Series]:
    """값이 하나도 없으면 None 을 돌려준다."""
    items = [(d, v) for d, v in pairs if v is not None]
    if not items:
        return None
    return Series.from_pairs(items, name=name)
