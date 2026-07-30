"""CoinMetrics 커뮤니티 API 어댑터.

API 키 없이 자동 계산 지표 8개를 전부 채운다.

    https://docs.coinmetrics.io/api/v4

**실현시총은 직접 주지 않는다.** 커뮤니티 티어에서 ``CapRealUSD`` 를 요청하면
403이 돌아온다. 대신 ``CapMVRVCur`` (= 시가총액 ÷ 실현시총) 가 2010-07-18부터
전 구간 열려 있어서 여기서 역산한다.

    실현시총 = 시가총액 ÷ MVRV

나눗셈 한 번이라 정보 손실이 없다. MVRV 자체가 CoinMetrics가 산출한 값이라
실현시총을 직접 받는 것과 결과가 같다.

발행량도 ``IssTotUSD`` 로 달러 값을 직접 받는다. 코인 수량에 종가를 곱하는
것보다 정확하다.

주의: 커뮤니티 티어는 요청 빈도 제한이 있고 최신 데이터가 하루 정도 늦다.
사이클 타이밍용으로는 문제가 되지 않는다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Iterable, Mapping, Optional

from ..indicators import MarketData
from ..series import Series
from .base import DataBundle, FetchError, coverage_warnings, optional_series

BASE_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"

# 커뮤니티 티어에서 실제로 열려 있는 지표만 요청한다.
# 하나라도 권한이 없으면 요청 전체가 403으로 죽으므로 목록을 넓히지 말 것.
# (확인 방법: /v4/catalog-v2/asset-metrics?assets=btc)
METRICS: Mapping[str, str] = {
    "PriceUSD": "price",
    "CapMrktCurUSD": "market_cap",
    "CapMVRVCur": "mvrv",              # 실현시총을 역산하는 데 쓴다
    "IssTotUSD": "issuance_usd",
    "IssTotNtv": "issuance_btc",
    "SplyCur": "supply",
    "HashRate": "hashrate",
    "AdrActCnt": "active_addresses",
    "SplyExNtv": "exchange_supply",
    "FlowInExNtv": "exchange_inflow",
    "FlowOutExNtv": "exchange_outflow",
}

PAGE_SIZE = 10000
# 서멀캡의 분모는 **창세 이래** 누적 채굴수익이라, 창을 자르면 배수가 부풀려진다
# (8년 창이면 +10%, 4년 창이면 +84%). 200주 이동평균만 보면 5년이면 충분하지만
# 서멀캡 때문에 전 구간을 받는다. API 가 2010-07-18 이전 데이터를 갖고 있지 않아
# 이 값을 더 키워도 응답 크기는 늘지 않는다.
DEFAULT_YEARS = 20


def fetch(
    *,
    years: float = DEFAULT_YEARS,
    start: Optional[date] = None,
    end: Optional[date] = None,
    timeout: int = 60,
    metrics: Optional[Iterable[str]] = None,
) -> DataBundle:
    """CoinMetrics 에서 시계열을 받아 MarketData 로 조립한다.

    ``start`` 를 주면 ``years`` 는 무시한다. 증분 갱신은 "최근 N일"만 받으면
    되는데, 그걸 ``years`` 로 표현하려면 호출부에서 365.25 를 나누게 된다 —
    날짜를 날짜로 넘기는 게 맞다.
    """
    end = end or date.today()
    if start is None:
        start = end - timedelta(days=int(years * 365.25) + 60)
    if start > end:
        raise FetchError(f"시작일이 종료일보다 늦습니다: {start} > {end}")
    wanted = list(metrics) if metrics else list(METRICS)

    rows = _fetch_all_pages(wanted, start, end, timeout)
    if not rows:
        raise FetchError("CoinMetrics 응답이 비어 있습니다. 기간·지표명을 확인하세요.")

    buckets: dict[str, list[tuple[date, Optional[float]]]] = {v: [] for v in METRICS.values()}
    for row in rows:
        try:
            d = date.fromisoformat(row["time"][:10])
        except (KeyError, ValueError):
            continue
        for api_name, our_name in METRICS.items():
            if api_name in row:
                buckets[our_name].append((d, _as_float(row[api_name])))

    if not buckets["price"]:
        raise FetchError("가격(PriceUSD) 데이터를 받지 못했습니다.")

    market = MarketData(
        price=Series.from_pairs(buckets["price"], name="price"),
        market_cap=optional_series(buckets["market_cap"], "market_cap"),
        realized_cap=derive_realized_cap(buckets["market_cap"], buckets["mvrv"]),
        issuance_btc=optional_series(buckets["issuance_btc"], "issuance_btc"),
        issuance_usd=optional_series(buckets["issuance_usd"], "issuance_usd"),
        supply=optional_series(buckets["supply"], "supply"),
        active_addresses=optional_series(buckets["active_addresses"], "active_addresses"),
        hashrate=optional_series(buckets["hashrate"], "hashrate"),
        exchange_supply=optional_series(buckets["exchange_supply"], "exchange_supply"),
        exchange_inflow=optional_series(buckets["exchange_inflow"], "exchange_inflow"),
        exchange_outflow=optional_series(buckets["exchange_outflow"], "exchange_outflow"),
    )
    return DataBundle(
        market=market,
        origin=f"CoinMetrics community API ({start} ~ {end})",
        warnings=coverage_warnings(market),
    )


def derive_realized_cap(
    market_cap: list[tuple[date, Optional[float]]],
    mvrv: list[tuple[date, Optional[float]]],
) -> Optional[Series]:
    """실현시총 = 시가총액 ÷ MVRV.

    커뮤니티 티어는 CapRealUSD 를 주지 않지만 CapMVRVCur 는 준다.
    MVRV 의 정의가 곧 시총/실현시총이므로 나눗셈 한 번으로 되돌릴 수 있다.
    """
    ratios = {d: v for d, v in mvrv if v not in (None, 0)}
    pairs = [
        (d, cap / ratios[d])
        for d, cap in market_cap
        if cap is not None and d in ratios
    ]
    return optional_series(pairs, "realized_cap")


def _fetch_all_pages(metrics: list[str], start: date, end: date, timeout: int) -> list[dict]:
    params = {
        "assets": "btc",
        "metrics": ",".join(metrics),
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "frequency": "1d",
        "page_size": str(PAGE_SIZE),
    }
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    rows: list[dict] = []
    seen_urls: set[str] = set()

    while url and url not in seen_urls:
        seen_urls.add(url)
        payload = _get_json(url, timeout)
        rows.extend(payload.get("data", []))
        url = payload.get("next_page_url", "")
        if len(seen_urls) > 50:     # 방어적 상한
            break
    return rows


def _get_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "btc-core/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise FetchError(f"CoinMetrics HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(
            f"CoinMetrics 접속 실패: {exc.reason}. "
            "네트워크가 막힌 환경이라면 --csv 로 로컬 파일을 쓰세요."
        ) from exc
    except json.JSONDecodeError as exc:
        raise FetchError(f"CoinMetrics 응답을 JSON으로 읽지 못했습니다: {exc}") from exc


def _as_float(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
