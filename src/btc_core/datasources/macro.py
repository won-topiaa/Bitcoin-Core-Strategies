"""거시 시계열(data/macro.csv)에서 LRS 구성요소 값을 계산한다.

지금 계산하는 것은 하나 — **중국 M2 유동성 임펄스**. 중국 M2 가 비트코인을 약
9~11개월(≈1년) 앞선다는 실측(docs/25)에 근거해, 이 값을 LRS(실행 '크기') 의
**선행 신호**로 쓴다. 방향(BCS) 에는 절대 넣지 않는다.

## 왜 '임펄스'(가속도)인가 — 레벨이 아니라

중국 M2 전년비는 늘 높은 좁은 띠(대략 6~14%)에 있고 장기적으로 완만히 낮아진다.
전년비 '수준'을 그대로 쓰면 고정 앵커에서 거의 상수(+큰 값)로 붙어 신호가 안 된다.
게다가 그 수준-상관(+0.43)의 일부는 '둘 다 우하향'이라는 공통추세 착시다. 그래서
**자기 24개월 평균에서 얼마나 벗어났나**(가속/감속)를 임펄스로 쓴다 — 추세를 빼고
남는 순수 순환 신호(실측 ρ≈+0.3, ~1년 선행)다. 값이 +면 완화 순풍, −면 긴축 역풍.

## 선행은 어떻게 반영되나

라이브 값은 **당월 임펄스를 그대로** 넣는다. 관계가 ~1년 선행이므로, 오늘의 중국
유동성 임펄스가 곧 향후 ~1년의 순풍/역풍을 미리 알려주는 셈이다 — 그래서 '선행'.
과거 스냅샷을 만들 때는 reference 시점 이하의 최신값만 쓴다(미래 참조 금지).
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Optional

# 중국 M2 임펄스: 전년비에서 뺄 자기 평균의 창(개월). 24개월이 실측에서 가장
# 강한 순환 신호를 남겼다(docs/25 보정). 최소 12개월 이력이 있어야 계산한다.
IMPULSE_WINDOW = 24


def _load_column(path: Path, column: str) -> dict[date, float]:
    """macro.csv 의 한 열을 날짜→값 사전으로. 빈 칸은 건너뛴다."""
    out: dict[date, float] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or column not in reader.fieldnames:
            return out
        for row in reader:
            raw = (row.get("date") or "").strip()
            val = (row.get(column) or "").strip()
            if not raw or not val:
                continue
            try:
                out[date.fromisoformat(raw[:10])] = float(val)
            except ValueError:
                continue
    return out


def _month_end(series: dict[date, float]) -> dict[date, float]:
    """각 (연,월) 의 마지막 관측만 남긴다."""
    by_month: dict[tuple[int, int], tuple[date, float]] = {}
    for d in sorted(series):
        by_month[(d.year, d.month)] = (d, series[d])
    return {d: v for d, v in by_month.values()}


def _yoy(series: dict[date, float], months: int = 12) -> dict[date, float]:
    """전년비 증가율(%). 월말 시계열에 쓴다."""
    out: dict[date, float] = {}
    ds = sorted(series)
    for i, d in enumerate(ds):
        if i >= months and series[ds[i - months]]:
            out[d] = (series[d] / series[ds[i - months]] - 1.0) * 100.0
    return out


def china_m2_impulse(path: Path, *, reference: Optional[date] = None,
                     window: int = IMPULSE_WINDOW) -> Optional[float]:
    """중국 M2 전년비 − 직전 `window`개월 평균(가속도, %p). reference 이하 최신값.

    reference 미래의 데이터는 절대 보지 않는다(과거 스냅샷 정직성). 이력이 모자라면
    None — LRS 는 그 구성요소를 결측으로 두고 나머지로 재정규화한다.
    """
    m2 = _yoy(_month_end(_load_column(path, "m2_cn")))
    if not m2:
        return None
    ds = [d for d in sorted(m2) if reference is None or d <= reference]
    if not ds:
        return None
    last = ds[-1]
    window_vals = [m2[d] for d in ds[-(window + 1):-1]]     # last 이전 window개
    if len(window_vals) < 12:
        return None
    return m2[last] - sum(window_vals) / len(window_vals)


def load_macro_signals(path: str | Path = "data/macro.csv", *,
                       reference: Optional[date] = None) -> dict[str, float]:
    """LRS 구성요소 자동값. 지금은 m2_impulse(중국 M2) 하나. 파일/이력 없으면 빈 사전."""
    out: dict[str, float] = {}
    imp = china_m2_impulse(Path(path), reference=reference)
    if imp is not None:
        out["m2_impulse"] = round(imp, 2)
    return out
