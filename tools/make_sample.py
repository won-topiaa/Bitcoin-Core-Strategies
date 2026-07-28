#!/usr/bin/env python3
"""data/sample_synthetic.csv 를 다시 만든다.

합성 데이터다. 실제 비트코인 시세가 아니다.
설치 직후 네트워크 없이 `btc-core score` 를 한 번 돌려보기 위한 것이고,
그 이상의 의미는 없다.

    python3 tools/make_sample.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import fixtures  # noqa: E402

OUT = ROOT / "data" / "sample_synthetic.csv"
COLUMNS = ("price", "market_cap", "realized_cap", "issuance_btc", "supply", "hashrate")
DIGITS = {"price": 2, "market_cap": 0, "realized_cap": 0,
          "issuance_btc": 2, "supply": 0, "hashrate": 0}


def main() -> int:
    md = fixtures.market_data()
    lookups = {
        "price": dict(zip(md.price.dates, md.price.values)),
        "market_cap": dict(zip(md.market_cap.dates, md.market_cap.values)),
        "realized_cap": dict(zip(md.realized_cap.dates, md.realized_cap.values)),
        "issuance_btc": dict(zip(md.issuance_btc.dates, md.issuance_btc.values)),
        "supply": dict(zip(md.supply.dates, md.supply.values)),
        "hashrate": dict(zip(md.hashrate.dates, md.hashrate.values)),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(("date",) + COLUMNS)
        for d in md.price.dates:
            row = [d.isoformat()]
            for c in COLUMNS:
                v = lookups[c].get(d)
                row.append("" if v is None else f"{v:.{DIGITS[c]}f}")
            w.writerow(row)
    print(f"{OUT}  ({OUT.stat().st_size / 1024:.0f} KB, {len(md.price)}일)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
