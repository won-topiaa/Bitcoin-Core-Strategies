"""Reserve Risk 근사식 후보.

정식 Reserve Risk = Price ÷ HODL Bank 이고, HODL Bank 는 **코인나이**(VOCD,
소각된 코인일의 가치)를 창세부터 누적한 값이다. 그 코인나이 데이터는 UTXO 를
나이별로 쪼개야 나오고, 우리 데이터(가격·실현시총·유통량)로는 만들 수 없다.

그래서 여기서 검정하는 것은 **"코인나이 없이, 가격과 실현시총만으로 Reserve
Risk 의 직관을 얼마나 되살릴 수 있는가"** 다. 직관은 하나다:

    낮은 Reserve Risk = 가격은 싼데 장기 보유자가 안 판다(확신 큼) = 저점
    높은 Reserve Risk = 가격 강세에 보유자가 판다(확신 소진) = 고점

핵심 관문은 **중복도**다. 이 근사식들이 전부 price/realized_price 계열이라
MVRV 와 |ρ|≥0.85 로 겹치면, "Reserve Risk 의 값어치는 정확히 우리가 없는
코인나이에서 나온다"는 것이 증명되는 셈이다. 겹치지 않는 변형이 하나라도 있으면
그건 진짜 새 정보다. 관문이 판단하게 둔다.

네 변형을 넣는다. 전부 가격·실현시총·유통량만 쓴다.
"""

from __future__ import annotations

from typing import Optional


def _expanding_mean(series, h):
    """창세부터의 누적 평균. HODL Bank 의 '천천히 쌓이는 은행' 성질을 흉내낸다."""
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
    dates = a.dates
    out = []
    for d in dates:
        x, y = la.get(d), lb.get(d)
        out.append(fn(x, y) if x is not None and y is not None else None)
    return h.Series(dates, tuple(out), name)


def build(m, h):
    price = m.price
    rc, sp = m.realized_cap, m.supply
    mc = m.market_cap
    if rc is None or sp is None or mc is None:
        return {}

    # 실현가격 = 실현시총 ÷ 유통량. 시장 전체의 평균 매입가다.
    realized_price = _combine(rc, sp, lambda c, s: c / s if s else None,
                              "realized_price", h)
    # NUPL = (시총 − 실현시총) ÷ 시총. 미실현 손익 비율.
    nupl = _combine(mc, rc, lambda c, r: (c - r) / c if c else None, "nupl", h)

    # --- 변형 1 : 은행 = 실현가격의 누적평균 --------------------------------
    # HODL Bank 를 "지금까지 쌓인 평균 매입가"로 근사한다. 가격이 이 느린 은행
    # 위로 튀면 RR 높음(고점), 은행에 가까우면 RR 낮음(저점).
    bank1 = _expanding_mean(realized_price, h)
    rr_bank = _combine(price, bank1, lambda p, b: p / b if b else None,
                       "Reserve Risk 근사 (실현가 은행)", h)

    # --- 변형 2 : NUPL 을 자기 누적평균으로 정규화 --------------------------
    # 확신의 적분. 지금 미실현이익이 '역대 평균 대비' 얼마나 큰가.
    bank2 = _expanding_mean(nupl, h)
    rr_nupl = _combine(nupl, bank2, lambda v, b: v / b if b and b > 0 else None,
                       "Reserve Risk 근사 (NUPL 적분)", h)

    # --- 변형 3 : MVRV 초과분을 은행으로 나눔 -------------------------------
    # 정식 RR 에 가장 가깝게: 분자는 가격의 실현가 대비 초과(=MVRV−1),
    # 분모는 실현가 은행. 코인나이 대신 실현가 누적을 은행으로 쓴다.
    excess = _combine(price, realized_price, lambda p, r: p / r if r else None,
                      "mvrv", h)
    rr_excess = _combine(excess, bank1, lambda e, b: (e - 1.0) / b if b else None,
                         "Reserve Risk 근사 (MVRV÷은행)", h)

    # --- 변형 4 : 실현시총 성장률로 확신을 근사 -----------------------------
    # 실현시총이 오르는 속도 = 코인이 더 높은 원가로 재정착하는 속도 ≈ 확신 유입.
    # 가격을 그 확신으로 나눈다. 코인 이동을 실현시총 차분으로 근사한 것.
    rc_growth = h.change(rc, 30)            # 30일 전 대비 실현시총 배수
    rr_growth = _combine(excess, rc_growth,
                         lambda e, g: e / g if g else None,
                         "Reserve Risk 근사 (실현시총 성장)", h)

    return {
        rr_bank.name: rr_bank,
        rr_nupl.name: rr_nupl,
        rr_excess.name: rr_excess,
        rr_growth.name: rr_growth,
    }
