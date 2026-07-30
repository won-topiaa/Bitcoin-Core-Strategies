"""원시 시계열 → 지표 원시값.

여기서 나오는 값은 아직 정규화 전이다. "MVRV Z-Score = 2.4" 같은,
BM Pro 차트에서 눈으로 읽는 것과 같은 단위의 숫자를 만든다.

자동 계산이 가능한 지표만 여기 있다. RHODL·Reserve Risk·LTH 실현가격·CVDD 는
무료 데이터로 재현할 수 없어서 수동 입력(datasources/manual.py)으로 받는다.
그 경계는 docs/02-핵심지표-정의.md 에 표로 정리해 뒀다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .series import (
    Series,
    expanding_stdev,
    history_of,
    ratio,
    resample_daily,
    sma,
)

# 이동평균 창 (일 단위)
PI_FAST = 111
PI_SLOW = 350
GRM_BASE = 350
MA_2Y = 730
MA_200W = 1400          # 200주 = 1400일
# Puell 기준선. **365일이 아니라 반감기를 넘는 1460일이다.**
#
# 원래 정의는 365일이지만, 그 형태는 공급 지표가 아니다. 반감기 사이 발행량이
# 일정하므로
#     (발행량 × 가격) ÷ MA365(발행량 × 가격)  →  가격 ÷ MA365(가격)
# 로 약분된다. 실측으로 확인했다 — 365일 Puell 은 '가격 ÷ MA365(가격)'과
# ρ +0.889, GRM(가격 ÷ MA350)과 ρ +0.890 이다. 즉 가격 지표가 공급 계열의
# 옷을 입고 있었고, 합의 게이트가 그것을 '비가격 확인'으로 세고 있었다.
#
# 1460일은 반감기 간격(약 1458일)을 넘으므로 발행량이 창 안에서 반으로 줄어
# 약분이 일어나지 않는다. 임의로 고른 값이 아니라 '반감기를 하나 포함하는
# 가장 짧은 창'이다.
#
# 실측 개선 (2026-07-30):
#   Puell ↔ GRM        +0.890 → +0.495
#   가격 계열 ↔ 공급 계열 +0.716 → +0.584
#   선행상관 ρ(365일)    -0.07 → -0.39
#   십분위 분리도        +39%p → +135%p
#   사이클별 부호        +0.24/-0.21/-0.46/+0.03 → -0.20/-0.51/-0.61/-0.50
#
# 대가도 있다. 16년 백테스트에서 2021 사이클 분배 평단이 그 사이클 고점의
# 49% → 42% 로 나빠졌다(매집은 네 사이클 모두 개선). 그래도 바꾼 이유는,
# 백테스트 7%p 보다 '공급 계열이 실제로 비가격 확인이 되는 것'이 합의 게이트의
# 전제이기 때문이다. 자세한 근거는 docs/13.
#
# 창이 길어져 이력 요구가 늘었다 — 유효 관측이 5,491일 → 4,396일이 된다.
PUELL_WINDOW = 1460
HASH_FAST = 30
HASH_SLOW = 60

HASH_RECOVERY_WINDOW = 45     # 재돌파 후 이 기간까지를 '회복' 신호로 본다
HASH_EXPANSION_RATIO = 1.08   # 30일선이 60일선을 8% 이상 웃돌면 '팽창'

# 서멀캡은 '창세 이래' 누적이라야 의미가 있다. 발행액 시계열이 이 날짜보다
# 늦게 시작하면 분모가 그만큼 작아져 배수가 부풀려진다.
# 실측(2026-07-28): 8년 창이면 +10.1%, 4년 창이면 +84.4% 과대평가된다.
# 밸류에이션 계열은 max_abs 라 부풀려진 값 하나가 30% 지분을 통째로 가져간다.
THERMOCAP_ORIGIN_CUTOFF = date(2011, 1, 1)


@dataclass(frozen=True)
class MarketData:
    """지표 계산에 필요한 원시 시계열 묶음.

    price 만 있어도 가격 계열 4개는 전부 계산된다. 나머지는 있으면 쓰고
    없으면 해당 지표를 결측으로 남긴다.
    """

    price: Series
    market_cap: Optional[Series] = None
    realized_cap: Optional[Series] = None
    issuance_btc: Optional[Series] = None      # 일간 신규 발행량(BTC)
    issuance_usd: Optional[Series] = None      # 그 발행량의 달러 가치 (있으면 우선)
    hashrate: Optional[Series] = None
    supply: Optional[Series] = None            # issuance 가 없을 때 차분으로 대체
    active_addresses: Optional[Series] = None  # 일간 활성 주소 수
    exchange_supply: Optional[Series] = None   # 거래소 보유량 (BTC)
    exchange_inflow: Optional[Series] = None   # 일간 거래소 유입 (BTC)
    exchange_outflow: Optional[Series] = None  # 일간 거래소 유출 (BTC)

    def up_to(self, end: date) -> "MarketData":
        """기준일 이후 데이터를 잘라낸다.

        이게 없으면 ``--as-of 2018-12-15`` 가 **2018년 날짜에 2026년 숫자**를
        붙여 보고한다. 리포트가 거짓말을 하는 것이라 조용히 넘길 수 없다.
        """
        def _c(s: Optional[Series]) -> Optional[Series]:
            if s is None:
                return None
            out = s.slice_to(end)
            return out if len(out) else None

        return MarketData(
            price=self.price.slice_to(end),
            market_cap=_c(self.market_cap),
            realized_cap=_c(self.realized_cap),
            issuance_btc=_c(self.issuance_btc),
            issuance_usd=_c(self.issuance_usd),
            hashrate=_c(self.hashrate),
            supply=_c(self.supply),
            active_addresses=_c(self.active_addresses),
            exchange_supply=_c(self.exchange_supply),
            exchange_inflow=_c(self.exchange_inflow),
            exchange_outflow=_c(self.exchange_outflow),
        )

    def normalized(self) -> "MarketData":
        """모든 시계열을 일간 연속으로 맞춘다."""
        def _r(s: Optional[Series]) -> Optional[Series]:
            return resample_daily(s) if s is not None and len(s) else None

        return MarketData(
            price=resample_daily(self.price),
            market_cap=_r(self.market_cap),
            realized_cap=_r(self.realized_cap),
            issuance_btc=_r(self.issuance_btc),
            issuance_usd=_r(self.issuance_usd),
            hashrate=_r(self.hashrate),
            supply=_r(self.supply),
            active_addresses=_r(self.active_addresses),
            exchange_supply=_r(self.exchange_supply),
            exchange_inflow=_r(self.exchange_inflow),
            exchange_outflow=_r(self.exchange_outflow),
        )


@dataclass(frozen=True)
class IndicatorValue:
    key: str
    value: Optional[float | str]
    as_of: Optional[date]
    detail: dict = field(default_factory=dict)
    note: str = ""


# ---------------------------------------------------------------------------
# 가격 계열 — 가격 시계열만 있으면 전부 계산된다
# ---------------------------------------------------------------------------

def pi_cycle_ratio(price: Series) -> IndicatorValue:
    """111일 이동평균 ÷ (350일 이동평균 × 2).

    1.0 도달이 곧 Pi Cycle Top 교차다. 비율로 두면 "얼마나 가까운지"를
    연속적으로 볼 수 있어서, 교차 이후에야 알게 되는 이진 신호보다 쓸모가 있다.
    """
    fast = sma(price, PI_FAST)
    slow = sma(price, PI_SLOW)
    f, s = fast.last, slow.last
    if f is None or s in (None, 0):
        return IndicatorValue("pi_cycle", None, price.last_date, note="이동평균 산출에 데이터 부족")
    val = f / (s * 2.0)
    # history_4y 는 퍼센타일 정규화의 재료다. 고정 앵커는 마지막 사이클에 맞춰
    # 조정된 것이라 사이클을 넘어 일반화되지 않는다 — 네 고점에서의 BCS
    # 표준편차가 앵커 전용 23.6 vs 퍼센타일 전용 16.2 였다(tools/robustness.py).
    # 그래서 모든 수치 지표가 퍼센타일 경로를 쓸 수 있게 이력을 함께 낸다.
    ratio_series = fast.map_with(slow, lambda a, b: a / (b * 2.0) if b else None)
    return IndicatorValue(
        "pi_cycle", val, price.last_date,
        detail={"ma111": f, "ma350": s, "ma350x2": s * 2.0, "crossed": val >= 1.0,
                "history_4y": history_of(ratio_series, years=4.0)},
    )


def golden_ratio_multiple(price: Series) -> IndicatorValue:
    """가격 ÷ 350일 이동평균. Golden Ratio Multiplier 상의 현재 배수."""
    base = sma(price, GRM_BASE)
    p, b = price.last, base.last
    if p is None or b in (None, 0):
        return IndicatorValue("grm", None, price.last_date, note="350일 이동평균 산출 불가")
    val = p / b
    fibs = [1.6, 2, 3, 5, 8, 13, 21]
    next_band = next((f for f in fibs if f > val), None)
    return IndicatorValue(
        "grm", val, price.last_date,
        detail={
            "ma350": b,
            "bands": {str(f): b * f for f in fibs},
            "next_band": next_band,
            "next_band_price": b * next_band if next_band else None,
            "history_4y": history_of(ratio(price, base), years=4.0),
        },
    )


def ma_multiple(price: Series, window: int, key: str) -> IndicatorValue:
    base = sma(price, window)
    p, b = price.last, base.last
    if p is None or b in (None, 0):
        return IndicatorValue(key, None, price.last_date, note=f"{window}일 이동평균 산출 불가")
    return IndicatorValue(
        key, p / b, price.last_date,
        detail={"ma": b, "window": window,
                "history_4y": history_of(ratio(price, base), years=4.0)},
    )


def ma200w_multiple(price: Series) -> IndicatorValue:
    """200주 이동평균 배수. 히트맵의 색 대신 숫자로 본다.

    원문 4.2 의 히트맵은 200주선의 '상승 속도'를 색으로 칠한 것이지만,
    매수·매도 판단에 쓰이는 실질 정보는 "지금 가격이 그 선의 몇 배인가"다.
    상승 속도는 detail 에 함께 담아 참고용으로 남긴다.
    """
    iv = ma_multiple(price, MA_200W, "ma200w_mult")
    base = sma(price, MA_200W)
    slope = None
    if len(base) > 30:
        cur, prev = base.values[-1], base.values[-31]
        if cur is not None and prev not in (None, 0):
            slope = (cur / prev - 1.0) * 100.0
    detail = dict(iv.detail)
    detail["monthly_slope_pct"] = slope
    detail["heatmap_hint"] = _heatmap_hint(slope)
    return IndicatorValue(iv.key, iv.value, iv.as_of, detail=detail, note=iv.note)


def _heatmap_hint(slope_pct: Optional[float]) -> Optional[str]:
    """200주선 월간 상승률을 히트맵 색 이름으로 되돌린다."""
    if slope_pct is None:
        return None
    if slope_pct < 0.6:
        return "파랑/보라 — 매수 구간"
    if slope_pct < 1.2:
        return "청록 — 식은 구간"
    if slope_pct < 2.0:
        return "노랑 — 중립"
    if slope_pct < 3.0:
        return "주황 — 과열 진입"
    return "빨강 — 과열"


def investor_tool_multiple(price: Series) -> IndicatorValue:
    """2년 이동평균 배수 (Bitcoin Investor Tool)."""
    iv = ma_multiple(price, MA_2Y, "ma2y_mult")
    base = sma(price, MA_2Y)
    detail = dict(iv.detail)
    if base.last:
        detail["buy_line"] = base.last          # 초록선
        detail["sell_line"] = base.last * 5.0   # 빨간선
    return IndicatorValue(iv.key, iv.value, iv.as_of, detail=detail, note=iv.note)


# ---------------------------------------------------------------------------
# 밸류에이션 계열 — 실현시총이 필요하다
# ---------------------------------------------------------------------------

def mvrv_zscore(market_cap: Optional[Series], realized_cap: Optional[Series]) -> IndicatorValue:
    """(시가총액 − 실현시총) ÷ 시가총액의 확장창 표준편차."""
    if market_cap is None or realized_cap is None:
        return IndicatorValue("mvrv_z", None, None, note="시총/실현시총 데이터 없음")
    diff = market_cap.map_with(realized_cap, lambda m, r: m - r)
    if not any(v is not None for v in diff.values):
        # 두 시계열이 각각 값을 갖고 있는데 결합 결과가 비었다면 날짜가 안 맞는 것이다.
        # 조용히 결측으로 넘기면 원인을 찾기 어려워진다.
        return IndicatorValue(
            "mvrv_z", None, market_cap.last_date,
            note="시총과 실현시총의 날짜가 겹치지 않습니다 — 데이터 소스를 확인하세요",
        )
    sd = expanding_stdev(market_cap)
    z = ratio(diff, sd)
    if z.last is None:
        return IndicatorValue("mvrv_z", None, market_cap.last_date, note="표준편차 산출에 데이터 부족")
    mv = market_cap.last
    rv = realized_cap.last
    return IndicatorValue(
        "mvrv_z", z.last, market_cap.last_date,
        detail={
            "market_cap": mv,
            "realized_cap": rv,
            "mvrv": (mv / rv) if mv and rv else None,
            "stdev": sd.last,
            "history_4y": history_of(z, years=4.0),
        },
    )


def nupl(market_cap: Optional[Series], realized_cap: Optional[Series]) -> IndicatorValue:
    """(시가총액 − 실현시총) ÷ 시가총액."""
    if market_cap is None or realized_cap is None:
        return IndicatorValue("nupl", None, None, note="시총/실현시총 데이터 없음")
    m, r = market_cap.last, realized_cap.last
    if m in (None, 0) or r is None:
        return IndicatorValue("nupl", None, market_cap.last_date, note="시총/실현시총 값 없음")
    val = (m - r) / m
    series = market_cap.map_with(realized_cap, lambda a, b: (a - b) / a if a else None)
    return IndicatorValue(
        "nupl", val, market_cap.last_date,
        detail={"phase": nupl_phase(val), "history_4y": history_of(series, years=4.0)},
    )


def thermocap_multiple(
    market_cap: Optional[Series],
    issuance_usd: Optional[Series],
    issuance_btc: Optional[Series] = None,
    price: Optional[Series] = None,
) -> IndicatorValue:
    """시가총액 ÷ 누적 채굴 수익(서멀캡).

    분모는 비트코인이 만들어진 이래 채굴자에게 지급된 달러의 총합이다.
    "이 네트워크를 만드는 데 지금까지 얼마가 들었나" 대비 "시장이 얼마로
    평가하나"를 보는 셈이다.

    MVRV 와 분자는 같지만 분모가 다르다(실현시총 vs 누적 채굴수익). 실측
    순위상관은 +0.77 로, 같은 방향을 보되 같은 것을 보지는 않는다.

    **고정 앵커를 쓰면 안 된다.** 분모가 단조증가하고 반감기마다 증가
    속도가 느려지므로 비율에 구조적 상승 추세가 있다. 사이클 저점 값이
    2.11(2015) → 5.57(2018) → 6.85(2022) 로 계속 올라갔다.
    그래서 config 에서 이 지표만 퍼센타일 전용으로 정규화한다.
    """
    if market_cap is None:
        return IndicatorValue("thermocap", None, None, note="시총 데이터 없음")

    revenue = issuance_usd
    if revenue is None and issuance_btc is not None and price is not None:
        # 달러 값이 없으면 수량 × 종가로 대신한다. 데이터 제공자가 블록별
        # 시점 가격으로 계산한 값보다는 부정확하지만 누적하면 차이가 묻힌다.
        revenue = issuance_btc.map_with(price, lambda i, p: i * p if i else None)
    if revenue is None:
        return IndicatorValue("thermocap", None, market_cap.last_date, note="발행액 데이터 없음")

    # 분모가 창세부터 누적된 것이 아니면 배수 자체가 다른 숫자다.
    # 그럴듯한 값을 조용히 내주는 대신 결측으로 둔다 — 이 계열은 max_abs 라
    # 부풀려진 값 하나가 밸류에이션 30% 를 전부 끌고 가기 때문이다.
    first = next((d for d, v in zip(revenue.dates, revenue.values) if v is not None), None)
    if first is None or first > THERMOCAP_ORIGIN_CUTOFF:
        return IndicatorValue(
            "thermocap", None, market_cap.last_date,
            note=f"발행액이 {first} 부터라 누적 채굴수익이 창세부터가 아닙니다 "
                 f"— 배수가 과대평가됩니다 (fetch --years 를 늘리세요)",
        )

    cumulative: list[Optional[float]] = []
    total = 0.0
    for v in revenue.values:
        if v is not None:
            total += v
        cumulative.append(total if total > 0 else None)
    thermocap = Series(revenue.dates, tuple(cumulative), "thermocap")

    mult = ratio(market_cap, thermocap)
    if mult.last is None:
        return IndicatorValue("thermocap", None, market_cap.last_date,
                              note="누적 채굴수익 산출 불가")
    return IndicatorValue(
        "thermocap", mult.last, market_cap.last_date,
        detail={"thermocap_usd": thermocap.last, "history_4y": history_of(mult, years=4.0)},
    )


ACTIVE_ADDR_WINDOW = 90       # 활성주소는 요일 효과가 커서 평활이 필요하다

# 반감기 날짜. 사이클 진행도를 재는 유일한 시간 축이다.
# 미래 반감기는 대략값이라 사이클이 끝날 무렵 다시 확인해야 한다.
HALVINGS = (
    date(2012, 11, 28),
    date(2016, 7, 9),
    date(2020, 5, 11),
    date(2024, 4, 20),
    date(2028, 4, 1),      # 추정
)


def days_since_halving(as_of: date) -> Optional[float]:
    """직전 반감기 이후 경과일.

    온체인 지표가 아니라 달력이다. 그래서 점수에 넣지 않는다. 다만 실측된
    규칙성이 상당하다 — 사이클 고점이 반감기 이후 367 / 526 / 548 / 534일에
    나왔다. 최근 세 사이클만 보면 편차가 22일이다.

    **4년 리듬이 깨지면 이 값은 즉시 무의미해진다.** 그래서 참고 정보로만 쓴다.
    """
    past = [h for h in HALVINGS if h <= as_of]
    return float((as_of - max(past)).days) if past else None


def mcap_per_active_address(
    market_cap: Optional[Series],
    active_addresses: Optional[Series],
) -> IndicatorValue:
    """시가총액 ÷ 활성주소 수(90일 평균) — '유저 1명당 시장가치'.

    2026-07-29 선별 검정으로 편입. 편입 이유는 하나다. **기존 지표들의 고점
    탐지력이 사이클마다 무너지고 있는데 이 지표만 무너지지 않는다.**

    네 사이클 고점에서의 4년 퍼센타일:

        Pi Cycle   +0.47 / +0.86 / -0.02 / -0.00
        Puell      +0.99 / +1.00 / +0.46 / -0.00
        2년 MA     +0.99 / +1.00 / +0.60 / +0.28
        MVRV Z     +0.94 / +0.99 / +0.69 / +0.71
        이 지표    +1.00 / +1.00 / +0.99 / +1.00

    십분위 검정에서도 최하위 십분위 이후 1년 +158%, 최상위 -4% 로 MVRV Z
    (+121% / +13%)보다 분리도가 좋았다. 기존 지표와의 최대 순위상관은
    서멀캡 +0.74 로 중복 관문(0.85)을 통과한다.

    **저점은 못 잡는다.** 사이클 저점에서 4년 퍼센타일이 +0.07 / +0.33 /
    -0.00 / +0.19 로 사실상 중립이다. 밸류에이션 계열이 max_abs 라 이 약점은
    무해하다 — 바닥에서는 더 극단인 MVRV Z 가 선택되고 이 지표는 무시된다.
    서멀캡을 편입할 때와 같은 논리다.

    **퍼센타일 전용이다.** 시가총액은 지수적으로 늘고 활성주소는 거의 평평해서
    비율에 강한 구조적 상승 추세가 있다(2015 저점 11,021 → 2026 1,939,735).
    고정 앵커는 의미가 없다.
    """
    if market_cap is None or active_addresses is None:
        return IndicatorValue("mcap_per_active", None, None, note="시총/활성주소 데이터 없음")

    smoothed = sma(active_addresses, ACTIVE_ADDR_WINDOW)
    mult = ratio(market_cap, smoothed)
    if mult.last is None:
        return IndicatorValue("mcap_per_active", None, market_cap.last_date,
                              note=f"활성주소 {ACTIVE_ADDR_WINDOW}일 평균 산출 불가")
    return IndicatorValue(
        "mcap_per_active", mult.last, market_cap.last_date,
        detail={"active_addresses_90d": smoothed.last,
                "history_4y": history_of(mult, years=4.0)},
    )


def nupl_phase(value: float) -> str:
    """원문 3.4 의 심리 단계 구분."""
    if value > 0.75:
        return "도취 (Euphoria/Greed)"
    if value > 0.5:
        return "확신 (Belief/Denial)"
    if value > 0.25:
        return "낙관 (Optimism/Anxiety)"
    if value >= 0:
        return "희망·공포 (Hope/Fear)"
    return "항복 (Capitulation)"


# ---------------------------------------------------------------------------
# 공급·채굴 계열
# ---------------------------------------------------------------------------

def puell_multiple(
    price: Series,
    issuance_btc: Optional[Series] = None,
    supply: Optional[Series] = None,
    issuance_usd: Optional[Series] = None,
) -> IndicatorValue:
    """일간 신규 발행의 달러 가치 ÷ 그 값의 365일 이동평균.

    달러 값을 직접 받았으면 그쪽을 쓴다. 데이터 제공자가 블록별 시점 가격으로
    계산한 값이라, 코인 수량에 종가를 곱하는 것보다 정확하다.

    다만 **우선순위가 곧 배타는 아니다.** 달러 시계열이 최근 몇 달치뿐이면
    365일 평균이 나오지 않는데, 그것 때문에 온전한 BTC 시계열까지 못 쓰게 되면
    있는 데이터를 버리는 셈이다. 그래서 계산이 되는 첫 후보를 쓴다.
    """
    candidates: list[Series] = []
    if issuance_usd is not None and issuance_usd.last is not None:
        candidates.append(Series(issuance_usd.dates, issuance_usd.values, "issuance_usd"))

    issuance = issuance_btc
    if issuance is None and supply is not None:
        issuance = supply.diff()          # 유통량 차분 = 그날 발행량
    if issuance is not None:
        r = issuance.map_with(price, lambda i, p: i * p if i and i > 0 else None)
        candidates.append(Series(r.dates, r.values, "issuance_usd"))

    if not candidates:
        return IndicatorValue("puell", None, price.last_date, note="발행량 데이터 없음")

    for revenue in candidates:
        avg = sma(revenue, PUELL_WINDOW)
        cur, base = revenue.last, avg.last
        if cur is None or base in (None, 0):
            continue
        return IndicatorValue(
            "puell", cur / base, price.last_date,
            detail={"issuance_usd": cur, "avg365_usd": base,
                    "history_4y": history_of(ratio(revenue, avg), years=4.0)},
        )
    return IndicatorValue("puell", None, price.last_date, note="365일 평균 산출 불가")


def hash_ribbons(hashrate: Optional[Series]) -> IndicatorValue:
    """해시레이트 30일선과 60일선의 관계를 상태로 환원한다.

    - capitulation: 30일선 < 60일선 (채굴자가 장비를 끄는 중)
    - recovery:     재돌파 후 45일 이내 (원문 4.4 가 말하는 '항복의 끝')
    - expansion:    30일선이 60일선을 8% 이상 초과
    - normal:       그 외
    """
    if hashrate is None or len(hashrate) < HASH_SLOW:
        return IndicatorValue("hash_ribbons", None, None, note="해시레이트 데이터 부족")

    fast = sma(hashrate, HASH_FAST)
    slow = sma(hashrate, HASH_SLOW)

    pairs = [
        (d, f, s)
        for d, f, s in zip(fast.dates, fast.values, slow.values)
        if f is not None and s is not None
    ]
    if not pairs:
        return IndicatorValue("hash_ribbons", None, hashrate.last_date, note="이동평균 산출 불가")

    last_date, f_last, s_last = pairs[-1]

    if f_last < s_last:
        state = "capitulation"
    else:
        # 마지막 상향 교차 시점을 뒤에서부터 찾는다
        days_since_cross: Optional[int] = None
        for i in range(len(pairs) - 1, 0, -1):
            prev_f, prev_s = pairs[i - 1][1], pairs[i - 1][2]
            cur_f, cur_s = pairs[i][1], pairs[i][2]
            if prev_f < prev_s and cur_f >= cur_s:
                days_since_cross = (last_date - pairs[i][0]).days
                break
        if days_since_cross is not None and days_since_cross <= HASH_RECOVERY_WINDOW:
            state = "recovery"
        elif s_last and f_last / s_last >= HASH_EXPANSION_RATIO:
            state = "expansion"
        else:
            state = "normal"

    return IndicatorValue(
        "hash_ribbons", state, last_date,
        detail={"ma30": f_last, "ma60": s_last, "ratio": f_last / s_last if s_last else None},
    )


# ---------------------------------------------------------------------------
# 바닥선
# ---------------------------------------------------------------------------

def ma200w_price(price: Series) -> Optional[float]:
    """200주 이동평균의 현재 가격값. 자동 계산되는 바닥선."""
    return sma(price, MA_200W).last


def realized_price_level(
    realized_cap: Optional[Series],
    supply: Optional[Series],
) -> Optional[float]:
    """실현가격 = 실현시총 ÷ 유통량. 자동 계산되는 두 번째 바닥선.

    시장 전체의 평균 취득 원가다. 사이클 저점이 이 선 대비
    0.56× (2015) → 0.69× (2018) → 0.78× (2022) → 1.09× (2026) 로 단조
    수렴해 왔고, 2026 저점에서는 바닥선 후보 중 가장 정확했다.
    가격이 이 선 아래였던 날은 16년 중 13.2% 뿐이다.

    MVRV(가격 ÷ 실현가격)가 이미 점수 지표로 있지만 중복이 아니다 —
    점수는 배수를 [-1,+1] 로 접은 것이고, 바닥선은 달러 수준 그대로다.
    LTH 실현가격이 계열(배수)과 바닥선(수준)에 따로 쓰였던 것과 같은 구조다.
    """
    if realized_cap is None or supply is None:
        return None
    rc, s = realized_cap.last, supply.last
    if rc is None or not s:
        return None
    return rc / s


def drawdown_from_ath(price: Series) -> tuple[Optional[float], Optional[int], Optional[float]]:
    """역대 최고가 대비 낙폭(%), 그 이후 경과일, 최고가.

    이동평균이 아니라 **단일 과거 극값**을 기준으로 삼는다는 점에서 가격 계열
    4종과 계산 원리가 다르다. 다만 실측 결과 MVRV Z·NUPL 과 순위상관이 +0.92 라
    점수 지표로는 쓰지 않는다(중복). 저점 프로파일 표시용이다.
    """
    peak = 0.0
    peak_on: Optional[date] = None
    last = None
    last_on: Optional[date] = None
    for d, v in zip(price.dates, price.values):
        if v is None:
            continue
        if v > peak:
            peak, peak_on = v, d
        last, last_on = v, d
    if not peak or last is None or peak_on is None or last_on is None:
        return None, None, None
    # 행 수가 아니라 달력 일수로 센다. 결측이 있거나 주간 데이터가 섞이면
    # 행 수와 일수가 갈라지는데, 이 값의 임계(저점 프로파일)는 '일' 단위다.
    return (last / peak - 1.0) * 100.0, (last_on - peak_on).days, peak


def bottom_profile_inputs(
    data: MarketData,
    computed: Optional[dict[str, "IndicatorValue"]] = None,
) -> dict[str, Optional[float]]:
    """저점 프로파일 조건 평가에 필요한 원시값들.

    사이클 저점의 공통점을 재는 값이다. **점수에는 들어가지 않는다.**
    이유는 docs/09-저점-프로파일.md 에 적어 뒀다.

    ``computed`` 로 이미 계산된 지표를 넘기면 재계산하지 않는다. 호출부가
    보통 compute_all 을 이미 돌린 뒤라, 안 넘기면 확장창 표준편차까지 통째로
    두 번 계산하게 된다(16년 데이터에서 평가 시간이 두 배가 됐다).
    """
    d = data.normalized()
    p = d.price
    dd, since, _ = drawdown_from_ath(p)

    mvrv = None
    if d.market_cap is not None and d.realized_cap is not None:
        mc, rc = d.market_cap.last, d.realized_cap.last
        if mc and rc:
            mvrv = mc / rc

    computed = computed if computed is not None else compute_all(data)
    return {
        "drawdown_pct": dd,
        "days_since_ath": float(since) if since is not None else None,
        "mvrv": mvrv,
        "nupl": computed["nupl"].value,
        "puell": computed["puell"].value,
        "ma200w_mult": computed["ma200w_mult"].value,
    }


def _percentile_now(iv: IndicatorValue) -> Optional[float]:
    """지표의 현재값이 자기 4년 이력 안에서 갖는 위치 [-1, +1]."""
    from .normalize import percentile_rank      # 지역 import: 순환 방지

    hist = iv.detail.get("history_4y")
    if iv.value is None or not isinstance(iv.value, (int, float)) or not hist or len(hist) < 90:
        return None
    return percentile_rank(float(iv.value), hist)


def top_profile_inputs(
    data: MarketData,
    computed: Optional[dict[str, "IndicatorValue"]] = None,
    as_of: Optional[date] = None,
) -> dict[str, Optional[float]]:
    """사이클 고점 프로파일 조건에 필요한 값들.

    저점 프로파일과 같은 취급이다 — **점수에 들어가지 않는다.**

    조건이 전부 퍼센타일이거나 달력인 데는 이유가 있다. 레벨 기반 조건은
    사이클마다 붕괴한다. 네 고점에서의 관측값을 보면 명확하다.

        2년 MA 배수   17.18 → 9.63 → 2.48 → 1.63
        Puell         9.14 → 6.62 → 1.55 → 1.07

    2013 년 기준으로 임계를 잡으면 최근 두 고점을 통째로 놓친다. 반면
    퍼센타일 형태는 네 고점에서 같은 값을 낸다.
    """
    d = data.normalized()
    computed = computed if computed is not None else compute_all(d)
    ref = as_of or d.price.last_date

    peak = 0.0
    peak_on: Optional[date] = None
    for dd, v in zip(d.price.dates, d.price.values):
        if v is not None and v > peak:
            peak, peak_on = v, dd

    return {
        "mcap_per_active_pct": _percentile_now(
            mcap_per_active_address(d.market_cap, d.active_addresses)
        ),
        "thermocap_pct": _percentile_now(computed["thermocap"]),
        "mvrv_z_pct": _percentile_now(computed["mvrv_z"]),
        "days_since_halving": days_since_halving(ref) if ref else None,
        "days_since_ath": (
            float((d.price.last_date - peak_on).days)
            if peak_on and d.price.last_date else None
        ),
    }


def compute_all(data: MarketData) -> dict[str, IndicatorValue]:
    """자동 계산 가능한 지표를 한 번에 산출한다."""
    d = data.normalized()
    p = d.price
    return {
        "pi_cycle": pi_cycle_ratio(p),
        "grm": golden_ratio_multiple(p),
        "ma200w_mult": ma200w_multiple(p),
        "ma2y_mult": investor_tool_multiple(p),
        "mvrv_z": mvrv_zscore(d.market_cap, d.realized_cap),
        "nupl": nupl(d.market_cap, d.realized_cap),
        "thermocap": thermocap_multiple(d.market_cap, d.issuance_usd, d.issuance_btc, p),
        "mcap_per_active": mcap_per_active_address(d.market_cap, d.active_addresses),
        "puell": puell_multiple(p, d.issuance_btc, d.supply, d.issuance_usd),
        "hash_ribbons": hash_ribbons(d.hashrate),
    }
