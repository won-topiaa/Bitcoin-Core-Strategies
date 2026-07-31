"""자본화 기반 유료 지표 근사 — 코인나이가 필요 없는 것들만.

Reserve Risk 검정([docs/23])에서 배운 것: 유료인 이유가 UTXO 나이 데이터
때문이면 근사는 MVRV/서멀캡으로 무너진다. 그래서 이번엔 **나이 데이터가 필요
없는데 우리가 아직 안 쓰는** 유료 지표만 골랐다. 전부 시총·실현시총·유통량·
발행량으로 만든다.

  1. Delta Cap 배수     — 시총 ÷ (실현시총 − 평균시총). Glassnode 바닥 모형.
  2. Delta Price 배수   — 가격 ÷ (실현가격 − 평균가격). 같은 것의 코인당 판.
  3. Average Cap 배수   — 시총 ÷ 평균시총. 200일선 대신 창세 이래 누적평균.
  4. Stock-to-Flow      — 유통량 ÷ 연간 발행량. 유명하지만 사실상 달력일 수 있다.

핵심은 다시 **중복도**다. 이것들도 느린 누적을 분모로 쓰므로 서멀캡과 겹칠
공산이 크다 — 그러면 "우리가 이미 그 정보를 갖고 있다"가 확인된다. 겹치지
않으면 진짜 새 정보다. 관문이 판단한다.
"""

from __future__ import annotations

from typing import Optional


def _expanding_mean(series, h):
    """창세부터의 누적 평균 = Average Cap / Average Price 의 정의 그대로."""
    out: list[Optional[float]] = []
    total, n = 0.0, 0
    for v in series.values:
        if v is not None:
            total += v
            n += 1
        out.append(total / n if n else None)
    return h.Series(series.dates, tuple(out), f"emean({series.name})")


def _combine(a, b, fn, name, h):
    la = dict(zip(a.dates, a.values))
    lb = dict(zip(b.dates, b.values))
    out = [fn(la.get(d), lb.get(d)) if la.get(d) is not None and lb.get(d) is not None
           else None for d in a.dates]
    return h.Series(a.dates, tuple(out), name)


def build(m, h):
    price, mc, rc, sp = m.price, m.market_cap, m.realized_cap, m.supply
    iss = m.issuance_btc
    if mc is None or rc is None or sp is None:
        return {}

    average_cap = _expanding_mean(mc, h)                       # 평균시총
    realized_price = _combine(rc, sp, lambda c, s: c / s if s else None,
                              "realized_price", h)
    average_price = _combine(average_cap, sp, lambda c, s: c / s if s else None,
                             "average_price", h)

    # 1. Delta Cap = 실현시총 − 평균시총. 시총을 이 바닥선으로 나눈다.
    #    delta_cap 이 0 근처면 배수가 폭발하므로, 양수이고 시총의 1% 이상일 때만.
    delta_cap = _combine(rc, average_cap, lambda r, a: r - a, "delta_cap", h)
    delta_cap_ratio = _combine(
        mc, delta_cap,
        lambda c, d: c / d if d and c and d > 0.01 * c else None,
        "Delta Cap 배수 (시총÷델타캡)", h)

    # 2. Delta Price = 실현가격 − 평균가격. 코인당 판.
    delta_price = _combine(realized_price, average_price, lambda r, a: r - a,
                           "delta_price", h)
    delta_price_ratio = _combine(
        price, delta_price,
        lambda p, d: p / d if d and p and d > 0.01 * p else None,
        "Delta Price 배수 (가격÷델타가격)", h)

    # 3. Average Cap 배수 = 시총 ÷ 평균시총. 200일선 대신 누적평균을 쓴 Mayer.
    avg_cap_ratio = _combine(mc, average_cap, lambda c, a: c / a if a else None,
                             "Average Cap 배수 (시총÷평균시총)", h)

    out = {
        delta_cap_ratio.name: delta_cap_ratio,
        delta_price_ratio.name: delta_price_ratio,
        avg_cap_ratio.name: avg_cap_ratio,
    }

    # 4. Stock-to-Flow = 유통량 ÷ 연간 발행량. 발행량이 있어야 만든다.
    if iss is not None:
        # 연간 발행 = 하루 발행 × 365. 발행이 0 인 날(데이터 구멍)은 건너뛴다.
        s2f = _combine(sp, iss, lambda s, i: s / (i * 365.0) if i and i > 0 else None,
                       "Stock-to-Flow (유통량÷연발행)", h)
        out[s2f.name] = s2f

    return out
