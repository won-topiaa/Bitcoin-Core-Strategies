#!/usr/bin/env python3
"""온체인 고유 지표 ↔ 비트코인 — 순환논증을 걸러내는 검정.

    python3 tools/onchain_correlation.py --csv data/market.csv

## 왜 거시와 다른 설계가 필요한가

거시 후보(docs/27·28)는 BTC 와 **독립적으로 생성되는** 시계열이라 동시점 상관이
곧바로 의미가 있었다. 온체인은 다르다 — 상당수가 **가격에서 파생**된다.

    market_cap = price × supply        →  Δlog(market_cap) ≡ Δlog(price)
    MVRV = market_cap / realized_cap   →  분자가 가격 그 자체

이런 값을 BTC 수익률과 동시점 상관내면 ρ≈1 이 나오는데, 그건 발견이 아니라
**항등식**이다. 게다가 이 함정은 **거시에서 쓰던 통제(ΔVIX·나스닥)를 그대로
통과한다** — 가격의 그림자는 나스닥과 무관하니 당연히 '독립적'으로 보인다.

> **그래서 이 도구의 핵심 규칙:** 통제를 통과했다고 신호가 아니다.
> **전방 예측력이 없는데 동시점만 높으면 가격의 그림자다.**

## 두 층으로 나눠 잰다

  [A] 비순환 계열 — 거래소 잔고·순유입, 해시레이트, 활성주소
      가격의 대수적 함수가 아니다. 동시점·전방 둘 다 의미가 있다.
  [B] 가격 파생 계열 — MVRV, Puell, 실현시총 유입
      **동시점은 계산조차 하지 않는다.** 전방 예측만 본다.

전방 예측(t 시점 값 → t~t+k 수익률)은 순환이 원천적으로 불가능하다. 미래 수익률은
오늘 값에 들어갈 수 없기 때문이다. 그리고 이것이 사이클 도구에 실제로 쓸모 있는
형태이기도 하다.

## 실측 결론 (docs/29)

통과 0. 특히 **실현시총 유입률**은 동시점 +0.33 에 ΔVIX·나스닥 통제도 +0.33 으로
전혀 줄지 않아 '첫 발견'처럼 보였으나, Δ가격과 **+0.67** 로 붙어 있고 전방 예측력이
0 이었다 — 가격의 그림자였다. 이 도구는 그 패턴을 자동으로 표시한다.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import macro_correlation as mc      # noqa: E402

# --extended 로 받아오는 외부 아카이브. 둘 다 **무료·공개**지만 조건이 다르다.
#   coinmetrics/data  : CC BY-NC 4.0 — 출처표시 + **비상업적 사용만**
#   supervik/...      : MIT — 제약 없음 (펀딩비 2020~2023, 갱신 중단)
# 상업적 배포를 계획한다면 CoinMetrics 조건을 반드시 확인해야 한다(docs/29).
CM_URL = ("https://raw.githubusercontent.com/coinmetrics/data/master/csv/btc.csv")
ETH_URL = "https://raw.githubusercontent.com/coinmetrics/data/master/csv/eth.csv"
FUNDING_URL = ("https://raw.githubusercontent.com/supervik/"
               "historical-funding-rates-fetcher/main/data/BTC-USDT/"
               "BTC-USDT_binance_2020-01-01_2024-01-01_funding_history.csv")

SIZE_BAR = 0.25
SHADOW_CONTEMP = 0.25      # 동시점이 이 이상인데
SHADOW_FWD = 0.10          # 전방이 이 미만이면 '가격의 그림자'로 표시


def load_market(path: str) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        names = [c for c in (rd.fieldnames or []) if c != "date"]
        for c in names:
            out[c] = {}
        for row in rd:
            try:
                d = date.fromisoformat((row.get("date") or "")[:10])
            except ValueError:
                continue
            for c in names:
                v = (row.get(c) or "").strip()
                if not v:
                    continue
                try:
                    f = float(v)
                except ValueError:
                    continue
                if math.isfinite(f):
                    out[c][d] = f
    return {c: s for c, s in out.items() if s}


def _sma(s: dict[date, float], win: int) -> dict[date, float]:
    ds = sorted(s)
    return {ds[i]: sum(s[ds[j]] for j in range(i - win + 1, i + 1)) / win
            for i in range(win - 1, len(ds))}


def build_candidates(m: dict[str, dict[date, float]]) -> list[tuple]:
    """(이름, 계열, 가격파생?, 수준으로 볼까?, 기대부호, 설명)"""
    out: list[tuple] = []
    mcap, rcap = m.get("market_cap"), m.get("realized_cap")

    if rcap:
        # **원계열을 그대로 넘기고 level=False 로 둔다.** 여기서 일간 증가율을 만들어
        # 넘기면, 뒤에서 month_end 가 '그 달 마지막 하루치'만 뽑아 월간 자금유입이
        # 아니라 하루치를 재게 된다(실제로 그렇게 짰다가 +0.32 라는 헛값이 나왔다).
        # 원계열을 넘기면 월말↔월말 로그변화 = 그 달 실현시총 유입률이 된다.
        out.append(("실현시총 유입률", rcap, True, False, "+",
                    "온체인 순자금유입 — ETF 순유입의 온체인 대응물"))
    if m.get("exchange_supply"):
        out.append(("거래소 잔고", m["exchange_supply"], False, False, "-",
                    "잔고 감소 = 매집"))
    if m.get("exchange_inflow") and m.get("exchange_outflow") and m.get("supply"):
        ein, eout, sup = m["exchange_inflow"], m["exchange_outflow"], m["supply"]
        net = {d: (ein[d] - eout[d]) / sup[d] * 1e6
               for d in set(ein) & set(eout) & set(sup) if sup[d]}
        out.append(("거래소 순유입", net, False, True, "-", "거래소로 순유입 = 매도압력"))
    if m.get("hashrate"):
        out.append(("해시레이트", m["hashrate"], False, False, "+", "채굴자 확신"))
    if m.get("active_addresses"):
        out.append(("활성주소", m["active_addresses"], False, False, "+", "네트워크 실사용"))
    if mcap and rcap:
        mv = {d: mcap[d] / rcap[d] for d in set(mcap) & set(rcap) if rcap[d] > 0}
        out.append(("MVRV", mv, True, True, "-", "원가 대비 위치 — 비싸면 이후 수익률↓"))
    price = m.get("price")
    if price:
        # 드로다운 — 사상 고점 대비. '싸게 사라'가 실제로 통하는지 재는 자리다.
        ds = sorted(price)
        peak = 0.0
        dd = {}
        for d in ds:
            peak = max(peak, price[d])
            if peak > 0:
                dd[d] = price[d] / peak - 1.0
        out.append(("드로다운(고점대비)", dd, True, True, "-",
                    "깊이 빠졌을수록 이후 수익률↑ (=역발상)"))
        # 30일 실현변동성 — 변동성 위험프리미엄
        lr = {ds[i]: math.log(price[ds[i]] / price[ds[i - 1]])
              for i in range(1, len(ds)) if price[ds[i - 1]] > 0 and price[ds[i]] > 0}
        ls = sorted(lr)
        vol = {}
        for i in range(29, len(ls)):
            w = [lr[ls[j]] for j in range(i - 29, i + 1)]
            mu = sum(w) / len(w)
            vol[ls[i]] = (sum((x - mu) ** 2 for x in w) / len(w)) ** 0.5 * (365 ** 0.5)
        if vol:
            out.append(("실현변동성(30일)", vol, True, True, "+",
                        "변동성 위험프리미엄 — 높을수록 이후 보상↑?"))
    if m.get("issuance_usd"):
        iu = m["issuance_usd"]
        avg = _sma(iu, 365)
        pu = {d: iu[d] / avg[d] for d in set(iu) & set(avg) if avg[d] > 0}
        out.append(("Puell", pu, True, True, "-", "채굴 매출 / 365일 평균"))
    return out


def forward(metric: dict[date, float], price: dict[date, float],
            months: int, level: bool,
            lo: Optional[int] = None, hi: Optional[int] = None) -> tuple[Optional[float], int]:
    """t 시점 지표 → t~t+k개월 로그수익률. 순환 불가한 유일한 프레임."""
    pm = mc.month_end(price)
    mm = mc.month_end(metric)
    if not level:
        pos = mm and min(mm.values()) > 0
        mm = mc._change(mm, bool(pos))
    P = {(d.year, d.month): v for d, v in pm.items()}
    M = {(d.year, d.month): v for d, v in mm.items()}
    ks = sorted(P)
    idx = {k: i for i, k in enumerate(ks)}
    xs, ys = [], []
    for k in sorted(set(M) & set(P)):
        i = idx.get(k)
        if i is None or i + months >= len(ks):
            continue
        if lo is not None and not (lo <= k[0] <= hi):
            continue
        a, b = P[ks[i]], P[ks[i + months]]
        if a > 0 and b > 0:
            xs.append(M[k])
            ys.append(math.log(b / a))
    return (mc.pearson(xs, ys), len(xs))


def _get(url: str, timeout: int = 60) -> Optional[str]:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except Exception as exc:                       # 정책 차단·네트워크 실패 모두
        print(f"  ! {url.rsplit('/', 1)[-1]}: {exc} — 건너뜀", file=sys.stderr)
        return None


def fetch_extended(btc_price: Optional[dict] = None) -> list[tuple]:
    """외부 아카이브에서 추가 후보를 받는다. 실패하면 빈 목록(치명적이지 않다)."""
    import io as _io
    out: list[tuple] = []
    txt = _get(CM_URL)
    if txt:
        cm: dict[str, dict[date, float]] = {}
        want = ("CapMrktCurUSD", "volume_reported_spot_usd_1d", "FeeTotNtv",
                "AdrBalCnt", "TxCnt")
        for row in csv.DictReader(_io.StringIO(txt)):
            try:
                d = date.fromisoformat((row.get("time") or "")[:10])
            except ValueError:
                continue
            for c in want:
                v = (row.get(c) or "").strip()
                if not v:
                    continue
                try:
                    f = float(v)
                except ValueError:
                    continue
                if math.isfinite(f):
                    cm.setdefault(c, {})[d] = f
        mcap, vol = cm.get("CapMrktCurUSD"), cm.get("volume_reported_spot_usd_1d")
        if mcap and vol:
            nvt = {d: mcap[d] / vol[d] for d in set(mcap) & set(vol) if vol[d] > 0}
            out.append(("NVT(시총/거래량)", nvt, True, True, "-", "고평가 = 이후 수익률↓"))
        fee, tx = cm.get("FeeTotNtv"), cm.get("TxCnt")
        if fee:
            out.append(("수수료 총액(BTC)", fee, False, False, "+", "블록 수요"))
        if fee and tx:
            ft = {d: fee[d] / tx[d] for d in set(fee) & set(tx) if tx[d] > 0}
            out.append(("건당 수수료", ft, False, False, "+", "혼잡 = 수요"))
        if cm.get("AdrBalCnt"):
            out.append(("잔고보유 주소수", cm["AdrBalCnt"], False, False, "+", "채택 확산"))
        if tx:
            out.append(("거래건수", tx, False, False, "+", "네트워크 사용"))

    txt = _get(ETH_URL)
    if txt:
        eth: dict[date, float] = {}
        for row in csv.DictReader(_io.StringIO(txt)):
            v = (row.get("PriceUSD") or "").strip()
            if not v:
                continue
            try:
                f = float(v)
            except ValueError:
                continue
            if math.isfinite(f) and f > 0:
                try:
                    eth[date.fromisoformat((row.get("time") or "")[:10])] = f
                except ValueError:
                    pass
        # **반드시 비율로 만든다.** ETH 달러가격을 그대로 넘기면 그 변화의 대부분이
        # BTC 변화라 이름만 '비율'인 다른 값이 된다(실제로 그렇게 짰다가 잡혔다).
        if btc_price:
            ratio = {d: eth[d] / btc_price[d]
                     for d in set(eth) & set(btc_price) if btc_price[d] > 0}
            if ratio:
                out.append(("ETH/BTC 비율", ratio, False, False, "+",
                            "크립토 내부 위험선호 — 알트 강세 = 위험선호"))
        else:
            print("  ! ETH/BTC: BTC 가격이 없어 비율을 만들 수 없음 — 건너뜀",
                  file=sys.stderr)

    txt = _get(FUNDING_URL)
    if txt:
        import statistics as _st
        byday: dict[date, list[float]] = {}
        for row in csv.DictReader(_io.StringIO(txt)):
            try:
                d = date.fromisoformat((row.get("Date") or "")[:10])
                byday.setdefault(d, []).append(float(row["Funding Rate"]))
            except (ValueError, KeyError):
                continue
        if byday:
            # 하루 3회(8시간) 정산의 합 = 그날 총 펀딩
            daily = {d: sum(v) for d, v in byday.items()}
            out.append(("펀딩비(Binance)", daily, True, True, "-",
                        "롱 과열 = 이후 수익률↓ (2020~2023, 갱신 중단)"))
    return out


def forward_ex_momentum(metric: dict[date, float], price: dict[date, float],
                        months: int, level: bool) -> Optional[float]:
    """전방 예측에서 **당월 가격수익률(모멘텀)** 을 통제한 부분상관.

    가격과 기계적으로 붙은 온체인 지표(실현시총 유입 등)는 '오르면 유입도 커진다'
    때문에 전방 상관이 뜬다. 그건 지표의 정보가 아니라 **가격 모멘텀**이다.
    당월 수익률을 통제해도 남는 몫이 있어야 고유 정보라 할 수 있다."""
    pm = mc.month_end(price)
    mm = mc.month_end(metric)
    if not level:
        mm = mc._change(mm, bool(mm) and min(mm.values()) > 0)
    P = {(d.year, d.month): v for d, v in pm.items()}
    M = {(d.year, d.month): v for d, v in mm.items()}
    R = {(d.year, d.month): v for d, v in mc.log_returns(pm).items()}
    ks = sorted(P)
    idx = {k: i for i, k in enumerate(ks)}
    xs, ys, zs = [], [], []
    for k in sorted(set(M) & set(R)):
        i = idx.get(k)
        if i is None or i + months >= len(ks):
            continue
        a, b = P[ks[i]], P[ks[i + months]]
        if a > 0 and b > 0:
            xs.append(M[k]); ys.append(math.log(b / a)); zs.append(R[k])
    if len(xs) < 24:
        return None
    xy, xz, yz = mc.pearson(xs, ys), mc.pearson(xs, zs), mc.pearson(ys, zs)
    if None in (xy, xz, yz):
        return None
    den = math.sqrt((1 - xz ** 2) * (1 - yz ** 2))
    return (xy - xz * yz) / den if den else None


def analyse(market: dict, macro: dict, extended: bool = False) -> list[dict]:
    price = market["price"]
    vix, ndq = macro.get("vix"), macro.get("nasdaq")
    rows = []
    cands = build_candidates(market)
    if extended:
        cands = cands + fetch_extended(price)
    for name, ser, derived, level, want, desc in cands:
        f3 = forward(ser, price, 3, level)
        f6 = forward(ser, price, 6, level)
        f12 = forward(ser, price, 12, level)
        # 동시점·통제는 **모든** 후보에 계산한다. 가격 파생이라고 건너뛰면 정작
        # '가격의 그림자'(동시점만 높고 전방은 0)를 탐지할 수 없다 — 그 진단이
        # 이 도구의 핵심이므로, 값을 구하고 순환 여부는 따로 표시한다.
        con = pv = pn = None
        if vix and ndq:
            r = mc.candidate_analysis(price, ser, vix, ndq,
                                      use_log=bool(ser and min(ser.values()) > 0))
            con = r["full_mo"] if r["monthly_only"] else r["full_wk"]
            pv, pn = r.get("partial_vix"), r.get("partial_ndq")

        # 순환 진단 — 이 지표의 '변화'가 '가격의 변화'와 얼마나 기계적으로 붙어 있나.
        # 높을수록 지표가 가격을 다시 쓴 것에 가깝다.
        pm_ = mc.month_end(price)
        mm_ = mc.month_end(ser)
        pch = mc.log_returns(pm_)
        if level:
            mch = mm_                      # 이미 변화율/수준 — 그대로 쓴다
        else:
            mch = mc._change(mm_, bool(mm_) and min(mm_.values()) > 0)
        PK = {(d.year, d.month): v for d, v in pch.items()}
        MK = {(d.year, d.month): v for d, v in mch.items()}
        ck = sorted(set(PK) & set(MK))
        with_price = mc.pearson([MK[k] for k in ck], [PK[k] for k in ck]) if len(ck) > 12 else None
        eras = {lab: forward(ser, price, 3, level, lo, hi)
                for lo, hi, lab in ((2014, 2017, "14~17"), (2018, 2021, "18~21"),
                                    (2022, 2100, "22~"))}
        best = max((abs(t[0]) for t in (f3, f6, f12) if t[0] is not None), default=0.0)
        signs = [t[0] for t in eras.values() if t[0] is not None]
        stable = bool(signs) and (all(s > 0 for s in signs) or all(s < 0 for s in signs))
        exm = forward_ex_momentum(ser, price, 3, level)
        rows.append(dict(name=name, derived=derived, desc=desc, want=want,
                         f3=f3, f6=f6, f12=f12, con=con, pv=pv, pn=pn,
                         with_price=with_price, ex_mom=exm,
                         eras=eras, best=best, stable=stable,
                         passed=best >= SIZE_BAR and stable
                                and exm is not None and abs(exm) >= 0.2))
    return rows


def report(rows: list[dict]) -> str:
    L: list[str] = []
    add = L.append
    add("=" * 96)
    add("  온체인 고유 지표 ↔ 비트코인 — 전방 예측(순환 불가)이 주요 프레임")
    add(f"  통과 기준: 전방 |ρ| ≥ {SIZE_BAR} AND 국면별 부호 일관 AND 모멘텀 통제 후 |ρ| ≥ 0.2")
    add("=" * 96)
    add(f"  {'지표':16}{'전방3M':>9}{'전방6M':>9}{'전방12M':>9}"
        f"{'동시점':>9}{'Δ가격과':>9}{'모멘텀통제':>11}  판정")
    add("  " + "-" * 92)
    g = lambda t: f"{t[0]:+.3f}" if t and t[0] is not None else "—"
    h = lambda v: f"{v:+.3f}" if isinstance(v, float) else "—"
    for r in rows:
        add(f"  {r['name']:16}"
            f"{g(r['f3']):>9}{g(r['f6']):>9}{g(r['f12']):>9}"
            f"{h(r['con']):>9}{h(r['with_price']):>9}{h(r['ex_mom']):>11}"
            f"  {'통과 ✓' if r['passed'] else '기각 ✗'}")
    add("")
    add("  국면별 3개월 전방 (부호가 뒤집히면 잡음):")
    for r in rows:
        cur = "  ".join(f"{k}:{(v[0] if v[0] is not None else float('nan')):+.2f}"
                        for k, v in r["eras"].items())
        add(f"    {r['name']:16} {cur}   {'부호 일관' if r['stable'] else '**부호 뒤집힘**'}")

    # 가격의 그림자 자동 탐지
    # '동시점이 전방보다 압도적'이면 그림자로 본다. 고정 임계 하나로는 못 잡는다 —
    # 동시점 0.78·전방 0.28 인 MVRV 도 걸러야 하기 때문이다.
    shadows = [r for r in rows
               if r["con"] is not None and abs(r["con"]) >= SHADOW_CONTEMP
               and abs(r["con"]) >= 2.0 * max(r["best"], 0.01)]
    add("")
    if shadows:
        add("  ⚠ [가격의 그림자] 동시점은 큰데 전방 예측력이 없는 지표:")
        for r in shadows:
            add(f"    {r['name']}  동시점={h(r['con'])}  통제후={h(r['pv'])}/{h(r['pn'])}"
                f"  전방최대={r['best']:.3f}  **Δ가격과 {h(r['with_price'])}**")
        add("     → 통제를 통과한 것은 신호라서가 아니라 **가격 자신이라 나스닥과")
        add("        무관**하기 때문이다. 통제 통과는 필요조건이지 충분조건이 아니다.")
    else:
        add("  [가격의 그림자] 해당 없음.")

    npass = sum(1 for r in rows if r["passed"])
    add("")
    add(f"  통과 {npass} / {len(rows)}")
    add("")
    add("  ※ 여기서 '기각'은 **수익률 예측력이 없다**는 뜻이지, 지표가 쓸모없다는 뜻이")
    add("     아니다. BCS 는 이 지표들을 '예측'이 아니라 **위치(4년 롤링 백분위)** 로")
    add("     쓴다 — 이 결과는 그 설계를 반박하지 않고 오히려 뒷받침한다(docs/29).")
    add("=" * 96)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="온체인 지표 ↔ 비트코인 연관성 검정")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--macro", default="data/macro.csv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--extended", action="store_true",
                    help="외부 아카이브(coinmetrics·펀딩비)에서 후보를 더 받아 함께 검정")
    args = ap.parse_args(argv)

    market = load_market(args.csv)
    if "price" not in market:
        raise SystemExit(f"price 열이 없습니다: {args.csv}")
    try:
        macro = mc.load_macro(args.macro)
    except SystemExit:
        macro = {}
    text = report(analyse(market, macro, extended=args.extended))
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
