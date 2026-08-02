#!/usr/bin/env python3
"""비트코인 '관련성 지도' — 무엇과 얼마나 관련 있는가.

    python3 tools/relationship_map.py

## 이 도구가 답하는 질문 — 다른 도구들과 다르다

이 저장소의 다른 검정 도구들은 **"이걸 모델에 넣을 수 있나"** 를 판정한다. 그래서
전방 예측력·독립성·표본외 안정성 같은 **엄격한 기준**을 건다.

이 도구는 그보다 **앞선 질문**에 답한다 — **"비트코인은 무엇과 관련이 있는가."**

    관련이 있다  ≠  신호로 쓸 수 있다

둘은 다른 층이다. 관련은 뚜렷한데 신호로는 못 쓰는 경우가 흔하다(동시에 움직여서
미리 알 수 없거나, 이미 아는 것의 되풀이라서). 반대로 관련이 없으면 신호일 수도 없다.
**이 도구는 첫 번째 층만 본다.**

## 세 가지 원칙

1. **수준이 아니라 변화율로 잰다.** BTC·나스닥·M2 는 전부 장기 우상향이라 수준끼리
   재면 "같이 올랐다"만 나온다(허위 상관). 이 저장소의 제1원칙이다(docs/25 2절).
2. **국면을 나눠 본다.** 전 구간 하나로 뭉개면 잘못 읽힌다 — 나스닥은 전 구간
   +0.13 이지만 2013~16 −0.01 / 2022~23 **+0.39** 로, **2020년부터 생긴 관계**다.
3. **파생과 외부를 구분한다.** MVRV·드로다운처럼 가격에서 계산되는 값은 상관이 높은
   것이 당연하다(항등식에 가깝다). '비트코인 밖의 무언가와 관련 있는가'라는 질문에
   답하는 것은 **외부 계열뿐**이다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import macro_correlation as mc        # noqa: E402
import onchain_correlation as oc      # noqa: E402

# (열, 라벨, 구분)  구분: 외부 = BTC 와 독립적으로 생성 / 파생 = 가격에서 계산
MACRO_SERIES = [
    ("nasdaq", "나스닥", "외부"), ("sp500", "S&P500", "외부"), ("vix", "VIX", "외부"),
    ("hy_spread", "신용 스프레드", "외부"), ("breakeven", "기대인플레", "외부"),
    ("m2_cn", "중국 M2", "외부"), ("dxy", "DXY", "외부"), ("kospi", "코스피", "외부"),
    ("gold", "금", "외부"), ("usdjpy", "엔(USDJPY)", "외부"),
    ("realyield", "10년 실질금리", "외부"), ("net_liq", "연준 순유동성", "외부"),
    ("em_fx", "신흥국 대비 달러", "외부"), ("copper", "구리", "외부"),
    ("oil", "WTI 원유", "외부"), ("curve", "수익률곡선", "외부"),
    ("china_eq", "중국 주가지수", "외부"),
]
ERAS = [(2013, 2016, "2013~16"), (2017, 2019, "2017~19"), (2020, 2021, "2020~21"),
        (2022, 2023, "2022~23"), (2024, 2100, "2024~")]


def band(x: Optional[float]) -> str:
    if x is None:
        return "—"
    a = abs(x)
    return ("뚜렷함" if a >= 0.40 else "있음" if a >= 0.25
            else "약함" if a >= 0.15 else "거의 없음")


def measure(price, ser, vix, ndq, use_log=None):
    r = mc.candidate_analysis(price, ser, vix, ndq,
                              use_log=(min(ser.values()) > 0) if use_log is None else use_log)
    prim = r["full_mo"] if r["monthly_only"] else r["full_wk"]
    return prim, r.get("partial_ndq"), r.get("n_primary", 0), r["primary"]


def era_scan(price, ser, use_log=None):
    """국면별 상관 — 관련성이 언제 생겼는지 본다."""
    import math
    freq = mc.weekly if not mc._is_monthly(ser) else mc.month_end
    monthly = mc._is_monthly(ser)
    pos = min(ser.values()) > 0 if use_log is None else use_log

    def keyed(chg):
        def k(d):
            return (d.year, d.month) if monthly else d.isocalendar()[:2]
        return {k(d): v for d, v in sorted(chg.items())}

    dk = keyed(mc._change(freq(ser), pos))
    bk = keyed(mc.log_returns(freq(price)))
    common = sorted(set(dk) & set(bk))
    out = []
    for lo, hi, lab in ERAS:
        ks = [c for c in common if lo <= c[0] <= hi]
        out.append((lab, mc.pearson([dk[c] for c in ks], [bk[c] for c in ks])
                    if len(ks) >= 15 else None, len(ks)))
    return out


def report(market: dict, macro: dict) -> str:
    price = market["price"]
    vix, ndq = macro.get("vix"), macro.get("nasdaq")
    L: list[str] = []
    add = L.append
    add("=" * 84)
    add("  비트코인 관련성 지도 — '무엇과 관련 있는가'")
    add("  ※ '모델에 넣을 수 있는가'는 다른 질문이다(docs/25~32). 여기서는 관련성만 본다.")
    add("=" * 84)

    rows = []
    for col, lab, kind in MACRO_SERIES:
        if col not in macro:
            continue
        p, pn, n, fr = measure(price, macro[col], vix, ndq)
        if p is not None:
            rows.append((kind, lab, p, pn, n, fr))
    mk, rc = market.get("market_cap"), market.get("realized_cap")
    if mk and rc:
        mv = {d: mk[d] / rc[d] for d in set(mk) & set(rc) if rc[d] > 0}
        p, pn, n, fr = measure(price, mv, vix, ndq)
        rows.append(("파생", "MVRV", p, pn, n, fr))
    if price:
        peak, dd = 0.0, {}
        for d in sorted(price):
            peak = max(peak, price[d])
            dd[d] = price[d] / peak - 1
        p, pn, n, fr = measure(price, dd, vix, ndq, use_log=False)
        rows.append(("파생", "드로다운", p, pn, n, fr))
    for col, lab in (("hashrate", "해시레이트"), ("active_addresses", "활성주소"),
                     ("exchange_supply", "거래소 잔고"), ("realized_cap", "실현시총")):
        if market.get(col):
            p, pn, n, fr = measure(price, market[col], vix, ndq)
            if p is not None:
                rows.append(("중간", lab, p, pn, n, fr))

    add(f"  {'구분':6}{'대상':16}{'|rho|':>8}{'나스닥통제':>11}{'n':>7}   관련성")
    add("  " + "-" * 76)
    for kind, lab, p, pn, n, fr in sorted(rows, key=lambda r: -abs(r[2])):
        add(f"  {kind:6}{lab:16}{abs(p):>8.3f}"
            f"{(f'{abs(pn):.3f}' if pn is not None else '—'):>11}{n:>7}   {band(p)}")
    add("")
    add("  ※ '파생'(MVRV·드로다운)은 가격에서 계산되므로 상관이 높은 것이 당연하다")
    add("     — 항등식에 가깝다. '외부' 계열만이 '비트코인 밖과 관련 있는가'에 답한다.")

    add("")
    add("  [국면별] 관련성은 시간에 따라 생기고 사라진다 — 전 구간 하나로 뭉개면 잘못 읽힌다")
    add(f"  {'대상':16}" + "".join(f"{lab:>10}" for _, _, lab in ERAS))
    add("  " + "-" * 76)
    for col, lab, kind in MACRO_SERIES[:8]:
        if col not in macro:
            continue
        es = era_scan(price, macro[col])
        add(f"  {lab:16}" + "".join(
            f"{(f'{v:+.2f}' if v is not None else '—'):>10}" for _, v, _ in es))
    add("")
    add("  → 답: **비트코인은 '위험자산'과 관련 있고, 그 관계는 2020년부터 생겼다.**")
    add("     나스닥 −0.01(2013~16) → +0.39(2022~23), VIX 도 같은 방향으로 확증된다.")
    add("     개별 자산(금·엔·원유·구리·부동산 등)과의 관련성은 대부분 그 관계의 그림자다.")
    add("=" * 84)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="비트코인 관련성 지도")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--macro", default="data/macro.csv")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    market = oc.load_market(args.csv)
    macro = mc.load_macro(args.macro)
    text = report(market, macro)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
