#!/usr/bin/env python3
"""'유명 인물' ↔ 비트코인 — 이벤트 스터디에서 **사후편향**을 걷어내는 검정.

    python3 tools/event_study.py

## 이 검정이 다루는 세 번째 실패 모드

거시 검정(docs/27·28)은 **위험자산 베타**를, 온체인 검정(docs/29)은 **가격의
그림자**를 걸러냈다. 인물 사건은 다른 함정을 가진다 — **사후편향(hindsight)**.

사건 목록을 사람이 고르면, 고르는 순간 이미 **"시장을 움직였기 때문에 기억나는"
날짜**만 뽑힌다. 그 목록으로 "사건일에 변동이 컸다"를 보이는 것은 발견이 아니라
**선택 과정을 되풀이해 잰 것**이다. 무작위 대비 p 값도 그만큼 부풀려진다.

## 그래서 사건을 두 갈래로 나눈다

  [예정]  일정이 **사전에 공표된** 사건 — 선거일, 컨퍼런스 기조연설, SEC 결정
          마감시한, 분기 실적 발표. 날짜 선택에 사후정보가 들어갈 수 없다.
          **이것만이 공정한 검정이다.**
  [돌발]  트윗·갑작스러운 공시·파산 등. 사후에 고른 것이므로 **기준선이 아니라
          참고**로만 본다.

실측 결론(docs/30): **예정 사건은 무작위 날과 구별되지 않는다(p≈0.23).** '유의미한'
결과는 전부 돌발(사후선택) 집합에서 나왔다.

## 그리고 애초에 신호가 아니다

설령 반응이 컸어도 **트윗을 미리 알 수 없다.** 이 검정이 답하는 것은 "인물이 가격을
움직였는가"(영향)이지 "그것으로 미리 살 수 있는가"(신호)가 아니다. 사이클 도구에
넣을 수 있는 것은 후자뿐이다.
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import onchain_correlation as oc      # noqa: E402

# (인물, 날짜, 사건, 종류)  종류: scheduled = 일정이 사전 공표된 것
# 날짜는 공개 보도로 확정했다(사후에 가격을 보고 고르지 않았다).
EVENTS: list[tuple[str, str, str, str]] = [
    ("일론 머스크",     "2021-02-08", "테슬라 15억달러 BTC 매입 공시", "surprise"),
    ("일론 머스크",     "2021-05-12", "테슬라 BTC 결제 중단 트윗",     "surprise"),
    ("일론 머스크",     "2022-07-20", "보유분 75% 매각(분기 실적일)",  "scheduled"),
    ("마이클 세일러",   "2020-08-11", "마이크로스트래티지 첫 매입",     "surprise"),
    ("마이클 세일러",   "2024-11-18", "55,000 BTC 최대 매입",         "surprise"),
    ("도널드 트럼프",   "2024-07-27", "비트코인 컨퍼런스 기조연설",     "scheduled"),
    ("도널드 트럼프",   "2024-11-06", "미 대선 승리 확정",            "scheduled"),
    ("도널드 트럼프",   "2025-03-06", "전략적 비트코인 준비금 행정명령", "surprise"),
    ("래리 핑크",       "2023-06-15", "블랙록 현물 ETF 신청",         "surprise"),
    ("래리 핑크",       "2024-01-10", "SEC 현물 ETF 승인(마감시한)",  "scheduled"),
    ("샘 뱅크먼-프리드", "2022-11-08", "FTX 출금 중단",               "surprise"),
    ("샘 뱅크먼-프리드", "2022-11-11", "FTX 파산 신청",               "surprise"),
    ("게리 겐슬러",     "2023-06-05", "SEC, 바이낸스 제소",           "surprise"),
]


def _nearest(target: date, idx: dict[date, int], span: int = 5) -> Optional[date]:
    """거래일이 아닐 수 있으니 가장 가까운 관측일로 맞춘다."""
    for k in range(span + 1):
        for s in (1, -1):
            c = target + timedelta(days=k * s)
            if c in idx:
                return c
    return None


def analyse(price: dict[date, float], events=EVENTS) -> dict:
    ds = sorted(price)
    idx = {d: i for i, d in enumerate(ds)}

    # 귀무분포는 **사건이 실제로 놓인 기간**에서만 뽑는다. 비트코인의 일간
    # 변동성은 2011년 5.8% → 2023년 1.5% 로 크게 줄었는데, 전 구간(2010~)에서
    # 뽑으면 2020~2025년 사건의 반응이 훨씬 조용한 시대의 표본과 비교돼 p 가
    # 과장된다. 이 파일은 아래 시대 분리에서 같은 이유로 초기 구간을 이미
    # 빼고 있었는데, 정작 p 를 만드는 풀에는 그 원칙이 빠져 있었다.
    ev_years = [date.fromisoformat(d).year for _, d, _, _ in events]
    lo_year = min(ev_years) if ev_years else ds[0].year
    daily_abs = [abs((price[ds[i]] / price[ds[i - 1]] - 1) * 100)
                 for i in range(1, len(ds))
                 if price[ds[i - 1]] > 0 and ds[i].year >= lo_year]

    rows = []
    for who, dstr, what, kind in events:
        t = _nearest(date.fromisoformat(dstr), idx)
        if t is None:
            continue
        i = idx[t]
        if i == 0:
            continue
        d0 = (price[t] / price[ds[i - 1]] - 1) * 100
        f7 = (price[ds[min(i + 7, len(ds) - 1)]] / price[t] - 1) * 100
        f30 = (price[ds[min(i + 30, len(ds) - 1)]] / price[t] - 1) * 100
        rows.append(dict(who=who, date=t, what=what, kind=kind,
                         d0=d0, f7=f7, f30=f30))
    return {"rows": rows, "daily_abs": daily_abs, "ds": ds, "idx": idx}


def _perm_p(sample: list[float], pool: list[float], seed: int = 7,
            n: int = 20000) -> float:
    """무작위로 같은 개수를 뽑았을 때 관측만큼 큰 평균이 나올 확률."""
    if not sample:
        return float("nan")
    rng = random.Random(seed)
    obs = statistics.mean(sample)
    worse = sum(1 for _ in range(n)
                if statistics.mean(rng.sample(pool, len(sample))) >= obs)
    # (worse+1)/(n+1) — 관측 자신을 귀무분포의 한 표본으로 센다. 그냥 worse/n
    # 으로 두면 한 번도 못 넘었을 때 p=0.0000 이 되어, 표본이 뒷받침할 수 없는
    # 확신을 주장하게 된다.
    return (worse + 1) / (n + 1)


def report(res: dict) -> str:
    rows, pool, ds = res["rows"], res["daily_abs"], res["ds"]
    L: list[str] = []
    add = L.append
    add("=" * 88)
    add("  '유명 인물' 사건 ↔ 비트코인 — 사후편향을 걷어낸 이벤트 스터디")
    add("=" * 88)
    add(f"  {'인물':16}{'날짜':12}{'구분':8}{'당일':>8}{'+7일':>9}{'+30일':>9}  사건")
    add("  " + "-" * 84)
    for r in sorted(rows, key=lambda x: x["date"]):
        k = "예정" if r["kind"] == "scheduled" else "돌발"
        add(f"  {r['who']:16}{str(r['date']):12}{k:8}"
            f"{r['d0']:+7.1f}%{r['f7']:+8.1f}%{r['f30']:+8.1f}%  {r['what']}")

    base = statistics.mean(pool)
    add("")
    add(f"  기준선: 전체 일간 평균 |수익률| = {base:.2f}%  (n={len(pool)}일)")
    add("")
    add("  [공정한 검정] 일정이 사전 공표된 '예정' 사건만 — 날짜 선택에 사후정보 없음")
    for kind, label in (("scheduled", "예정"), ("surprise", "돌발(사후선택)")):
        sub = [abs(r["d0"]) for r in rows if r["kind"] == kind]
        if not sub:
            continue
        p = _perm_p(sub, pool)
        verdict = ("무작위와 구별 안 됨" if p > 0.10 else "무작위보다 큼")
        add(f"    {label:14} {len(sub)}건  평균 {statistics.mean(sub):5.2f}%"
            f"   순열검정 p = {p:.4f}   → {verdict}")
    add("")
    add("    ※ '돌발'이 유의하게 나오는 것은 발견이 아니다 — 시장을 움직였기 때문에")
    add("       기억나는 날짜만 고른 결과이므로, 선택 과정을 되풀이해 잰 값이다.")

    # 현대 구간의 '가장 큰 날'과 겹치는지 (초기 폭등기는 변동이 달라 제외)
    add("")
    add("  [현대 구간 대조] 초기 폭등기를 빼고 변동 상위 20일과 겹치는지")
    evd = {r["date"] for r in rows}
    prices = res["price_map"]
    for lo in (2017, 2020):
        mv = [(abs(math.log(prices[ds[i]] / prices[ds[i - 1]])), ds[i])
              for i in range(1, len(ds))
              if ds[i].year >= lo and prices[ds[i - 1]] > 0 and prices[ds[i]] > 0]
        mv.sort(reverse=True)
        top = {d for _, d in mv[:20]}
        hit = sorted(top & evd)
        add(f"    {lo}년 이후 상위 20일 중 겹침: {len(hit)}일"
            + (f"  {[str(x) for x in hit]}" if hit else ""))
    add("    ※ 전 구간으로 재면 상위 20일이 전부 2010~11년(하루 30~50% 변동)이라")
    add("       '겹침 0'이 자동으로 나온다 — 그건 인물과 무관한 초기 변동성 착시다.")

    big = [r for r in rows if abs(r["d0"]) >= 2 * base]
    add("")
    add(f"  [지속성] 당일 충격이 큰 사건({len(big)}건)에서 방향이 이어졌는가")
    if big:
        same7 = sum(1 for r in big if r["d0"] * r["f7"] > 0)
        # **관측된 사건의 확률**을 찍는다. 예전에는 '전부 같은 면이 나올 확률'을
        # 찍었는데, 관측이 4/5 인데 5/5 의 확률을 대는 것은 일어나지도 않은 사건의
        # 확률이다. 게다가 그 값(6.2%)이 더 작아서 우연 가능성을 실제보다 작게
        # 보이게 했다 — 기각하는 쪽 결론이라 눈에 안 띄었을 뿐, 방향이 틀렸다.
        # 지금은 '일치가 관측만큼 이상 나올 확률'(단측 이항)이다.
        n = len(big)
        p_obs = sum(math.comb(n, i) for i in range(same7, n + 1)) / 2 ** n
        add(f"    +7일 같은 방향 {same7}/{n}   "
            f"(동전 {n}번에서 일치가 {same7}건 이상 나올 확률 = {p_obs * 100:.1f}%)")
        add("    → 표본이 이렇게 작으면 우연으로도 흔히 나온다. 근거로 쓸 수 없다.")

    add("")
    add("  [근본 한계] 설령 반응이 컸어도 **트윗을 미리 알 수 없다.** 이 검정은")
    add("     '인물이 가격을 움직였는가'(영향)를 재지, '미리 살 수 있는가'(신호)를")
    add("     재지 않는다. 사이클 도구에 넣을 수 있는 것은 후자뿐이다.")
    add("=" * 88)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="인물 사건 ↔ 비트코인 이벤트 스터디")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    market = oc.load_market(args.csv)
    if "price" not in market:
        raise SystemExit(f"price 열이 없습니다: {args.csv}")
    res = analyse(market["price"])
    res["price_map"] = market["price"]
    text = report(res)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
