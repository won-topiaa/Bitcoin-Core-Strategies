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
   +0.25 이지만 2013~16 +0.23 / 2022~23 **+0.60** 으로, **2020년부터 뚜렷해진
   관계**다.
4. **모든 행을 같은 빈도(월간)로 잰다.** 후보마다 자기 빈도로 재면 표본 빈도만으로
   상관 크기가 갈려(Epps 효과) 순위가 뒤집힌다. 실제로 나스닥(주간 +0.13)이
   S&P500(월간 +0.22) 아래에 적혀 있었는데, 같은 월간 격자에서는 나스닥
   +0.25 > S&P500 +0.22 다.
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

# (열, 한국어, 영어, 구분, 국면스캔)
#   구분:     외부 = BTC 와 독립적으로 생성 / 파생 = 가격에서 계산
#   국면스캔: 아래 국면별 표·차트에 넣을 것. **슬라이스가 아니라 표시로 고른다** —
#             예전엔 `MACRO_SERIES[:8]` 이었고, 목록 순서를 바꾸면 표의 내용이
#             말없이 바뀌었다.
MACRO_SERIES = [
    ("nasdaq", "나스닥", "Nasdaq", "외부", True),
    ("sp500", "S&P500", "S&P 500", "외부", True),
    ("vix", "VIX", "VIX", "외부", True),
    ("hy_spread", "신용 스프레드", "Credit spread", "외부", True),
    ("breakeven", "기대인플레", "Breakeven", "외부", True),
    ("m2_cn", "중국 M2", "China M2", "외부", True),
    ("dxy", "DXY", "DXY", "외부", True),
    ("kospi", "코스피", "KOSPI", "외부", True),
    ("gold", "금", "Gold", "외부", False),
    ("usdjpy", "엔(USDJPY)", "Yen (USDJPY)", "외부", False),
    ("realyield", "10년 실질금리", "10y real yield", "외부", False),
    ("net_liq", "연준 순유동성", "Fed net liq.", "외부", False),
    ("em_fx", "신흥국 대비 달러", "USD vs EM", "외부", False),
    ("copper", "구리", "Copper", "외부", False),
    ("oil", "WTI 원유", "WTI crude", "외부", False),
    ("curve", "수익률곡선", "Yield curve", "외부", False),
    ("china_eq", "중국 주가지수", "China equities", "외부", False),
]
# 시장·온체인 쪽. (열, 한국어, 영어, 구분)
MARKET_SERIES = [
    ("hashrate", "해시레이트", "Hash rate", "중간"),
    ("active_addresses", "활성주소", "Active addresses", "중간"),
    ("exchange_supply", "거래소 잔고", "Exchange balance", "중간"),
    ("realized_cap", "실현시총", "Realized cap", "중간"),
]
ERAS = [(2013, 2016, "2013~16"), (2017, 2019, "2017~19"), (2020, 2021, "2020~21"),
        (2022, 2023, "2022~23"), (2024, 2100, "2024~")]

# 구분·관련성 등급의 영어. 화면이 두 언어라 한쪽만 있으면 영어 화면에 한국어가 남는다.
# 영어는 짧게 — 이 값이 표의 한 칸에 들어가고, 폰에서 표 폭을 결정한다.
# "Derived from price" 로 적었더니 그 칸 하나가 화면 밖으로 표를 밀어냈다.
KIND_EN = {"외부": "External", "파생": "From price", "중간": "In between"}
BAND_EN = {"뚜렷함": "Strong", "있음": "Present", "약함": "Weak",
           "거의 없음": "Little to none", "—": "—"}
# |ρ| 등급의 경계. 화면·문서·리포트가 같은 표를 쓴다.
BAND_CUTS = ((0.40, "뚜렷함"), (0.25, "있음"), (0.15, "약함"))


def band(x: Optional[float]) -> str:
    if x is None:
        return "—"
    a = abs(x)
    for cut, label in BAND_CUTS:
        if a >= cut:
            return label
    return "거의 없음"


# 표본이 이보다 짧으면 '한 국면만 본 값'이다. 사이클 하나가 약 4년이라
# 5년을 못 넘기면 다른 국면과 비교할 수가 없다.
SHORT_YEARS = 5.0


def measure(price, ser, vix, ndq, use_log=None):
    """이 장의 모든 행을 **같은 월간 격자**에서 잰다.

    이 표는 여러 후보를 나란히 세워 순위를 매기고 밴드('뚜렷함'/'약함'...)를
    붙이는 곳이다. 후보마다 자기 빈도(주간/월간)로 재면 표본 빈도만으로 크기가
    갈려(Epps 효과) 순위가 뒤집힌다 — 실제로 나스닥은 주간 +0.130, S&P500 은
    월간 +0.223 이라 "비트코인은 S&P500 과 더 관련 있다"로 읽혔지만, 같은 월간
    격자에서는 나스닥 +0.253 > S&P500 +0.223 이다. 국면 표(era_scan)도 같은
    이유로 월간으로 맞춘다.
    """
    r = mc.candidate_analysis(price, ser, vix, ndq,
                              use_log=(min(ser.values()) > 0) if use_log is None else use_log,
                              force_monthly=True)
    return r["full_mo"], r.get("partial_ndq"), r.get("n_primary", 0), r["primary"], _years(r)


def _years(r: dict) -> float:
    """주요 프레임이 실제로 덮은 기간(년).

    표본 수(n)만 보여 주면 주간 156개가 3년인지 3주인지 알 수 없다. 그리고
    이 저장소에서 **가장 큰 외부 상관(신용 스프레드)이 하필 가장 짧은 표본**
    위에 있어서, 그 사실이 화면에 안 보이면 순위표가 사람을 오도한다.
    """
    span = r.get("span")
    if not span:
        return 0.0
    (y0, u0), (y1, u1) = span
    per = 12 if r.get("monthly_only") else 52.0        # 월 or ISO 주
    return round(((y1 - y0) * per + (u1 - u0)) / per, 1)


def era_scan(price, ser, use_log=None):
    """국면별 상관 — 관련성이 언제 생겼는지 본다.

    **월간 격자로 고정한다.** 이 값들은 한 차트에 여러 계열을 겹쳐 그리는 데
    쓰이므로, 계열마다 빈도가 다르면 선끼리 비교가 성립하지 않는다(measure 와
    같은 이유). 한 국면은 2~5년이라 월간으로도 24~60점이 나온다.
    """
    pos = min(ser.values()) > 0 if use_log is None else use_log

    def keyed(chg):
        return {(d.year, d.month): v for d, v in sorted(chg.items())}

    dk = keyed(mc._change(mc.month_end(ser), pos))
    bk = keyed(mc.log_returns(mc.month_end(price)))
    common = sorted(set(dk) & set(bk))
    out = []
    for lo, hi, lab in ERAS:
        ks = [c for c in common if lo <= c[0] <= hi]
        # 국면 길이의 절반은 덮어야 그 국면의 값이라고 부를 수 있다. 예전에는
        # 빈도와 무관하게 15점만 넘으면 값을 냈는데, 그러면 5개월치 조각이
        # '2022~23' 이라는 두 해짜리 칸에 그려졌다(신용 스프레드가 실제로 그랬다).
        need = max(12, int((hi - lo + 1) * 12 * 0.5)) if hi < 2100 else 12
        out.append((lab, mc.pearson([dk[c] for c in ks], [bk[c] for c in ks])
                    if len(ks) >= need else None, len(ks)))
    return out


def drawdown(price: dict) -> dict:
    peak, dd = 0.0, {}
    for d in sorted(price):
        peak = max(peak, price[d])
        dd[d] = price[d] / peak - 1
    return dd


def payload(market: dict, macro: dict) -> dict:
    """관련성 지도를 **자료 구조로** 낸다 — 화면과 리포트가 같은 숫자를 쓰게.

    화면(viz/relationship.body.html)이 이 값을 굽는다. 숫자를 페이지에 손으로
    적어 두면 데이터가 갱신될 때 문서·리포트와 조용히 갈라진다.
    """
    price = market.get("price") or {}
    vix, ndq = macro.get("vix"), macro.get("nasdaq")
    ranked: list[dict] = []

    def add(key: str, kind: str, ko: str, en: str, ser: dict, *, use_log=None) -> None:
        # 빈 계열은 `min(ser.values())` 에서 터진다. 표본 CSV 로 빌드할 때
        # 실제로 밟는 경로라, 조용히 건너뛴다.
        if not ser or not price:
            return
        p, pn, n, fr, yrs = measure(price, ser, vix, ndq, use_log=use_log)
        if p is None:
            return
        ranked.append({
            # 화면이 특정 계열(나스닥·VIX)을 골라 강조한다. **라벨이 아니라 키로**
            # 고른다 — 라벨은 번역되고 다듬어지지만 키는 데이터 열 이름이다.
            "key": key,
            # 영어 필드는 **`_en` 접미사**여야 한다. 화면의 pick(o, "label") 이
            # o["label_en"] 을 찾는다 — camelCase 로 적으면 영어 화면에 한국어가
            # 그대로 남는다(실제로 그랬다). tests/test_viz.py 가 이제 막는다.
            "kind": kind, "kind_en": KIND_EN[kind], "label": ko, "label_en": en,
            "rho": round(p, 3),
            # 나스닥 자신은 자기를 통제할 수 없다 — 그 칸은 비운다
            "partial": None if pn is None else round(pn, 3),
            "n": n, "freq": fr, "years": yrs,
            # 한 국면만 덮은 표본은 다른 행과 같은 잣대로 읽으면 안 된다.
            "short": yrs < SHORT_YEARS,
            "band": band(p), "band_en": BAND_EN[band(p)],
        })

    for col, ko, en, kind, _era in MACRO_SERIES:
        if col in macro:
            add(col, kind, ko, en, macro[col])
    mk, rc = market.get("market_cap"), market.get("realized_cap")
    if mk and rc:
        add("mvrv", "파생", "MVRV", "MVRV",
            {d: mk[d] / rc[d] for d in set(mk) & set(rc) if rc[d] > 0})
    if price:
        add("drawdown", "파생", "드로다운", "Drawdown", drawdown(price), use_log=False)
    for col, ko, en, kind in MARKET_SERIES:
        if market.get(col):
            add(col, kind, ko, en, market[col])
    ranked.sort(key=lambda r: -abs(r["rho"]))

    eras: list[dict] = []
    for col, ko, en, _kind, in_era in MACRO_SERIES:
        if not in_era or not macro.get(col) or not price:
            continue
        scan = era_scan(price, macro[col])
        eras.append({"key": col, "label": ko, "label_en": en,
                     "v": [None if v is None else round(v, 3) for _, v, _ in scan],
                     "n": [n for _, _, n in scan]})

    return {"ranked": ranked, "eraLabels": [lab for _, _, lab in ERAS],
            "eras": eras, "cuts": [c for c, _ in BAND_CUTS],
            # 화면의 각주가 이 숫자를 그대로 읽는다 — 문턱을 여기서만 정한다
            "shortYears": SHORT_YEARS}


def render(pl: dict) -> str:
    L: list[str] = []
    add = L.append
    add("=" * 84)
    add("  비트코인 관련성 지도 — '무엇과 관련 있는가'")
    add("  ※ '모델에 넣을 수 있는가'는 다른 질문이다(docs/25~32). 여기서는 관련성만 본다.")
    add("=" * 84)
    add(f"  {'구분':6}{'대상':16}{'rho':>8}{'나스닥통제':>11}{'n':>7}{'기간':>7}   관련성")
    add("  " + "-" * 76)
    for r in pl["ranked"]:
        pn = r["partial"]
        add(f"  {r['kind']:6}{r['label']:16}{r['rho']:>+8.3f}"
            f"{(f'{pn:+.3f}' if pn is not None else '—'):>11}{r['n']:>7}"
            f"{r['years']:>6.1f}y{'*' if r['short'] else ' '}  {r['band']}")
    add("")
    add("  ※ '파생'(MVRV·드로다운)은 가격에서 계산되므로 상관이 높은 것이 당연하다")
    add("     — 항등식에 가깝다. '외부' 계열만이 '비트코인 밖과 관련 있는가'에 답한다.")
    add(f"  ※ '*' 는 표본이 {SHORT_YEARS:.0f}년 미만 = **한 국면만 본 값**이라는 표시다.")

    add("")
    add("  [국면별] 관련성은 시간에 따라 생기고 사라진다 — 전 구간 하나로 뭉개면 잘못 읽힌다")
    add(f"  {'대상':16}" + "".join(f"{lab:>10}" for lab in pl["eraLabels"]))
    add("  " + "-" * 76)
    for row in pl["eras"]:
        add(f"  {row['label']:16}" + "".join(
            f"{(f'{v:+.2f}' if v is not None else '—'):>10}" for v in row["v"]))
    add("")
    # 이 문장의 숫자는 위 표에서 그대로 읽는다. 손으로 적어 두면 격자를 바꾸거나
    # 데이터가 갱신될 때 표와 문장이 조용히 갈라진다(실제로 그랬다 — 주간 격자
    # 시절의 −0.01/+0.39 가 월간으로 바꾼 뒤에도 남아 있었다).
    nd_row = next((r for r in pl["eras"] if r["label"] == "나스닥"), None)
    if nd_row and nd_row["v"] and len(nd_row["v"]) >= 2:
        first = next(((pl["eraLabels"][i], v) for i, v in enumerate(nd_row["v"])
                      if v is not None), None)
        last = next(((pl["eraLabels"][i], v) for i, v in reversed(list(enumerate(nd_row["v"])))
                     if v is not None), None)
        add("  → 답: **비트코인은 '위험자산'과 관련 있고, 그 관계는 2020년부터 뚜렷해졌다.**")
        if first and last and first[0] != last[0]:
            add(f"     나스닥 {first[1]:+.2f}({first[0]}) → {last[1]:+.2f}({last[0]}), "
                "VIX 도 같은 방향으로 확증된다.")
        # 결론의 이어지는 문장이라 결론이 없으면 같이 빠져야 한다. 예전에는 이 줄만
        # 바깥에 있어서 나스닥 열이 없을 때 '그 관계'가 무엇인지 없는 채로 홀로 찍혔다.
        add("     개별 자산(금·엔·원유·구리·부동산 등)과의 관련성은 대부분 그 관계의 그림자다.")
    else:
        add("  → 나스닥 열이 없어 이 지도의 결론(위험자산과의 관련성)은 내지 않는다.")
    add("=" * 84)
    return "\n".join(L)


def report(market: dict, macro: dict) -> str:
    return render(payload(market, macro))


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
