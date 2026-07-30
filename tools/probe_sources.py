#!/usr/bin/env python3
"""무료 데이터 경로 진단 — 어디서 무엇을 받을 수 있는지 실제로 찔러본다.

    python3 tools/probe_sources.py
    python3 tools/probe_sources.py --series realized_cap
    python3 tools/probe_sources.py --timeout 20 --verbose

이 시스템은 **무료 데이터만으로** 돌아가야 한다. 그러면 위험의 크기는 곧
"외부에서 반드시 받아야 하는 시계열이 몇 개이고, 각각 대안이 몇 개인가"다.
소스가 죽거나 유료화되는 것은 예외가 아니라 예정된 일이다.

이 도구는 그 지도를 **말이 아니라 응답 코드로** 확인한다. 네트워크 정책은
환경마다 다르므로(회사 프록시, 국가 차단, 클라우드 IP 거부) 배포할 곳에서
직접 돌려보는 것이 유일한 확인 방법이다.

## 파생으로 대체되는 것은 여기서 찾지 않는다

    유통량·발행량·시가총액·발행액  →  btc_core.datasources.derive (API 불필요)

그래서 **진짜 외부 의존은 price 와 realized_cap 둘뿐이다.** 나머지
(hashrate, active_addresses)는 없으면 해당 지표 하나가 결측될 뿐이다.

종료 코드: price 와 realized_cap 을 모두 받을 수 있으면 0, 아니면 1.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

UA = "btc-core/1.0 (+source probe)"

# 우리가 계산에 쓰는 시계열 이름
SERIES = ("price", "realized_cap", "hashrate", "active_addresses",
          "supply", "market_cap", "issuance")

# 파생으로 대체 가능한 것들 — 소스가 없어도 시스템이 멈추지 않는다
DERIVABLE = {"supply", "market_cap", "issuance"}

# 없으면 지표 하나가 결측될 뿐인 것들
OPTIONAL = {"hashrate", "active_addresses"}

# 반드시 외부에서 받아야 하는 것들
REQUIRED = {"price", "realized_cap"}


@dataclass(frozen=True)
class Source:
    """무료로 받을 수 있는 후보 경로 하나."""

    name: str
    url: str                       # 실제로 찔러볼 최소 질의
    provides: tuple[str, ...]
    history_from: str              # 이 소스가 커버하는 시작 시점
    limits: str                    # 무료 티어의 제약
    note: str = ""
    needs_key: bool = False
    # 응답이 200 이어도 내용이 비어 있으면 쓸 수 없다. 이 키가 있는지 본다.
    expect_key: Optional[str] = None


SOURCES: tuple[Source, ...] = (
    # --- price + 거의 전부 ---------------------------------------------------
    Source(
        name="CoinMetrics community",
        url=("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
             "?assets=btc&metrics=PriceUSD,CapMVRVCur&frequency=1d&page_size=2"),
        provides=("price", "realized_cap", "hashrate", "active_addresses",
                  "supply", "market_cap", "issuance"),
        history_from="2010-07-18",
        limits="키 없음, 초당 요청 제한 있음, 최신 데이터 하루 지연",
        note="CapRealUSD 는 403. CapMVRVCur 로 실현시총을 역산한다(나눗셈 한 번).",
        expect_key="data",
    ),
    Source(
        name="blockchain.info charts",
        url="https://api.blockchain.info/charts/market-price?timespan=7days&format=json",
        provides=("price", "hashrate", "active_addresses", "supply", "issuance"),
        history_from="2009-01-03",
        limits="키 없음. 차트별 개별 요청. 이력이 가장 길다.",
        note="market-price / hash-rate / n-unique-addresses / total-bitcoins / "
             "miners-revenue. 실현시총만 없다.",
        expect_key="values",
    ),

    # --- realized_cap 대안 — 유일한 계산 불가 시계열 -------------------------
    Source(
        name="BGeometrics bitcoin-data.com",
        url="https://bitcoin-data.com/v1/mvrv/last",
        provides=("realized_cap",),
        history_from="2010-10-01",
        limits="키 없음(무료), 지표별 엔드포인트",
        note="/v1/mvrv, /v1/realized-price, /v1/nupl 등. 실현시총 2차 경로로 "
             "가장 값지다 — CoinMetrics 가 막히면 여기가 유일하다.",
    ),

    # --- price 대안 (거래소 원본) -------------------------------------------
    Source(
        name="Bitstamp OHLC",
        url=("https://www.bitstamp.net/api/v2/ohlc/btcusd/"
             "?step=86400&limit=3"),
        provides=("price",),
        history_from="2011-08-18",
        limits="키 없음. 요청당 최대 1,000봉.",
        note="거래소 중 이력이 가장 길다. 사이클 4개를 전부 덮는 유일한 거래소.",
        expect_key="data",
    ),
    Source(
        name="Coinbase Exchange candles",
        url=("https://api.exchange.coinbase.com/products/BTC-USD/candles"
             "?granularity=86400"),
        provides=("price",),
        history_from="2015-07-20",
        limits="키 없음. 요청당 300봉 — 16년치면 20회 페이징.",
    ),
    Source(
        name="Binance klines",
        url="https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=3",
        provides=("price",),
        history_from="2017-08-17",
        limits="키 없음, 요청당 1,000봉. 일부 지역에서 차단.",
        note="USDT 표시라 달러와 미세하게 다르고 2017 이전이 없다. 3순위.",
    ),
    Source(
        name="Kraken OHLC",
        url="https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440",
        provides=("price",),
        history_from="요청 시점에서 720봉",
        limits="키 없음이지만 **일봉을 720개만** 준다 — 이력용으로 부적합",
        note="현재가 확인용으로만.",
    ),
    Source(
        name="CoinGecko free",
        url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
        provides=("price", "market_cap", "supply"),
        history_from="2013-04-28",
        limits="무료 티어는 **일별 이력을 365일까지만** 준다 (2024년 정책 변경)",
        note="현재가는 좋지만 16년 이력을 못 받아 이 시스템에는 부적합.",
    ),

    # --- 보조 지표 ----------------------------------------------------------
    Source(
        name="mempool.space",
        url="https://mempool.space/api/v1/mining/hashrate/1y",
        provides=("hashrate",),
        history_from="2009-01-03",
        limits="키 없음. 해시레이트·난이도.",
        note="Hash Ribbons 전용 대안.",
    ),
    Source(
        name="Blockchair stats",
        url="https://api.blockchair.com/bitcoin/stats",
        provides=("hashrate", "supply"),
        history_from="현재 스냅샷만",
        limits="무료는 하루 1,440 요청. **이력이 아니라 현재값만.**",
        note="이력이 없어 이 시스템에는 보조용.",
    ),

    # --- LRS 거시 축 --------------------------------------------------------
    Source(
        name="FRED (거시)",
        url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10",
        provides=(),
        history_from="1962-",
        limits="CSV 는 키 없이 열린다. API 는 키 필요.",
        note="LRS 용 — 실질금리(DFII10), 달러지수(DTWEXBGS), M2(WM2NS). "
             "LRS 는 BCS 를 바꾸지 않고 실행 크기만 0.75~1.25배로 조절한다.",
    ),
    Source(
        name="Stooq (달러지수)",
        url="https://stooq.com/q/d/l/?s=dx.f&i=d",
        provides=(),
        history_from="2000-",
        limits="키 없음, CSV.",
        note="LRS 보조. FRED 가 막힐 때의 달러지수 대안.",
    ),
)


@dataclass
class Probe:
    source: Source
    status: str = ""
    detail: str = ""
    ok: bool = False
    body_head: str = field(default="", repr=False)


def probe(src: Source, timeout: int) -> Probe:
    req = urllib.request.Request(
        src.url, headers={"Accept": "application/json, text/csv, */*", "User-Agent": UA}
    )
    out = Probe(source=src)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4096)
            out.status = str(resp.status)
            out.body_head = raw[:300].decode("utf-8", "replace")
            out.ok = 200 <= resp.status < 300
            if out.ok and src.expect_key:
                try:
                    if src.expect_key not in json.loads(raw.decode("utf-8", "replace")):
                        out.ok = False
                        out.detail = f"'{src.expect_key}' 키 없음 — 응답 형식이 바뀜"
                except json.JSONDecodeError:
                    # 페이징 응답을 4KB 만 읽으면 JSON 이 잘린다. 상태코드를 믿는다.
                    out.detail = "응답이 잘려 형식 확인 생략"
            if out.ok and not out.detail:
                out.detail = f"{len(raw)}B 수신"
    except urllib.error.HTTPError as exc:
        out.status = str(exc.code)
        out.detail = _http_hint(exc.code)
    except urllib.error.URLError as exc:
        out.status = "차단"
        out.detail = f"{exc.reason}"
    except (TimeoutError, ssl.SSLError, OSError) as exc:
        out.status = "차단"
        out.detail = f"{type(exc).__name__}: {exc}"
    return out


def _http_hint(code: int) -> str:
    return {
        401: "인증 필요 — 무료 티어에서 빠졌을 수 있다",
        403: "거부 — 프록시 정책 또는 유료 전용 지표",
        404: "엔드포인트 이동 — URL 갱신 필요",
        405: "메서드 거부 — 프록시가 가로챘을 가능성",
        407: "프록시 인증 필요",
        429: "요청 한도 — 잠시 후 재시도 (경로 자체는 살아 있다)",
        451: "법적 차단 — 지역 제한",
        502: "게이트웨이 오류 — 일시적일 수 있다",
        503: "서비스 불가 — 일시적일 수 있다",
    }.get(code, "HTTP 오류")


def main() -> int:
    ap = argparse.ArgumentParser(description="무료 데이터 경로 진단")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--series", default=None,
                    help="이 시계열을 주는 소스만 찔러본다")
    ap.add_argument("--verbose", action="store_true", help="응답 앞부분도 출력")
    args = ap.parse_args()

    targets = [s for s in SOURCES
               if not args.series or args.series in s.provides]
    if not targets:
        print(f"'{args.series}' 를 주는 후보가 없습니다. "
              f"파생 가능 여부를 확인하세요: {sorted(DERIVABLE)}")
        return 1

    print("무료 데이터 경로 진단")
    print("=" * 78)
    results = [probe(s, args.timeout) for s in targets]

    print(f"\n{'소스':28s} {'상태':>6s}  {'제공':38s}")
    print("-" * 78)
    for r in results:
        mark = "O" if r.ok else "X"
        provides = ",".join(r.source.provides) or "(거시 보조)"
        print(f"{mark} {r.source.name:26s} {r.status:>6s}  {provides[:38]}")
        print(f"{'':28s} {'':>6s}  {r.detail}")
        if args.verbose and r.body_head:
            print(f"{'':30s}{r.body_head[:160]!r}")

    # --- 시계열별로 살아 있는 경로가 있는지 --------------------------------
    print("\n시계열별 확보 상황")
    print("-" * 78)
    live: dict[str, list[str]] = {k: [] for k in SERIES}
    for r in results:
        if r.ok:
            for s in r.source.provides:
                live[s].append(r.source.name)

    missing_required = []
    for s in SERIES:
        if s in DERIVABLE:
            tag = "파생 가능"
        elif s in OPTIONAL:
            tag = "없으면 지표 1개 결측"
        else:
            tag = "필수"
        paths = live[s]
        state = f"{len(paths)}개 경로" if paths else "경로 없음"
        print(f"  {s:18s} {tag:20s} {state:10s} {', '.join(paths[:3])}")
        if s in REQUIRED and not paths:
            missing_required.append(s)

    print("\n" + "=" * 78)
    if missing_required:
        print(f"필수 시계열에 경로가 없습니다: {', '.join(missing_required)}")
        if "realized_cap" in missing_required:
            print("  실현시총은 계산으로 대체할 수 없습니다. 이것이 없으면 "
                  "MVRV Z / NUPL 이 빠지고 밸류에이션 계열에 서멀캡만 남습니다.")
        if "price" in missing_required:
            print("  가격이 없으면 아무것도 계산되지 않습니다. "
                  "data/market.csv 같은 로컬 파일로 돌리세요.")
        print("\n  로컬 파일 경로: python3 -m btc_core.cli report --csv data/market.csv")
        return 1

    print("필수 시계열(price, realized_cap) 경로 확보. "
          f"파생으로 채워지는 것: {', '.join(sorted(DERIVABLE))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
