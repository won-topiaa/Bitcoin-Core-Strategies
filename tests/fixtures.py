"""검증용 합성 시계열.

실제 시장 데이터를 커밋하지 않고도 엔진 전체를 돌려보기 위한 것이다.
난수를 쓰지 않고 결정론적으로 만들어서, 테스트가 실행할 때마다 같은 값을 낸다.

모양은 실제 사이클을 흉내 낸다: 긴 축적 → 지수 상승 → 급락 → 재축적.
절대 수치는 실제 비트코인과 다르지만, 지표들이 "저평가 → 과열 → 저평가"를
제대로 따라가는지 확인하기에는 충분하다.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from btc_core.indicators import MarketData
from btc_core.series import Series

START = date(2018, 1, 1)
DAYS = 2600            # 약 7.1년 — 200주(1400일) 이동평균이 나오고도 남는다
BASE = 14_000.0        # 최근 사이클과 자릿수를 맞춰서 데모 출력이 읽히게 한다


def _cycle_price(i: int) -> float:
    """4년 주기 사인파 + 완만한 로그 성장 추세."""
    trend = BASE * math.exp(i / 1650.0)
    phase = 2.0 * math.pi * (i % 1461) / 1461.0
    swing = math.exp(1.05 * math.sin(phase - math.pi / 2))
    ripple = 1.0 + 0.02 * math.sin(i / 11.0)     # 잔물결 — 이동평균이 평탄해지지 않게
    return trend * swing * ripple


def price_series(days: int = DAYS, start: date = START) -> Series:
    return Series.from_pairs(
        [(start + timedelta(days=i), _cycle_price(i)) for i in range(days)],
        name="price",
    )


def flat_price_series(value: float = 50_000.0, days: int = DAYS, start: date = START) -> Series:
    """모든 이동평균이 같아지는 시계열. 배수가 정확히 1.0 이 나와야 한다."""
    return Series.from_pairs(
        [(start + timedelta(days=i), value) for i in range(days)], name="price"
    )


def market_data(days: int = DAYS, start: date = START) -> MarketData:
    """가격·시총·실현시총·발행량·해시레이트를 모두 갖춘 묶음."""
    price = price_series(days, start)
    supply_at = lambda i: 16_800_000.0 + 900.0 * i / 3.0     # 대략적인 유통량 증가

    dates = price.dates
    caps: list[tuple[date, float]] = []
    real: list[tuple[date, float]] = []
    issu: list[tuple[date, float]] = []
    sply: list[tuple[date, float]] = []
    hash_: list[tuple[date, float]] = []

    realized_price = BASE * 0.875
    for i, (d, p) in enumerate(zip(dates, price.values)):
        s = supply_at(i)
        caps.append((d, p * s))
        # 실현가격은 시장가를 아주 느리게 따라간다 — 실제 실현시총의 성질
        realized_price += (p - realized_price) * 0.0035
        real.append((d, realized_price * s))
        issu.append((d, 900.0 if i < 850 else 450.0 if i < 2310 else 225.0))
        sply.append((d, s))
        # 해시레이트는 가격을 지연 추종하고, 하락 뒤에는 실제로 줄어든다
        lag = price.values[max(0, i - 45)]
        hash_.append((d, 5.0e19 * (lag / 35_000.0) ** 0.55))

    return MarketData(
        price=price,
        market_cap=Series.from_pairs(caps, name="market_cap"),
        realized_cap=Series.from_pairs(real, name="realized_cap"),
        issuance_btc=Series.from_pairs(issu, name="issuance_btc"),
        supply=Series.from_pairs(sply, name="supply"),
        hashrate=Series.from_pairs(hash_, name="hashrate"),
    )


def truncated(md: MarketData, days: int) -> MarketData:
    """앞에서부터 days 일만 남긴다. 특정 사이클 국면을 재현할 때 쓴다."""
    def cut(s):
        return None if s is None else Series(s.dates[:days], s.values[:days], s.name)

    return MarketData(
        price=cut(md.price),
        market_cap=cut(md.market_cap),
        realized_cap=cut(md.realized_cap),
        issuance_btc=cut(md.issuance_btc),
        supply=cut(md.supply),
        hashrate=cut(md.hashrate),
    )


MANUAL_FULL = {
    "as_of": date.today().isoformat(),
    "indicators": {"rhodl": 0.45, "reserve_risk": 0.0031, "lth_mvrv": 1.59},
    "macro": {
        "m2_impulse": 5.2,
        "dxy_trend": -2.1,
        "fed_stance": "pause_easing",
        "etf_flow": 2.4,
    },
    "floors": {"lth_rp": 74_000, "cvdd": 41_000},
}
