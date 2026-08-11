#!/usr/bin/env python3
"""비트코인 트레저리 기업 이벤트 검정 — "기업이 사겠다고 공시하면 오르는가".

    python3 tools/treasury_study.py            # 전체 (순열 10,000회)
    python3 tools/treasury_study.py --quick    # 요일효과 순열만 1,000회로 빠르게

## 이 검정의 함정 두 개 — 사후편향, 그리고 **내생성**

인물 사건 검정(docs/30, tools/event_study.py)의 함정은 사후편향이었다. 트레저리
축은 그걸 물려받고 하나를 더 가진다 — **내생성(역인과)**.

> 트레저리 기업은 BTC 상승기에 등장한다. 2020-08(MicroStrategy) 이전 10년 동안
> 한 건도 없다가 2020~2021 강세장에 11건, 2024~2025 강세장에 10건이 몰렸다.
> **가격이 올라서 기업이 생긴 것**일 수 있는데, 그 기업의 공시일 근처 수익률을 재면
> "기업이 사서 올랐다"로 읽힌다. 원인과 결과가 뒤집힌다.

그래서 이 도구는 공시 **후** 창만 재지 않고 공시 **전 60일** 수익률도 같이 잰다
(역인과의 직접 측정 — 발표 전에 이미 올라 있었다면 그게 내생성의 증거다). 창 수익률은
원수익률과 **나스닥 차감 초과수익** 둘 다 낸다 — 트레저리 공시가 몰린 시기는 위험자산
전반의 강세기라, 나스닥을 빼지 않으면 시장 베타를 기업 효과로 오독한다(docs/25~28).

## 사후편향은 kind 로 다룬다

  surprise  사전 신호 없던 공시 — 첫 채택, 갑작스러운 대형 매수.
  flagged   자금조달 발표·법 통과 등으로 **매수가 이미 예고된 뒤**의 공시.
            예고된 매수의 공시일에 반응이 있다면 그건 새 정보가 아니라
            이미 가격에 있던 정보를 다시 잰 것이다.

## 귀무분포는 원형 이동(circular shift)으로 만든다

단순 셔플은 BTC 수익률의 자기상관을 파괴해 p 를 16배까지 부풀린다(docs/31 의
교훈 — 셔플 0.004 vs 순환이동 0.065). 이벤트 날짜들을 표본 안에서 통째로 s일
민다 — 이벤트 사이 간격과 수익률의 시간 구조가 모두 보존된다.

## 그리고 1층/2층을 섞지 않는다 (docs/33)

이 검정의 주 질문은 1층("관련이 있는가")이다. 설령 surprise 공시에 반응이
실재해도, 공시를 미리 알 수 없으므로 2층("모델에 넣을 수 있는가")은 자동으로
성립하지 않는다 — 인물 축(docs/30)과 같은 구조다.
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

import event_study as es              # noqa: E402  (_nearest 재사용)
import macro_correlation as mc        # noqa: E402  (load_macro 재사용)
import onchain_correlation as oc      # noqa: E402  (load_market 재사용)

# ---------------------------------------------------------------------------
# 이벤트 목록 — 날짜는 전부 공개 보도로 확정했다 (출처 URL 을 주석에 남긴다).
# 검증 못 한 후보는 싣지 않았다. (date, name, cls, kind, note)
#   cls : company / country
#   kind: surprise = 사전 신호 없던 공시
#         flagged  = 자금조달 발표·법 통과 등으로 매수가 예고된 뒤의 공시.
#                    **언론 유출·사전 보도도 사전 신호로 센다** — FT 유출(Trump
#                    Media·Twenty One)과 CNBC 사전 보도(GameStop)를 같은 잣대로.
#                    이 기준이 무너지면 검정이 사후편향 재생산기가 된다.
# ---------------------------------------------------------------------------
EVENTS: list[tuple[str, str, str, str, str]] = [
    # coindesk.com/markets/2020/08/11/microstrategy-buys-250m-in-bitcoin-calling-the-crypto-superior-to-cash
    ("2020-08-11", "MicroStrategy 첫 매수 $250M", "company", "surprise",
     "상장기업 최초의 트레저리 채택"),
    # cnbc.com/2020/10/08/square-buys-50-million-in-bitcoin-says-cryptocurrency-aligns-with-companys-purpose.html
    ("2020-10-08", "Square $50M (4,709 BTC)", "company", "surprise", "두 번째 상장기업"),
    # decrypt.co/52163/microstrategy-completes-650-million-bitcoin-buy — 전환사채 조달은
    # 12-07~11 에 공표됐다(sec.gov 8-K): 매수가 예고된 상태였다.
    ("2020-12-21", "MicroStrategy 29,646 BTC $650M", "company", "flagged",
     "12월 전환사채 $650M 로 예고된 매수"),
    # globenewswire.com/news-release/2021/01/25/2163436/0/en/Marathon-Invests-150-Million-in-Bitcoin.html
    ("2021-01-25", "Marathon $150M", "company", "surprise", "채굴사의 현금 매수"),
    # cnbc.com/2021/02/08/tesla-buys-1point5-billion-in-bitcoin.html (10-K 공시)
    ("2021-02-08", "Tesla $1.5B", "company", "surprise", "event_study 와 공유하는 사건"),
    # strategy.com/press/microstrategy-acquires-additional-19452-bitcoins-for-1-026-billion_02-24-2021
    # — 전환사채 $1.05B 완료가 02-19 에 공표됐다: 매수가 예고된 상태였다.
    ("2021-02-24", "MicroStrategy 19,452 BTC $1.026B", "company", "flagged",
     "2월 전환사채 $1.05B 로 예고된 매수"),
    # coindesk.com/markets/2021/03/07/software-firm-meitu-buys-22m-of-ether-179m-bitcoin-for-its-treasury
    ("2021-03-07", "Meitu $40M (BTC+ETH)", "company", "surprise", "홍콩 상장사"),
    # news.cision.com/aker-asa/r/aker-asa-launches-seetee-to-invest-in-bitcoin-and-blockchain-technology,c3301475
    ("2021-03-08", "Aker ASA(Seetee) $58M", "company", "surprise", "노르웨이 상장사"),
    # coindesk.com/markets/2021/04/28/asian-video-game-publisher-nexon-buys-100m-in-bitcoin
    # (businesswire 배포 04-27 미국시각 = 04-28 JST)
    ("2021-04-28", "Nexon $100M", "company", "surprise", "일본 상장사"),
    # coindesk.com/business/2021/06/05/president-of-el-salvador-says-hes-submitting-bill-to-make-bitcoin-legal-tender
    ("2021-06-05", "엘살바도르 법정통화 추진 발표", "country", "surprise",
     "마이애미 컨퍼런스 영상 발표(토요일)"),
    # cnbc.com/2021/09/07/el-salvador-buys-400-bitcoin-ahead-of-law-making-it-legal-currency.html
    # — 법은 06-08 통과, 09-07 발효가 예고돼 있었다: 매수(09-06 트윗)는 예고된 수순.
    ("2021-09-06", "엘살바도르 첫 400 BTC 매수", "country", "flagged",
     "법정통화 발효(09-07) 전날, 6월 법 통과로 예고됨"),
    # 채택 공시는 2024-04-08(JST 오후 = UTC 4-08) — 다수 후속 보도(ccn·
    # bitcoinmagazine 등)가 4-08 로 기록하고 서구 보도가 4-09 에 몰린다.
    # 4-08 로 확정한다. 당일 수익이 날짜에 민감(4-08 +3.3% vs 4-09 −3.7%)하다는
    # 것 자체가 ±1일 창을 함께 봐야 하는 이유다(적대적 검증의 지적).
    ("2024-04-08", "Metaplanet 트레저리 채택", "company", "surprise",
     "일본 상장사, 이후 반복 매수의 시작"),
    # cnbc.com/2024/05/28/med-tech-stock-semler-scientific-surges-on-new-bitcoin-treasury-strategy.html
    ("2024-05-28", "Semler Scientific $40M (581 BTC)", "company", "surprise",
     "의료기기사의 채택"),
    # coindesk.com/business/2024/10/30/michael-saylors-microstrategy-plans-to-raise-42b-to-buy-more-bitcoin-over-next-3-years
    # — 분기 실적일에 나온 '조달 계획'. 매수 자체가 아니라 매수의 예고라 flagged.
    ("2024-10-30", "MicroStrategy 21/21 계획 ($42B 조달)", "company", "flagged",
     "실적발표일 공시, 이후 매수들의 예고"),
    # strategy.com/press/microstrategy-announces-btc-atm-activity-raised-2-billion-purchased-27200-btc-now-holds-279420-btc-with-btc-yield-of-26-ytd
    ("2024-11-11", "MicroStrategy 27,200 BTC $2.03B", "company", "flagged",
     "21/21 계획으로 예고된 매수, 월요일 공시 시대의 시작"),
    # investing.com/news/stock-market-news/microstrategy-acquires-another-55500-bitcoins-3739614
    # — 매수 기간은 11-18~24, **공시는 11-25**. event_study 의 11-18 은 매수 시작일이다.
    ("2024-11-25", "MicroStrategy 55,500 BTC $5.4B", "company", "flagged",
     "최대 단일 매수, 전환사채 $3B 로 예고 (Rumble 과 같은 날)"),
    # cnbc.com/2024/11/25/rumble-plans-to-buy-up-to-20-million-in-bitcoin-in-new-treasury-strategy.html
    ("2024-11-25", "Rumble 최대 $20M", "company", "surprise",
     "MicroStrategy 55,500 BTC 공시와 같은 날 — 당일 창이 겹친다"),
    # globenewswire.com/news-release/2025/03/25/3049165/0/en/GameStop-Announces-Update-to-its-Investment-Policy-to-Add-Bitcoin-as-a-Treasury-Reserve-Asset.html
    # — 단 CNBC 가 2-13 에 '비트코인 투자 검토'를 보도해 GME 가 8%대 급등했다
    #   (cnbc.com/2025/02/13). '언론에 먼저 알려진 매수'는 flagged 다 — 적대적
    #   검증이 잡은 자체 기준(Trump Media) 위반을 바로잡았다.
    ("2025-03-25", "GameStop 이사회 승인", "company", "flagged",
     "2-13 CNBC 사전 보도로 예고 — 정책 채택(매수는 5월)"),
    # businesswire.com/news/home/20250423962305/en/ (Cantor·Tether·SoftBank)
    # — FT 가 4-22 에 동일 딜($3B, 21 Capital 명칭까지)을 먼저 보도했고 로이터가
    #   당일 받아썼다(kfgo.com 2025/04/22). Trump Media 와 같은 구조 = flagged.
    ("2025-04-23", "Twenty One Capital 출범 (42,000 BTC)", "company", "flagged",
     "FT 4-22 유출로 예고 — SPAC 합병으로 3위 보유 기업 등장"),
    # coindesk.com/markets/2025/05/27/trump-media-raising-25b-for-bitcoin-treasury-strategy
    # — FT 가 05-26 에 $3B 조달 계획을 먼저 보도했다(fxstreet 202505262123): 예고됨.
    ("2025-05-27", "Trump Media $2.5B 조달", "company", "flagged",
     "전날 FT 보도로 유출된 뒤의 공식 공시"),
    # investor.gamestop.com/news-releases/news-details/2025/GameStop-Announces-Purchase-of-Bitcoin/default.aspx
    ("2025-05-28", "GameStop 4,710 BTC $513M", "company", "flagged",
     "3월 정책 채택 + 전환사채 $1.3B 로 예고된 매수"),
]

KINDS = ("surprise", "flagged")

# 귀무분포를 만들 표본 구간의 시작. 전 구간(2010~)으로 원형 이동하면 하루 30~50%
# 움직이던 초기 폭등기가 귀무분포에 섞여 판정이 그쪽 변동성에 끌려간다(docs/30 의
# '초기 변동성 착시'와 같은 문제). 이벤트가 전부 2020-08 이후이므로 그 앞 1년 남짓의
# 현대 구간부터 자른다.
STUDY_START = date(2019, 1, 1)

# MicroStrategy(현 Strategy)가 매주 월요일 8-K 로 매수를 공시하던 시대의 시작.
# 2024-11-11 첫 대형 월요일 공시(27,200 BTC) 이후 월요일 공시가 관행이 됐다
# (fortune.com/crypto/2024/11/11/, kucoin.com/news/flash/michael-saylor-teases-
# another-bitcoin-purchase-for-strategy-on-monday). 2026-07 에 5주 연속 매수 중단이
# 보도됐지만(stocktwits) **공시 관행 자체는 표본 끝까지 이어진다**고 보고, 시대의
# 끝은 표본 끝으로 둔다.
MSTR_ERA_START = date(2024, 11, 11)

# 이벤트 창: (라벨, 시작 오프셋 a, 끝 오프셋 b) — 공시일 i 기준 i+a ~ i+b 일의
# 로그수익률 합. 당일 [0], 전후 [-1..+1], 이후 [0..+5].
WINDOWS = (("당일 [0]", 0, 0), ("[-1..+1]", -1, 1), ("[0..+5]", 0, 5))


# ---------------------------------------------------------------------------
# 그리드 준비 — BTC 일간 격자 위에서 모든 창을 O(1) 로 계산할 수 있게 만든다
# ---------------------------------------------------------------------------
def build_grid(price: dict[date, float], ndq: Optional[dict[date, float]] = None,
               start: date = STUDY_START) -> dict:
    """BTC 로그가격 배열 + 각 날짜에 가장 가까운 나스닥 관측(±5일) 인덱스.

    나스닥은 주말·휴장일이 비므로 정확한 날짜 교집합 대신 '가장 가까운 관측'으로
    잇는다. 초과수익 창은 BTC 창의 양끝 달력 날짜를 나스닥 격자에 각각 사영해
    같은 달력 구간의 나스닥 로그수익률을 뺀다.
    """
    ds = [d for d in sorted(price) if d >= start and price[d] > 0]
    logp = [math.log(price[d]) for d in ds]
    grid = {"ds": ds, "logp": logp, "idx": {d: i for i, d in enumerate(ds)}}
    if ndq:
        nds = [d for d in sorted(ndq) if ndq[d] > 0]
        logq = [math.log(ndq[d]) for d in nds]
        # 사영은 '가장 가까운'이 아니라 **'그 날 이전 마지막'** 관측으로 한다.
        # 가까운 관측을 고르면 일요일이 월요일 관측으로 앞당겨져, 월요일 공시
        # (21건 중 10건!)의 당일 초과수익이 ja==jb 로 퇴화해 원수익과 같아진다
        # — 적대적 검증이 잡았다. 이전 관측으로 사영하면 [일~월] 창이 [금~월]
        # 나스닥 수익률을 빼게 되어 차감이 실제로 작동한다.
        near: list[Optional[int]] = []
        j = -1
        for d in ds:
            while j + 1 < len(nds) and nds[j + 1] <= d:
                j += 1
            near.append(j if j >= 0 and (d - nds[j]).days <= 5 else None)
        grid["logq"] = logq
        grid["near"] = near
    return grid


def _win(grid: dict, i: int, a: int, b: int) -> Optional[float]:
    """공시일 i 기준 [i+a .. i+b] 일의 BTC 로그수익률 합(%). 창이 표본을 벗어나면 None."""
    lo, hi = i + a - 1, i + b
    logp = grid["logp"]
    if lo < 0 or hi >= len(logp):
        return None
    return (logp[hi] - logp[lo]) * 100.0


def _win_excess(grid: dict, i: int, a: int, b: int) -> Optional[float]:
    """같은 달력 구간의 나스닥 로그수익률을 뺀 초과수익(%). 나스닥이 없으면 None."""
    raw = _win(grid, i, a, b)
    near = grid.get("near")
    if raw is None or near is None:
        return None
    ja, jb = near[i + a - 1], near[i + b]
    if ja is None or jb is None:
        return None
    return raw - (grid["logq"][jb] - grid["logq"][ja]) * 100.0


# ---------------------------------------------------------------------------
# 분석 — 이벤트 창 + 공시 전 60일(내생성 프로브)
# ---------------------------------------------------------------------------
def analyse(price: dict[date, float], ndq: Optional[dict[date, float]] = None,
            events=EVENTS, start: date = STUDY_START) -> dict:
    grid = build_grid(price, ndq, start)
    rows = []
    for dstr, name, cls, kind, note in events:
        t = es._nearest(date.fromisoformat(dstr), grid["idx"])
        if t is None:                      # 표본 밖 → 조용히 건너뛴다
            continue
        i = grid["idx"][t]
        row = dict(date=t, name=name, cls=cls, kind=kind, note=note, i=i,
                   raw=[_win(grid, i, a, b) for _, a, b in WINDOWS],
                   exc=[_win_excess(grid, i, a, b) for _, a, b in WINDOWS],
                   pre60=_win(grid, i, -60, -1))
        rows.append(row)
    return {"grid": grid, "rows": rows}


def _subset_means(rows: list[dict], which: str) -> dict[tuple, Optional[float]]:
    """(subset, window, raw/exc) → 평균. 창이 None 인 이벤트는 그 창에서만 빠진다."""
    out: dict[tuple, Optional[float]] = {}
    for sub in (*KINDS, "all"):
        picked = [r for r in rows if sub == "all" or r["kind"] == sub]
        for wi, (label, _, _) in enumerate(WINDOWS):
            for fam in ("raw", "exc"):
                vals = [r[fam][wi] for r in picked if r[fam][wi] is not None]
                out[(sub, label, fam)] = statistics.mean(vals) if vals else None
        pre = [r["pre60"] for r in picked if r["pre60"] is not None]
        out[(sub, "전60일", "raw")] = statistics.mean(pre) if pre else None
    return out


def perm_test(res: dict, n: int = 10000, seed: int = 7) -> dict:
    """원형 이동 귀무분포 — 이벤트 날짜 묶음을 통째로 s일 민다.

    간격 구조와 수익률의 자기상관이 모두 보존된다(단순 셔플과의 차이는 docs/31).
    p 는 단측: 귀무분포에서 관측 평균 이상이 나온 비율(매수 공시=호재 가설이
    양(+)의 방향이므로). 전60일도 단측 — 역인과 가설도 '발표 전에 올라 있었다'는
    양의 방향이다.
    """
    grid, rows = res["grid"], res["rows"]
    N = len(grid["ds"])
    # **구별 가능한 이동을 전수로 돈다.** 원형이동으로 만들 수 있는 서로 다른
    # 귀무 표본은 N-122개(≈2,600)뿐인데, 예전에는 그걸 10,000회 복원추출하고
    # 분모에 10,000을 썼다. 없는 정밀도를 주장하는 셈이라 p 가 0.000 으로
    # 인쇄됐고(docs/34 는 같은 수치를 0.0004 로 적는다), --quick 여부에 따라
    # 값이 달라지기까지 했다. 전수는 10,000회보다 **오히려 싸다**.
    obs = _subset_means(rows, "obs")
    shifts = range(61, N - 61)
    ge = {k: 0 for k, v in obs.items() if v is not None}   # null >= obs 횟수
    cnt = {k: 0 for k in ge}                                # 유효 null 횟수
    for s in shifts:
        # 버퍼 61일 — 창 폭(전60일)보다 짧은 버퍼(±30)는 관측 창과 최대 30일
        # 겹치는 널을 허용한다(적대적 검증). 겹침 없는 이동만 센다.
        shifted = []
        for r in rows:
            i = (r["i"] + s) % N
            shifted.append(dict(
                kind=r["kind"],
                raw=[_win(grid, i, a, b) for _, a, b in WINDOWS],
                exc=[_win_excess(grid, i, a, b) for _, a, b in WINDOWS],
                pre60=_win(grid, i, -60, -1)))
        nm = _subset_means(shifted, "null")
        for k in ge:
            v = nm.get(k)
            if v is None:
                continue
            cnt[k] += 1
            if v >= obs[k]:
                ge[k] += 1
    # (ge+1)/(cnt+1) — 관측 자신을 귀무분포의 한 표본으로 세는 표준 보정.
    # 이제 cnt 는 실제로 센 서로 다른 이동의 수라, p 의 하한이 검정의 진짜
    # 해상도(1/(N-121))와 일치한다.
    pvals = {k: ((ge[k] + 1) / (cnt[k] + 1) if cnt[k] else float("nan")) for k in ge}
    return {"obs": obs, "p": pvals, "n": len(shifts)}


# ---------------------------------------------------------------------------
# MSTR 주간매수 시대의 요일 효과 — 월요일(UTC) vs 다른 요일, 시대 전후 대조
# ---------------------------------------------------------------------------
def weekday_study(price: dict[date, float], era_start: date = MSTR_ERA_START,
                  era_end: Optional[date] = None, n: int = 10000,
                  seed: int = 13) -> dict:
    """시대 안의 월요일 평균수익 − 비월요일 평균수익, 그리고 같은 길이의 직전
    구간에서 같은 값. 시대에만 있고 직전엔 없어야 '매수 공시 요일 효과'다.

    p 는 라벨 재표집 순열(월요일 개수만큼 무작위로 뽑아 차이를 재기)의 양측 —
    요일은 위·아래 어느 쪽 효과든 주장이 되므로 양측으로 잰다. 일간 수익률의
    자기상관은 월간 격자(docs/31)만큼 크지 않아 재표집으로 충분하다.
    """
    ds = sorted(d for d in price if price[d] > 0)
    if era_end is None:
        era_end = ds[-1]
    rets = {ds[k]: math.log(price[ds[k]] / price[ds[k - 1]]) * 100.0
            for k in range(1, len(ds))}
    span = (era_end - era_start).days
    pre_end = era_start - timedelta(days=1)
    pre_start = pre_end - timedelta(days=span)
    rng = random.Random(seed)

    def one(lo: date, hi: date) -> Optional[dict]:
        mon = [v for d, v in rets.items() if lo <= d <= hi and d.weekday() == 0]
        oth = [v for d, v in rets.items() if lo <= d <= hi and d.weekday() != 0]
        if len(mon) < 8 or len(oth) < 8:
            return None
        obs = statistics.mean(mon) - statistics.mean(oth)
        pool = mon + oth
        hits = 0
        for _ in range(n):
            pick = rng.sample(pool, len(mon))
            rest_sum = sum(pool) - sum(pick)
            d_ = statistics.mean(pick) - rest_sum / len(oth)
            if abs(d_) >= abs(obs):
                hits += 1
        return dict(mon=statistics.mean(mon), oth=statistics.mean(oth),
                    diff=obs, n_mon=len(mon), n_oth=len(oth), p=hits / n)
    return {"era": one(era_start, era_end), "pre": one(pre_start, pre_end),
            "era_span": (era_start, era_end), "pre_span": (pre_start, pre_end)}


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------
def report(res: dict, pt: dict, wd: dict) -> str:
    rows = res["rows"]
    L: list[str] = []
    add = L.append
    add("=" * 96)
    add("  트레저리 기업 이벤트 ↔ 비트코인 — 내생성·사후편향을 정면에 둔 이벤트 스터디")
    add("=" * 96)
    add(f"  {'날짜':12}{'구분':10}{'당일':>8}{'초과':>8}{'[0..+5]':>9}{'전60일':>9}  이벤트")
    add("  " + "-" * 92)
    for r in sorted(rows, key=lambda x: x["date"]):
        k = "돌발" if r["kind"] == "surprise" else "예고"
        k += "·국가" if r["cls"] == "country" else ""
        e0 = r["exc"][0]

        def f(v, w=7):
            return f"{v:+{w}.1f}%" if v is not None else " " * w + "—"
        add(f"  {str(r['date']):12}{k:10}{f(r['raw'][0])}{f(e0)}"
            f"{f(r['raw'][2], 8)}{f(r['pre60'], 8)}  {r['name']}")
    add("")
    add("  창 수익률 = 로그수익률 합(%) · 초과 = 같은 달력 구간의 나스닥 수익률 차감")
    add(f"  귀무분포 = 이벤트 날짜 원형 이동 {pt['n']:,}회 ({res['grid']['ds'][0]}~ 구간 안)")

    add("")
    # p 를 %.3f 로 찍으면 0.00038 이 '0.000' 이 된다 — 검정이 가진 것보다 큰
    # 확신을 주장하는 인쇄다(docs/34 는 같은 값을 0.0004 로 적는다). 해상도
    # 하한(1/(구별 가능한 이동+1))보다 작아지면 그 하한을 그대로 보여 준다.
    _p_floor = 1.0 / (pt.get("n", 0) + 1) if pt.get("n") else 0.0

    def fmt_p(p_: Optional[float]) -> str:
        if p_ is None:
            return "—"
        return f"{p_:.4f}" if p_ < 0.001 else f"{p_:.3f}"

    add("  [창 평균과 순열 p — 단측(양의 방향)]")
    add(f"    {'집합':12}{'창':10}{'원수익 평균':>12}{'p':>8}{'초과 평균':>12}{'p':>8}")
    add("    " + "-" * 62)
    for sub, label in (("surprise", "돌발"), ("flagged", "예고"), ("all", "전체")):
        n_sub = sum(1 for r in rows if sub == "all" or r["kind"] == sub)
        for wlabel, _, _ in WINDOWS:
            vr = pt["obs"].get((sub, wlabel, "raw"))
            ve = pt["obs"].get((sub, wlabel, "exc"))
            pr = pt["p"].get((sub, wlabel, "raw"))
            pe = pt["p"].get((sub, wlabel, "exc"))

            def g(v, p_):
                return (f"{v:+10.2f}%{fmt_p(p_):>8}" if v is not None and p_ is not None
                        else f"{'—':>11}{'—':>8}")
            add(f"    {label}({n_sub}건){'':2}{wlabel:10}{g(vr, pr)}  {g(ve, pe)}")
    add("")
    add("  ※ '예고(flagged)' 집합에 반응이 있다면 그것은 새 정보가 아니다 — 자금조달")
    add("     발표 때 이미 가격에 들어간 정보를 공시일에 다시 잰 것이다. 기준선은 돌발이다.")

    add("")
    add("  [내생성(역인과) 프로브 — 공시 **전** 60일 수익률]")
    for sub, label in (("surprise", "돌발"), ("flagged", "예고"), ("all", "전체")):
        v = pt["obs"].get((sub, "전60일", "raw"))
        p_ = pt["p"].get((sub, "전60일", "raw"))
        if v is not None:
            add(f"    {label:6} 평균 {v:+7.1f}%   순열 p = {fmt_p(p_)}"
                + (f" (검정 해상도 하한 {_p_floor:.4f})" if p_ is not None and p_ <= _p_floor else ""))
    add("    → 공시 전에 이미 올라 있었다면, '기업이 사서 올랐다'가 아니라 '올라서")
    add("      기업이 샀다'가 데이터와 부합한다. 이 축의 인과는 그래서 판정 불능이다.")

    add("")
    add("  [MSTR 주간매수 시대의 요일 효과 — 월요일(UTC) vs 다른 요일, 양측 p]")
    for key, tag in (("era", "시대"), ("pre", "직전 같은 길이")):
        o = wd[key]
        lo, hi = wd[f"{key}_span"]
        if o is None:
            add(f"    {tag}: 표본 부족")
            continue
        add(f"    {tag} ({lo}~{hi}): 월요일 {o['mon']:+.3f}% (n={o['n_mon']}) vs"
            f" 그 외 {o['oth']:+.3f}% (n={o['n_oth']})   차이 {o['diff']:+.3f}%p"
            f"   p = {o['p']:.3f}")
    if wd["era"] and wd["pre"]:
        dd = wd["era"]["diff"] - wd["pre"]["diff"]
        add(f"    시대 간 차이(시대 − 직전) = {dd:+.3f}%p")
        add("    → 시대에만 유의하게 나타나야 '매수 공시 요일 효과'다. 직전 구간에도")
        add("      비슷하면 그냥 BTC 요일 잡음이다(docs/31 의 요일 검정과 같은 기준).")

    n_tests = len(pt["p"]) + sum(1 for k in ("era", "pre") if wd[k] is not None)
    add("")
    add("  [다중검정 경고]")
    add(f"    이 리포트가 계산한 p 값은 총 {n_tests}개다. 전부 효과가 없어도 α=0.05 에서")
    add(f"    기대 오탐 ≈ {n_tests * 0.05:.1f}개 — p 하나가 0.05 아래라고 발견이 아니다.")
    add("    본페로니 기준을 넘으려면 p < " + f"{0.05 / n_tests:.4f}" + " 이어야 한다.")

    add("")
    add("  [근본 한계 — 1층과 2층 (docs/33)]")
    add("    이 검정은 1층('공시와 가격이 관련 있는가')을 잰다. 설령 관련이 실재해도")
    add("    공시를 미리 알 수 없고, 트레저리 기업 자체가 상승기의 산물이라(내생성)")
    add("    2층('모델에 넣을 수 있는가')은 별도로 성립하지 않는다 — 인물 축과 같다.")
    add("=" * 96)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="트레저리 기업 이벤트 ↔ 비트코인 검정")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--macro", default="data/macro.csv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--quick", action="store_true",
                    help="요일효과 순열을 1,000회로 줄인다. 이벤트 순열검정은 전수라 영향 없다")
    args = ap.parse_args(argv)

    market = oc.load_market(args.csv)
    if "price" not in market:
        raise SystemExit(f"price 열이 없습니다: {args.csv}")
    ndq = None
    macro_path = Path(args.macro)
    if macro_path.exists():
        ndq = mc.load_macro(str(macro_path)).get("nasdaq")
    if ndq is None:
        print("※ 나스닥 열이 없어 초과수익은 비워 둔다 (원수익률만 계산).")

    n = 1000 if args.quick else 10000
    res = analyse(market["price"], ndq)
    # 이벤트 순열검정은 구별 가능한 이동을 전수로 돈다 — n 을 받지 않는다.
    pt = perm_test(res)
    wd = weekday_study(market["price"], n=n)
    text = report(res, pt, wd)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
