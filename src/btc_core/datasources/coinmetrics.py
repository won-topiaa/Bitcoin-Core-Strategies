"""CoinMetrics 커뮤니티 API 어댑터.

API 키 없이 쓸 수 있고, 밸류에이션 계열에 필요한 실현시총(CapRealUSD)을
무료로 주는 거의 유일한 소스다. 여기 하나로 자동 계산 지표 8개 중 8개가
전부 채워진다.

    https://docs.coinmetrics.io/api/v4

주의: 커뮤니티 티어는 요청 빈도 제한이 있고, 최신 데이터가 하루 정도
늦게 들어온다. 사이클 타이밍용으로는 아무 문제가 없다.
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

# 필요한 지표와 우리 쪽 이름의 대응
METRICS: Mapping[str, str] = {
    "PriceUSD": "price",
    "CapMrktCurUSD": "market_cap",
    "CapRealUSD": "realized_cap",
    "IssContNtv": "issuance_btc",
    "SplyCur": "supply",
    "HashRate": "hashrate",
}

PAGE_SIZE = 10000
DEFAULT_YEARS = 8       # 200주(≈3.8년) 이동평균 + 여유


def fetch(
    *,
    years: float = DEFAULT_YEARS,
    end: Optional[date] = None,
    timeout: int = 60,
    metrics: Optional[Iterable[str]] = None,
) -> DataBundle:
    """CoinMetrics 에서 시계열을 받아 MarketData 로 조립한다."""
    end = end or date.today()
    start = end - timedelta(days=int(years * 365.25) + 60)
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
        realized_cap=optional_series(buckets["realized_cap"], "realized_cap"),
        issuance_btc=optional_series(buckets["issuance_btc"], "issuance_btc"),
        supply=optional_series(buckets["supply"], "supply"),
        hashrate=optional_series(buckets["hashrate"], "hashrate"),
    )
    return DataBundle(
        market=market,
        origin=f"CoinMetrics community API ({start} ~ {end})",
        warnings=coverage_warnings(market),
    )


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
