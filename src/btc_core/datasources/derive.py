"""네트워크 없이 계산되는 시계열 — 무료 데이터 의존을 최소화하는 장치.

무료 데이터로 지표를 만들려면 **외부에서 반드시 받아야 하는 시계열이 몇 개인가**가
곧 위험의 크기다. 소스가 하나 늘면 그 소스가 죽거나 유료화될 확률만큼 시스템이
멈출 확률이 커진다.

이 모듈은 그 개수를 줄인다. 비트코인의 발행 스케줄은 합의 규칙에 박혀 있어서
API 없이 계산할 수 있다.

    유통량   = 반감기 스케줄로 계산     (API 불필요)
    발행량   = 유통량의 일간 차분        (API 불필요)
    시가총액 = 가격 × 유통량            (가격만 있으면 됨)
    발행액   = 발행량 × 가격            (가격만 있으면 됨)

그래서 **외부에서 진짜로 받아야 하는 것은 `price` 와 `realized_cap` 둘뿐이다.**
실현시총만은 체인을 전부 훑어 UTXO 하나하나의 생성 시점 가격을 합산해야 나오는
값이라 계산으로 대체할 방법이 없다.

## 정확도 (CoinMetrics 실측 SplyCur 대비)

    전 구간   평균 절대오차 5.845%   최대 4553.740%   n=6,409
    2011년+   평균 0.295%            최대    6.650%   n=5,687
    2013년+   평균 0.176%            최대    1.258%   n=4,956
    2015년+   평균 0.079%            최대    0.989%   n=4,226
    2020년+   평균 0.011%            최대    0.051%   n=2,400

초기 오차가 터무니없이 큰 이유는 하나다. 2009년에는 해시레이트가 거의 없어서
블록이 10분보다 **훨씬 느리게** 나왔고, 이 모델은 반감기 구간 안에서 블록이
균일하게 나온다고 가정한다. 그래서 2009년 초에는 파생값이 실측의 수십 배가
된다. 다만 그 시절은 가격 데이터도 없어서 지표 계산에 들어오지 않는다.

**2015년 이후로는 평균 0.08%, 2020년 이후로는 0.01%다.** MVRV·NUPL·서멀캡을
계산할 때 이 정도 오차는 소수점 아래에서 사라진다.

파생 발행량은 **끝난 반감기 구간에서는 구조적으로 정확하다** — 구간 경계가
실측 날짜이고 구간 총량이 210,000 × 보상으로 고정이기 때문이다.

    era1 2012-11~2016-07   실측 3,982.6  파생 3,982.9 BTC/일   +0.01%
    era2 2016-07~2020-05   실측 1,872.9  파생 1,873.8          +0.05%
    era3 2020-05~2024-04   실측   912.6  파생   912.1          −0.05%
    era4 2024-04~ (진행중)  실측   451.9  파생   455.6          +0.82%

진행 중인 구간만 +0.82% 벌어져 있는데, 이건 **다음 반감기 날짜가 추정값**이기
때문이다. 2028-04-01 보다 늦게 오면 파생 일간 발행량은 그만큼 과대평가된다.
Puell 은 발행액을 자기 1460일 이동평균으로 나눈 **비율**이라 분자·분모에 같은
편차가 걸려 대부분 상쇄되고, 서멀캡도 누적합의 비율이라 같다.

## 쓰는 방법

파생값은 **실측값이 없을 때만** 채운다. 실측이 있으면 실측을 쓴다.

    from btc_core.datasources.derive import backfill
    market, notes = backfill(market)
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ..indicators import MarketData
from ..series import Series, ratio

GENESIS = date(2009, 1, 3)

# 반감기 스케줄. (구간 시작일, 그 구간의 블록 보상).
#
# 앞의 넷은 실제로 일어난 날짜이고 2028-04-01 은 추정이다. indicators.HALVINGS
# 와 같은 날짜를 쓴다 — 둘이 어긋나면 사이클 진행도와 유통량이 다른 달력을
# 보게 되므로, 아래 _check_schedule_matches_indicators() 가 그걸 막는다.
HALVING_SCHEDULE: tuple[tuple[date, float], ...] = (
    (GENESIS, 50.0),
    (date(2012, 11, 28), 25.0),
    (date(2016, 7, 9), 12.5),
    (date(2020, 5, 11), 6.25),
    (date(2024, 4, 20), 3.125),
    (date(2028, 4, 1), 1.5625),      # 추정
)

BLOCKS_PER_ERA = 210_000
HALVING_INTERVAL_DAYS = 1458     # 실측 반감기 간격 평균. 스케줄 뒤쪽 연장에 쓴다.
EXTEND_ERAS = 8                  # 2028 이후 8회(약 32년)까지 미리 채운다

# 진행 중인 반감기 구간에서 파생 발행량이 실측보다 이만큼 크다(+0.82%).
# 원인은 다음 반감기 날짜가 추정값이라는 것뿐이다 — 끝난 구간은 ±0.05% 안이다.
# 보정하지 않는다: Puell·서멀캡은 비율이라 상쇄되고, 보정계수는 곧 또 하나의
# 임의 파라미터가 된다. 절대량을 쓰는 코드가 생기면 여기를 보라는 표시로 남긴다.
DERIVED_ISSUANCE_BIAS = 0.0082


def _full_schedule() -> tuple[tuple[date, float], ...]:
    """추정 반감기를 뒤로 연장한 스케줄."""
    out = list(HALVING_SCHEDULE)
    last_date, last_reward = out[-1]
    for _ in range(EXTEND_ERAS):
        last_date = last_date + timedelta(days=HALVING_INTERVAL_DAYS)
        last_reward = last_reward / 2.0
        out.append((last_date, last_reward))
    return tuple(out)


_SCHEDULE = _full_schedule()

# 각 구간 시작 시점의 누적 유통량. 구간이 끝나면 정확히 210,000 × 보상이 늘어난다.
_CUMULATIVE: tuple[float, ...] = tuple(
    sum(BLOCKS_PER_ERA * reward for _, reward in _SCHEDULE[:i])
    for i in range(len(_SCHEDULE))
)


def derive_supply(as_of: date) -> float:
    """반감기 스케줄로 계산한 그 날의 누적 유통량(BTC).

    구간 안에서는 블록이 균일하게 나온다고 가정한다. 실제 블록 간격은
    난이도 조정 주기(2주)마다 흔들리지만, 구간 **경계**는 실측 날짜를 쓰므로
    구간별 총량은 정확하고 오차는 구간 내부의 배분에만 남는다.
    """
    if as_of <= GENESIS:
        return 0.0
    for i in range(len(_SCHEDULE) - 1, -1, -1):
        start, reward = _SCHEDULE[i]
        if as_of >= start:
            if i + 1 < len(_SCHEDULE):
                span = (_SCHEDULE[i + 1][0] - start).days
            else:
                span = HALVING_INTERVAL_DAYS
            elapsed = (as_of - start).days
            fraction = min(1.0, elapsed / span) if span > 0 else 1.0
            return _CUMULATIVE[i] + BLOCKS_PER_ERA * reward * fraction
    return 0.0


def derive_daily_issuance(as_of: date) -> float:
    """그 날 새로 발행된 BTC. 유통량의 일간 차분이다."""
    return max(0.0, derive_supply(as_of) - derive_supply(as_of - timedelta(days=1)))


def supply_series(dates: tuple[date, ...]) -> Series:
    return Series(dates, tuple(derive_supply(d) for d in dates), "supply(파생)")


def issuance_series(dates: tuple[date, ...]) -> Series:
    return Series(dates, tuple(derive_daily_issuance(d) for d in dates), "issuance_btc(파생)")


def _times(a: Series, b: Series, name: str) -> Series:
    """두 시계열의 곱. a 의 날짜를 기준으로 하고 b 는 그 날짜에서 조회한다."""
    lookup = dict(zip(b.dates, b.values))
    out: list[Optional[float]] = []
    for d, v in zip(a.dates, a.values):
        w = lookup.get(d)
        out.append(None if v is None or w is None else v * w)
    return Series(a.dates, tuple(out), name)


def backfill(market: MarketData) -> tuple[MarketData, tuple[str, ...]]:
    """결측 시계열을 계산으로 채운다. 실측이 있으면 손대지 않는다.

    채울 수 있는 것: supply, issuance_btc, issuance_usd, market_cap.
    채울 수 없는 것: realized_cap, hashrate, active_addresses, 거래소 계열.

    돌려주는 메모는 리포트에 그대로 실린다. **파생값을 쓰고 있다는 사실이
    화면에서 사라지면 안 된다** — 오차가 0.08% 라도 실측과 파생은 다른 것이다.
    """
    notes: list[str] = []
    price = market.price
    if not len(price):
        return market, ("가격이 없어 파생 계산을 건너뜁니다.",)

    dates = price.dates

    supply = market.supply
    if supply is None or supply.last is None:
        supply = supply_series(dates)
        notes.append(
            "유통량을 반감기 스케줄로 계산했습니다 "
            "(2015년 이후 실측 대비 평균오차 0.08%)"
        )

    issuance_btc = market.issuance_btc
    if issuance_btc is None or issuance_btc.last is None:
        issuance_btc = issuance_series(dates)
        notes.append(
            "발행량을 반감기 스케줄로 계산했습니다 "
            f"(끝난 구간은 ±0.05%, 진행 구간은 {DERIVED_ISSUANCE_BIAS * 100:+.1f}% — "
            "다음 반감기 날짜가 추정이라서)"
        )

    issuance_usd = market.issuance_usd
    if issuance_usd is None or issuance_usd.last is None:
        issuance_usd = _times(issuance_btc, price, "issuance_usd(파생)")
        notes.append("발행액을 발행량 × 가격으로 계산했습니다")

    market_cap = market.market_cap
    if market_cap is None or market_cap.last is None:
        market_cap = _times(price, supply, "market_cap(파생)")
        notes.append("시가총액을 가격 × 유통량으로 계산했습니다")

    filled = MarketData(
        price=price,
        market_cap=market_cap,
        realized_cap=market.realized_cap,
        issuance_btc=issuance_btc,
        issuance_usd=issuance_usd,
        hashrate=market.hashrate,
        supply=supply,
        active_addresses=market.active_addresses,
        exchange_supply=market.exchange_supply,
        exchange_inflow=market.exchange_inflow,
        exchange_outflow=market.exchange_outflow,
    )
    if market.realized_cap is None:
        notes.append(
            "실현시총은 계산으로 대체할 수 없습니다 — MVRV Z / NUPL 결측 "
            "(밸류에이션 계열 가중치 40 중 서멀캡만 남습니다)"
        )
    return filled, tuple(notes)


def backfill_bundle(bundle):
    """DataBundle 을 받아 파생값을 채운 새 번들을 돌려준다.

    메모는 경고 목록에 합쳐진다 — 리포트 상단에 그대로 뜨므로 파생값을 쓰는
    중이라는 사실이 화면에서 사라지지 않는다.
    """
    from .base import DataBundle, coverage_warnings

    market, notes = backfill(bundle.market)
    if not notes:
        return bundle
    return DataBundle(
        market=market,
        origin=f"{bundle.origin} + 반감기 스케줄 파생",
        warnings=tuple(notes) + coverage_warnings(market),
    )


def implied_mvrv(market: MarketData) -> Optional[Series]:
    """시가총액 ÷ 실현시총. 두 시계열이 다 있을 때의 확인용."""
    if market.market_cap is None or market.realized_cap is None:
        return None
    return ratio(market.market_cap, market.realized_cap)


def _check_schedule_matches_indicators() -> None:
    """반감기 날짜가 indicators.HALVINGS 와 같아야 한다.

    사이클 진행도(days_since_halving)와 유통량이 다른 달력을 보면 저점·고점
    프로파일과 점수가 서로 다른 사이클을 이야기하게 된다. import 시점에 깨뜨려서
    조용히 어긋나지 않게 한다.
    """
    from ..indicators import HALVINGS

    ours = tuple(d for d, _ in HALVING_SCHEDULE[1:])
    if ours != tuple(HALVINGS):
        raise AssertionError(
            f"반감기 스케줄 불일치: derive={ours} vs indicators={tuple(HALVINGS)}"
        )


_check_schedule_matches_indicators()
