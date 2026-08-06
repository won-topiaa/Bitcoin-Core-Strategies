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
import math
from datetime import date
from pathlib import Path
from typing import Optional

from ..indicators import MarketData
from ..series import Series
from .base import DataBundle, FetchError, coverage_warnings, optional_series

COLUMNS = ("price", "market_cap", "realized_cap", "issuance_btc", "issuance_usd",
           "supply", "hashrate", "active_addresses",
           "exchange_supply", "exchange_inflow", "exchange_outflow")


def load_csv_bundle(path: str | Path) -> DataBundle:
    p = Path(path)
    if not p.exists():
        raise FetchError(f"CSV 파일이 없습니다: {p}")

    buckets: dict[str, list[tuple[date, Optional[float]]]] = {c: [] for c in COLUMNS}
    # 숫자로 읽히지 않은 칸. 조용히 결측으로 넘기면 천 단위 쉼표 하나가
    # 시계열 전체를 소리 없이 갉아먹는다.
    bad: dict[str, list[str]] = {}
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
                if col not in row:
                    continue
                cell = row[col]
                v = _as_float(cell)
                if v is None and str(cell or "").strip():
                    bad.setdefault(col, []).append(f"{row_no}행 {str(cell).strip()!r}")
                buckets[col].append((d, v))

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
        active_addresses=optional_series(buckets["active_addresses"], "active_addresses"),
        exchange_supply=optional_series(buckets["exchange_supply"], "exchange_supply"),
        exchange_inflow=optional_series(buckets["exchange_inflow"], "exchange_inflow"),
        exchange_outflow=optional_series(buckets["exchange_outflow"], "exchange_outflow"),
    )
    warns = list(coverage_warnings(market))
    if unknown:
        warns.append(f"알 수 없는 열 무시됨: {sorted(unknown)}")
    for col, samples in sorted(bad.items()):
        warns.append(
            f"{col}: 숫자로 읽히지 않은 칸 {len(samples)}개를 결측 처리했습니다 "
            f"(예: {', '.join(samples[:3])})"
        )
    return DataBundle(market=market, origin=f"CSV {p}", warnings=tuple(warns))


def bundle_cells(bundle: DataBundle) -> dict[date, dict[str, float]]:
    """번들을 ``date → {열: 값}`` 로 눕힌다.

    CSV 쓰기와 증분 병합(``tools/refresh_data.py``)이 **같은 표현**을 쓰게 하려고
    따로 뺐다. 두 곳이 각자 열 목록을 엮으면 열을 하나 추가할 때 한쪽만 고쳐지고,
    그러면 새 열이 저장은 되는데 갱신은 안 되는 상태가 된다.
    """
    m = bundle.market
    lookups = {
        "price": dict(zip(m.price.dates, m.price.values)),
        "market_cap": _lookup(m.market_cap),
        "realized_cap": _lookup(m.realized_cap),
        "issuance_btc": _lookup(m.issuance_btc),
        "issuance_usd": _lookup(m.issuance_usd),
        "supply": _lookup(m.supply),
        "hashrate": _lookup(m.hashrate),
        "active_addresses": _lookup(m.active_addresses),
        "exchange_supply": _lookup(m.exchange_supply),
        "exchange_inflow": _lookup(m.exchange_inflow),
        "exchange_outflow": _lookup(m.exchange_outflow),
    }
    missing = set(COLUMNS) - set(lookups)
    if missing:                                  # 열을 추가하고 여기를 잊었을 때
        raise FetchError(f"CSV 열을 채우는 규칙이 없습니다: {sorted(missing)}")

    out: dict[date, dict[str, float]] = {}
    for col, table in lookups.items():
        for d, v in table.items():
            if v is not None:
                out.setdefault(d, {})[col] = float(v)
    return out


def save_csv_bundle(bundle: DataBundle, path: str | Path) -> Path:
    """받아온 시계열을 CSV로 떨궈 둔다. 다음부터는 오프라인으로 돌릴 수 있다."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cells = bundle_cells(bundle)

    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("date",) + COLUMNS)
        for d in sorted(cells):
            row = cells[d]
            writer.writerow([d.isoformat()] + [_fmt(row.get(c)) for c in COLUMNS])
    return p


def write_cells(cells: dict[date, dict[str, float]], path: str | Path) -> Path:
    """``date → {열: 값}`` 를 CSV 로 쓴다. 병합 결과를 저장할 때 쓴다."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("date",) + COLUMNS)
        for d in sorted(cells):
            row = cells[d]
            writer.writerow([d.isoformat()] + [_fmt(row.get(c)) for c in COLUMNS])
    return p


def read_cells(path: str | Path, *,
               out: Optional[dict] = None) -> dict[date, dict[str, float]]:
    """CSV 를 ``date → {열: 값}`` 로 읽는다. 빈 칸은 키 자체가 없다.

    ``load_csv_bundle`` 과 달리 Series 로 조립하지 않는다. 증분 병합은 **칸 단위**로
    해야 하기 때문이다 — 시계열로 바꿔 놓고 합치면 "이 날 이 열이 원래 비어 있었는지"
    가 사라진다.

    ``out`` 을 주면 ``out["unknown"]`` 에 모르는 열 이름을, ``out["unparsed"]`` 에
    숫자로 읽히지 않은 칸을 담는다. **둘 다 무시하면 안 된다.** 이 함수로 읽고
    ``write_cells`` 로 쓰면 그것들이 조용히 사라진다 — 모르는 열은 열째로,
    ``"60,000.0"`` 같은 칸은 빈 칸으로. 손으로 고쳐 둔 것이 갱신 한 번에 날아간다.
    """
    p = Path(path)
    if out is not None:
        out["unknown"] = []
        out["unparsed"] = []
    if not p.exists():
        return {}
    result: dict[date, dict[str, float]] = {}
    with p.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "date" not in reader.fieldnames:
            raise FetchError(f"{p}: 첫 열은 'date' 여야 합니다. 현재 헤더: {reader.fieldnames}")
        if out is not None:
            out["unknown"] = sorted(set(reader.fieldnames) - {"date"} - set(COLUMNS))
        for row_no, row in enumerate(reader, start=2):
            raw = (row.get("date") or "").strip()
            if not raw:
                continue
            try:
                d = date.fromisoformat(raw)
            except ValueError as exc:
                raise FetchError(f"{p}:{row_no} 날짜 형식 오류 {raw!r} (YYYY-MM-DD)") from exc
            cells = {}
            for col in COLUMNS:
                raw_cell = row.get(col)
                v = _as_float(raw_cell)
                if v is not None:
                    cells[col] = v
                elif out is not None and str(raw_cell or "").strip():
                    out["unparsed"].append(f"{row_no}행 {col}={str(raw_cell).strip()!r}")
            result[d] = cells
    return result


def _lookup(s: Optional[Series]) -> dict:
    return dict(zip(s.dates, s.values)) if s is not None else {}


def _fmt(v: Optional[float]) -> str:
    # NaN·inf 는 절대 파일에 쓰지 않는다. 한 번 들어가면 다음 실행부터
    # read_cells 의 '숫자로 안 읽히는 칸' 검사에 걸려 갱신이 계속 실패하고,
    # CSV 는 캐시로 되살아나므로 사람이 손으로 고칠 때까지 파이프라인이
    # 멈춘다. 빈 칸(결측)으로 떨어뜨리는 편이 회복 가능하다.
    if v is None:
        return ""
    f = float(v)
    return repr(f) if math.isfinite(f) else ""


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    # inf/nan 은 결측으로 취급한다 — 그대로 흘리면 점수·payload 로 번져 json.dumps
    # 가 무효 토큰(NaN/Infinity)을 써서 브라우저 JSON.parse 가 죽고 사이트가 백지가 된다.
    return f if math.isfinite(f) else None
