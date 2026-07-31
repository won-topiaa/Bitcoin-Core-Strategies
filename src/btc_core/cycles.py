"""사이클 전환점 — 고르지 않고 **잰다**.

## 왜 이 파일이 생겼나

전환점 날짜가 도구 다섯 곳에 각각 하드코딩돼 있었고, 그중 하나가 나머지와
달랐다. 전수 점검에서 재 보니 이랬다.

    구간         실제 극값일    하드코딩된 날짜        차이
    2013 고점    2013-12-04    2013-11-30            −4일
    2017 고점    2017-12-16    2017-12-17            +1일
    2021 고점    2021-11-08    2021-11-10            +2일
    2022 저점    2022-11-09    2022-11-21           +12일
    2026 저점    2026-06-30    2026-06-06           −24일

앞의 셋은 하루이틀 차이라 결과가 거의 안 움직인다(평균 +66.6 → +66.8).
**문제는 마지막이다.** 2026-06-06 을 쓰면 저점 네 곳의 σ 가 8.7 이고 고점과
저점 사이 빈 공간이 124점인데, 실제 최저 종가인 2026-06-30 을 쓰면 σ 10.8,
빈 공간 118점이 된다. **고른 날짜가 잰 날짜보다 결과를 좋아 보이게 했다.**

의도한 것은 아니다. 하지만 [docs/14](../../docs/14-과적합-노출도.md)가 경고한
바로 그 형태다 — 결과를 평가하는 기준 자체를 사람이 고르면, 고르는 순간
편향이 들어간다. 그래서 **날짜를 데이터에서 뽑는다.**

## 어떻게 재는가

사람이 정하는 것은 **탐색 창**뿐이고, 그 안에서 최고·최저 종가일을 찾는다.
창은 반감기에 걸어 둔다 — 반감기는 합의 규칙에 박힌 날짜라 사후 선택이
아니다.

    고점 = [반감기, 반감기+{TOP_WINDOW_DAYS}일] 안의 최고 종가일
    저점 = [그 고점, 다음 반감기+{BOTTOM_PAD_DAYS}일] 안의 최저 종가일

창을 넓게 잡아도 답이 안 바뀐다는 것을 테스트가 확인한다. 창 자체가 결과를
만들면 그것도 고른 것이나 마찬가지이기 때문이다.

## 진행 중인 사이클

마지막 저점은 **잠정**이다. 사이클이 안 끝났으므로 더 낮은 날이 나올 수 있다.
`Turn.provisional` 이 그 사실을 들고 다닌다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, Optional, Sequence

from .indicators import HALVINGS
from .series import Series

# 사람이 정하는 것은 이 둘뿐이다.
TOP_WINDOW_DAYS = 800      # 반감기 이후 고점이 나온 최장 시점은 546일이었다
BOTTOM_PAD_DAYS = 60       # 저점은 다음 반감기 직전까지도 갈 수 있다


@dataclass(frozen=True)
class Turn:
    """전환점 하나."""

    phase: Literal["top", "bottom"]
    when: date
    price: float
    cycle: str                  # 그 사이클을 부르는 이름 ("2021" 등)
    provisional: bool = False   # 사이클이 안 끝나 더 극단이 나올 수 있음

    @property
    def label(self) -> str:
        return f"{self.cycle} {'고점' if self.phase == 'top' else '저점'}"


def _extreme(px: dict[date, float], lo: date, hi: date,
             kind: str) -> Optional[tuple[date, float]]:
    span = {d: v for d, v in px.items() if lo <= d <= hi and v > 0}
    if not span:
        return None
    pick = max(span, key=span.get) if kind == "top" else min(span, key=span.get)
    return pick, span[pick]


def find_turns(price: Series, *, halvings: Sequence[date] = HALVINGS,
               top_window: int = TOP_WINDOW_DAYS,
               bottom_pad: int = BOTTOM_PAD_DAYS) -> list[Turn]:
    """가격 시계열에서 전환점을 뽑는다.

    반감기마다 고점 하나와 그 뒤의 저점 하나. 데이터가 창을 못 덮으면 그
    전환점은 건너뛴다 — 반쪽 창에서 뽑은 극값은 극값이 아니다.
    """
    px = {d: v for d, v in zip(price.dates, price.values) if v}
    if not px:
        return []
    first, last = min(px), max(px)

    out: list[Turn] = []
    for i, h in enumerate(halvings):
        if h > last:
            break
        top_hi = h + timedelta(days=top_window)
        # 창이 데이터 끝을 넘으면 아직 고점이 확정되지 않았다.
        top = _extreme(px, max(h, first), min(top_hi, last), "top")
        if top is None:
            continue
        cycle = str(top[0].year)
        out.append(Turn("top", top[0], top[1], cycle,
                        provisional=top_hi > last))

        nxt = halvings[i + 1] if i + 1 < len(halvings) else None
        bot_hi = (nxt + timedelta(days=bottom_pad)) if nxt else last
        bot = _extreme(px, top[0] + timedelta(days=1), min(bot_hi, last), "bottom")
        if bot is None:
            continue
        out.append(Turn("bottom", bot[0], bot[1], str(bot[0].year),
                        provisional=bot_hi > last))
    return out


def tops(turns: Sequence[Turn]) -> list[Turn]:
    return [t for t in turns if t.phase == "top"]


def bottoms(turns: Sequence[Turn]) -> list[Turn]:
    return [t for t in turns if t.phase == "bottom"]


# ---------------------------------------------------------------------------
# 데이터가 없을 때 쓰는 값
# ---------------------------------------------------------------------------
# `data/market.csv` (2010-07 ~ 2026-07) 에서 위 규칙으로 실제로 뽑은 결과다.
# 도구가 매번 전체 가격 시계열을 읽지 않아도 되게 여기 적어 둔다.
# **tests/test_cycles.py 가 이 값을 다시 재서 어긋나면 실패한다** — 데이터가
# 갱신되면 여기도 같이 갱신하라는 뜻이다.
MEASURED_TOPS: tuple[date, ...] = (
    date(2013, 12, 4),
    date(2017, 12, 16),
    date(2021, 11, 8),
    date(2025, 10, 6),
)
MEASURED_BOTTOMS: tuple[date, ...] = (
    date(2015, 1, 14),
    date(2018, 12, 15),
    date(2022, 11, 9),
    date(2026, 6, 30),      # 잠정 — 사이클이 안 끝났다
)

# 마지막 저점은 사이클이 진행 중이라 더 낮은 날이 나올 수 있다.
LAST_BOTTOM_IS_PROVISIONAL = True
