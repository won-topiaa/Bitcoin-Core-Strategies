"""CSV 어댑터.

네트워크가 막힌 환경, 또는 받아둔 데이터로 과거 시점을 재현할 때 쓴다.
``btc-core fetch --save data/market.csv`` 로 저장한 파일을 그대로 읽는다.

형식 — 헤더 필수, 첫 열은 date(YYYY-MM-DD):

    date,price,market_cap,realized_cap,issuance_btc,issuance_usd,supply,hashrate
    2020-01-01,7200.17,130800000000,90100000000,1800.0,12960306,18100000,1.1e20

price 외의 열은 없어도 된다. 빈 칸은 결측으로 처리한다.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Optional

from ..indicators import MarketData
from ..series import Series
from .base import DataBundle, FetchError, coverage_warnings, optional_series

COLUMNS = ("price", "market_cap", "realized_cap", "issuance_btc", "issuance_usd",
           "supply", "hashrate")


def load_csv_bundle(path: str | Path) -> DataBundle:
    p = Path(path)
    if not p.exists():
        raise FetchError(f"CSV 파일이 없습니다: {p}")

    buckets: dict[str, list[tuple[date, Optional[float]]]] = {c: [] for c in COLUMNS}
    with p.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "date" not in reader.fieldnames:
            raise FetchError(f"{p}: 첫 열은 'date' 여야 합니다. 현재 헤더: {reader.fieldnames}")
        unknown = set(reader.fieldnames) - {"date"} - set(COLUMNS)
        for row_no, row in enumerate(reader, start=2):
            raw_date = (row.get("date") or "").strip()
            if not raw_date:
                continue
            try:
                d = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise FetchError(f"{p}:{row_no} 날짜 형식 오류 {raw_date!r} (YYYY-MM-DD)") from exc
            for col in COLUMNS:
                if col in row:
                    buckets[col].append((d, _as_float(row[col])))

    if not [v for _, v in buckets["price"] if v is not None]:
        raise FetchError(f"{p}: price 열에 유효한 값이 없습니다.")

    market = MarketData(
        price=Series.from_pairs([(d, v) for d, v in buckets["price"] if v is not None], name="price"),
        market_cap=optional_series(buckets["market_cap"], "market_cap"),
        realized_cap=optional_series(buckets["realized_cap"], "realized_cap"),
        issuance_btc=optional_series(buckets["issuance_btc"], "issuance_btc"),
        issuance_usd=optional_series(buckets["issuance_usd"], "issuance_usd"),
        supply=optional_series(buckets["supply"], "supply"),
        hashrate=optional_series(buckets["hashrate"], "hashrate"),
    )
    warns = list(coverage_warnings(market))
    if unknown:
        warns.append(f"알 수 없는 열 무시됨: {sorted(unknown)}")
    return DataBundle(market=market, origin=f"CSV {p}", warnings=tuple(warns))


def save_csv_bundle(bundle: DataBundle, path: str | Path) -> Path:
    """받아온 시계열을 CSV로 떨궈 둔다. 다음부터는 오프라인으로 돌릴 수 있다."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    m = bundle.market

    lookups = {
        "price": dict(zip(m.price.dates, m.price.values)),
        "market_cap": _lookup(m.market_cap),
        "realized_cap": _lookup(m.realized_cap),
        "issuance_btc": _lookup(m.issuance_btc),
        "issuance_usd": _lookup(m.issuance_usd),
        "supply": _lookup(m.supply),
        "hashrate": _lookup(m.hashrate),
    }
    all_dates = sorted(set().union(*[set(l) for l in lookups.values() if l]))

    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("date",) + COLUMNS)
        for d in all_dates:
            writer.writerow(
                [d.isoformat()] + [_fmt(lookups[c].get(d)) for c in COLUMNS]
            )
    return p


def _lookup(s: Optional[Series]) -> dict:
    return dict(zip(s.dates, s.values)) if s is not None else {}


def _fmt(v: Optional[float]) -> str:
    return "" if v is None else repr(float(v))


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None
