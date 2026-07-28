"""원시 지표값을 [-1, +1] 로 옮기는 정규화 계층.

-1 = 극단 저평가/항복,  0 = 중립,  +1 = 극단 과열

정규화 방식은 두 가지뿐이다.

1. 꺾은선 보간(piecewise linear) — config 의 ``anchors`` 를 그대로 쓴다.
   구간마다 기울기를 다르게 줄 수 있어서, "MVRV Z 3.2 → 0.70" 처럼
   과거 관찰을 직접 좌표로 박아 넣을 수 있다.
2. 상태 매핑(categorical) — Hash Ribbons 나 연준 스탠스처럼 값이 아니라
   상태인 지표용.

절대 수치에 집착하지 말라는 원문 3.2 의 경고 때문에, 꺾은선과 별개로
``percentile_rank`` 를 제공한다. 과거 N년 분포에서 지금이 몇 퍼센타일인지를
보는 방식이라 사이클마다 고점 수치가 낮아져도 따라간다.
"""

from __future__ import annotations

import bisect
import math
from typing import Mapping, Sequence


Anchor = Sequence[float]


class NormalizationError(ValueError):
    """정규화 설정이 잘못됐을 때."""


def _validate_anchors(anchors: Sequence[Anchor], name: str) -> list[tuple[float, float]]:
    if not anchors or len(anchors) < 2:
        raise NormalizationError(f"{name}: anchors 는 최소 2개 필요합니다.")
    pairs: list[tuple[float, float]] = []
    for a in anchors:
        if len(a) != 2:
            raise NormalizationError(f"{name}: anchor 는 [원시값, 정규화값] 쌍이어야 합니다: {a!r}")
        pairs.append((float(a[0]), float(a[1])))

    xs = [p[0] for p in pairs]
    if any(b <= a for a, b in zip(xs, xs[1:])):
        raise NormalizationError(f"{name}: anchors 의 원시값은 순증가해야 합니다: {xs}")

    ys = [p[1] for p in pairs]
    if any(y < -1.0 - 1e-9 or y > 1.0 + 1e-9 for y in ys):
        raise NormalizationError(f"{name}: 정규화값은 [-1, 1] 범위여야 합니다: {ys}")
    if any(b < a for a, b in zip(ys, ys[1:])):
        raise NormalizationError(f"{name}: 정규화값은 단조 비감소여야 합니다: {ys}")
    return pairs


def piecewise(value: float, anchors: Sequence[Anchor], *, name: str = "indicator") -> float:
    """꺾은선 보간. 앵커 바깥은 끝값으로 클램프한다."""
    pairs = _validate_anchors(anchors, name)
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    v = float(value)
    # NaN 은 모든 비교가 False 라서 위아래 클램프를 둘 다 빠져나간 뒤
    # bisect 가 범위 밖 인덱스를 돌려준다. 막지 않으면 IndexError 로 죽는다.
    # ±inf 는 클램프에 걸려 ±1.0 이 나오지만, 그건 데이터 오류를 극단 신호로
    # 둔갑시키는 것이라 역시 거부한다.
    if not math.isfinite(v):
        raise NormalizationError(f"{name}: 유한한 숫자가 아닙니다 ({value!r})")

    if v <= xs[0]:
        return ys[0]
    if v >= xs[-1]:
        return ys[-1]

    i = bisect.bisect_right(xs, v)
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    span = x1 - x0
    if span == 0:  # _validate_anchors 가 막지만 방어적으로
        return y1
    return y0 + (y1 - y0) * (v - x0) / span


def categorical(state: str, states: Mapping[str, float], *, name: str = "indicator") -> float:
    """상태 → 정규화값."""
    key = str(state).strip().lower()
    lowered = {str(k).strip().lower(): float(v) for k, v in states.items()}
    if key not in lowered:
        raise NormalizationError(
            f"{name}: 알 수 없는 상태 {state!r}. 가능한 값: {sorted(lowered)}"
        )
    score = lowered[key]
    if not -1.0 - 1e-9 <= score <= 1.0 + 1e-9:
        raise NormalizationError(f"{name}: 상태 {state!r} 의 값이 [-1,1] 밖입니다: {score}")
    return clamp(score)


def percentile_rank(value: float, history: Sequence[float]) -> float:
    """과거 분포에서의 위치를 [-1, +1] 로 환산한다.

    0 퍼센타일 → -1, 50 퍼센타일 → 0, 100 퍼센타일 → +1.
    사이클마다 지표의 절대 수치가 낮아지는 문제(원문 3.2, 7.1)에 대한 대안 척도.
    고정 앵커와 함께 보고, 둘이 크게 갈리면 그 자체가 경고 신호다.
    """
    vals = [float(v) for v in history if v is not None and math.isfinite(float(v))]
    if len(vals) < 2:
        raise NormalizationError("percentile_rank: 유한한 과거값이 최소 2개 필요합니다.")
    vals.sort()
    v = float(value)
    if not math.isfinite(v):
        raise NormalizationError(f"percentile_rank: 유한한 숫자가 아닙니다 ({value!r})")
    # 동일값을 절반으로 세는 표준 정의 (mid-rank)
    below = bisect.bisect_left(vals, v)
    equal = bisect.bisect_right(vals, v) - below
    pct = (below + 0.5 * equal) / len(vals)
    return clamp(2.0 * pct - 1.0)


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def blend(fixed: float, adaptive: float, weight_adaptive: float = 0.35) -> float:
    """고정 앵커 점수와 퍼센타일 점수를 섞는다.

    기본은 고정 앵커 65% + 퍼센타일 35%. 앵커는 해석 가능하고 퍼센타일은
    사이클 감쇠에 강하다. 둘의 장점을 조금씩 가져간다.
    """
    w = clamp(weight_adaptive, 0.0, 1.0)
    return clamp(fixed * (1.0 - w) + adaptive * w)
