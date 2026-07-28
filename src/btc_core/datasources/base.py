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
            ("발행액", self.market.issuance_usd),
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

    if market.market_cap is None:
        out.append("시가총액 없음 — MVRV Z-Score / NUPL / 서멀캡 전부 결측 "
                   "(밸류에이션 계열 전체가 빠집니다)")
    elif market.realized_cap is None:
        out.append("실현시총 없음 — MVRV Z-Score / NUPL 결측")
    if market.hashrate is None:
        out.append("해시레이트 없음 — Hash Ribbons 결측")

    # Puell 은 issuance_usd → issuance_btc → supply 차분 순으로 대체한다.
    # 셋 다 없을 때만 결측이다.
    if market.issuance_usd is None and market.issuance_btc is None and market.supply is None:
        out.append("발행량/유통량 없음 — Puell Multiple 결측")
    if market.issuance_usd is None and market.issuance_btc is None:
        out.append("발행액 없음 — 서멀캡 배수 결측")
    else:
        # 서멀캡 분모는 창세부터의 누적이라야 한다. 창을 자르면 배수가 부풀려진다.
        from ..indicators import THERMOCAP_ORIGIN_CUTOFF

        src = market.issuance_usd or market.issuance_btc
        if src.dates and src.dates[0] > THERMOCAP_ORIGIN_CUTOFF:
            out.append(
                f"발행액이 {src.dates[0]} 부터 — 누적 채굴수익이 창세부터가 아니라 "
                f"서멀캡 배수를 산출하지 않습니다 (fetch --years 를 늘리세요)"
            )

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
