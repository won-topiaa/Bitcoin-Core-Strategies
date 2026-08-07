#!/usr/bin/env python3
"""MSTR(스트래티지) ↔ 비트코인 — 상대성과와 상관.

    python3 tools/mstr_study.py            # 터미널 리포트 (데이터 없으면 그렇게 말하고 0)

주가는 data/stocks.csv(tools/fetch_stock.py 가 CI 에서 받는다), 비트코인은
data/market.csv, 나스닥 통제는 data/macro.csv 에서 읽는다. **분석만** 한다.

## 무엇을 재는가

  상관     — 주간 로그수익률 상관 + 나스닥 통제 부분상관(mc.candidate_analysis
             재사용). MSTR 는 'BTC 보유 회사'이자 나스닥 기술주라, 나스닥을
             통제해야 'BTC 와 독립으로 붙는 몫'이 보인다. 국면은 2020-08-11
             (첫 BTC 매입 공시) 전후로 가르고, 채택 후는 연 단위로 본다.
  상대성과 — 90일(달력) 롤링 로그수익률 스프레드(MSTR−BTC). 스프레드가
             OUTPERF_SPREAD(0.5 ≈ 총수익 기준 1.65배)를 넘는 날들을 연속
             구간으로 묶는다. 반대 방향(BTC 우위)도 같은 문턱으로.

## 조심할 것 — 격자와 정의

MSTR 는 주 5일, BTC 는 주 7일이다. 달력일로 나이브하게 짝지으면 주말마다
어긋나므로, 롤링 스프레드는 **두 계열의 공통 거래일**(사실상 MSTR 거래일)로만
잰다. 90일 창은 달력일 기준이되 창 안의 공통 관측만 쓰고, 창이 80일도 못 덮는
계열 시작부는 스프레드를 내지 않는다(반쪽 창은 이름만 90일이다).

구간의 경계 날짜는 창(90일)·문턱(0.5)이라는 **정의에 종속된 값**이다 — '정확한
시점'이 아니라 '이 잣대로 잰 구간'으로 읽어야 한다. 상대성과지수는 비율이라
BTC 가 빠질 때 MSTR 가 덜 빠져도 오른다 — '절대 상승'과 다르다.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import macro_correlation as mc        # noqa: E402

# 90일 롤링 로그수익률 스프레드가 이걸 넘으면 '크게 앞선' 구간으로 본다.
# 0.5 는 **로그 수익률의 차**다. e^0.5 = 1.65 이므로 "90일 동안 원금이 불어난
# **배수**가 BTC 의 1.65배"라는 뜻이다 — 두 수익률의 **차**가 65%p 라는 뜻도,
# 두 수익률의 **비**가 1.65 라는 뜻도 **아니다**. 같은 0.5 가 MSTR +200%/BTC
# +82%(격차 118%p, 수익률 비 2.4)에서도, MSTR −20%/BTC −52%(격차 32%p, 수익률
# 비는 부호가 갈려 정의조차 안 된다)에서도 나온다. 실제 관측 구간(2020-08~,
# 2,180개)에서 스프레드 0.5 가 뜻하는 %p 격차는 **26.8%p ~ 234.0%p 로 8.7배**
# 벌어지고 중앙값은 68.8%p 다. 그래서 %p 로도 '수익률의 몇 배'로도 못 적는다.
# 베타(MSTR 는 BTC 의 ~1.5배로 움직인다)만으로는
# 웬만해선 못 넘고, 프리미엄 확장 같은 국면 사건이 있어야 넘는 크기다.
OUTPERF_SPREAD = 0.5
WINDOW_DAYS = 90          # 롤링 창(달력일)
MIN_SPAN_DAYS = 80        # 창이 실제로 덮은 달력일이 이보다 짧으면(계열 시작부) 내지 않는다
ADOPTION = date(2020, 8, 11)   # MSTR 첫 BTC 매입 공시 — 국면 경계


def load_mstr(csv_path: str = "data/stocks.csv") -> Optional[dict[date, float]]:
    """data/stocks.csv 의 mstr 열. 파일이 없거나 열이 비면 None — 이 환경에선
    stooq·야후가 403 이라 파일이 없는 것이 정상 경로다(CI 가 채운다)."""
    p = Path(csv_path)
    if not p.exists():
        return None
    out: dict[date, float] = {}
    with p.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            v = (row.get("mstr") or "").strip()
            if not v:
                continue
            try:
                out[date.fromisoformat((row.get("date") or "")[:10])] = float(v)
            except ValueError:
                continue
    return out or None


# ---------------------------------------------------------------------------
# 상대성과 — 90일 롤링 스프레드와 구간
# ---------------------------------------------------------------------------
def rolling_spread(mstr: dict[date, float], btc: dict[date, float],
                   window_days: int = WINDOW_DAYS,
                   min_span: int = MIN_SPAN_DAYS) -> dict[date, float]:
    """공통 거래일 격자 위의 롤링 로그수익률 스프레드(MSTR−BTC).

    각 공통 거래일 d 에서 창 [d−window, d] 안의 **가장 오래된 공통 거래일** d0 를
    잡아 log(MSTR_d/MSTR_d0) − log(BTC_d/BTC_d0) 를 낸다. 달력일로 90일 전을
    직접 찾으면 주말·휴장일마다 어긋난다 — 창은 달력, 관측은 공통 격자."""
    days = [d for d in sorted(set(mstr) & set(btc))
            if mstr[d] > 0 and btc[d] > 0
            and math.isfinite(mstr[d]) and math.isfinite(btc[d])]
    out: dict[date, float] = {}
    lo = 0
    for d in days:
        floor = d - timedelta(days=window_days)
        while days[lo] < floor:          # 두 포인터 — days 정렬이라 lo 는 단조 증가
            lo += 1
        d0 = days[lo]
        if (d - d0).days < min_span:     # 반쪽 창(계열 시작부)은 내지 않는다
            continue
        out[d] = math.log(mstr[d] / mstr[d0]) - math.log(btc[d] / btc[d0])
    return out


def _runs(spread: dict[date, float], qualifies: Callable[[float], bool],
          better: Callable[[float, float], float]) -> list[tuple[date, date, float]]:
    """문턱을 넘는 날들을 공통 격자에서 연속 구간으로. (시작, 끝, 극값)."""
    periods: list[tuple[date, date, float]] = []
    start: Optional[date] = None
    end: Optional[date] = None
    ext = 0.0
    for d in sorted(spread):
        if qualifies(spread[d]):
            if start is None:
                start, ext = d, spread[d]
            else:
                ext = better(ext, spread[d])
            end = d
        elif start is not None:
            periods.append((start, end, ext))
            start = None
    if start is not None:
        periods.append((start, end, ext))
    return periods


def outperformance(mstr: dict[date, float], btc: dict[date, float],
                   threshold: float = OUTPERF_SPREAD) -> dict:
    """{"spread": 날짜→스프레드, "up": [(시작, 끝, 최대)], "down": [(시작, 끝, 최소)]}.

    up 은 스프레드 ≥ +threshold(MSTR 가 크게 앞섬), down 은 ≤ −threshold(BTC 가
    크게 앞섬). down 의 극값은 가장 깊은 음수다 — |값| 이 최대인 지점."""
    sp = rolling_spread(mstr, btc)
    return {"spread": sp,
            "up": _runs(sp, lambda v: v >= threshold, max),
            "down": _runs(sp, lambda v: v <= -threshold, min)}


# ---------------------------------------------------------------------------
# 상관 — candidate_analysis 재사용 + 채택 전후 국면
# ---------------------------------------------------------------------------
def analyse(mstr: dict[date, float], btc: dict[date, float],
            nasdaq: Optional[dict[date, float]] = None) -> dict:
    """주간 로그수익률 상관 + 나스닥 통제 부분상관 + 채택(2020-08) 전후 국면.

    상관·부분상관·선행/후행은 mc.candidate_analysis(거시 후보와 같은 잣대)를
    그대로 쓴다. 국면만 이 종목 고유의 경계(ADOPTION)로 다시 가른다 —
    candidate_analysis 의 고정 국면(2013~17…)은 MSTR 이야기와 안 맞는다."""
    if not mstr or not btc:
        return {"full_wk": None, "n_wk": 0, "partial_ndq": None,
                "best_lag": None, "best_corr": None, "span": None, "eras": {}}
    r = mc.candidate_analysis(btc, mstr, vix=None, nasdaq=nasdaq, use_log=True)

    # 채택 전후 국면 — 주간 격자. ISO 주 키로 맞추되 경계(날짜) 비교를 위해
    # MSTR 쪽 관측일을 대표 날짜로 들고 다닌다.
    wm = mc.log_returns(mc.weekly(mstr))
    wb = mc.log_returns(mc.weekly(btc))
    mk = {d.isocalendar()[:2]: (d, v) for d, v in sorted(wm.items())}
    bk = {d.isocalendar()[:2]: v for d, v in sorted(wb.items())}
    common = sorted(set(mk) & set(bk))

    def between(lo: date, hi: date) -> tuple[Optional[float], int]:
        xs, ys = [], []
        for k in common:
            d, v = mk[k]
            if lo <= d < hi:
                xs.append(v)
                ys.append(bk[k])
        return (mc.pearson(xs, ys), len(xs))

    eras: dict[str, tuple[Optional[float], int]] = {
        "채택 전(~2020-08)": between(date.min, ADOPTION)}
    if common:
        last_year = max(mk[k][0] for k in common).year
        for y in range(ADOPTION.year, last_year + 1):
            lo = ADOPTION if y == ADOPTION.year else date(y, 1, 1)
            label = f"{y}(08월~)" if y == ADOPTION.year else str(y)
            eras[label] = between(lo, date(y + 1, 1, 1))

    return {"full_wk": r.get("full_wk"), "n_wk": r.get("n_wk", 0),
            "partial_ndq": r.get("partial_ndq"),
            "best_lag": r.get("best_lag"), "best_corr": r.get("best_corr"),
            "span": r.get("span"), "eras": eras}


# ---------------------------------------------------------------------------
# 사이트 payload — 화면이 굽는 자료 구조 (relationship_map.payload 와 같은 취지)
# ---------------------------------------------------------------------------
def site_payload(mstr: dict[date, float], btc: dict[date, float],
                 base: date) -> Optional[dict]:
    """{"series": [[base 로부터의 일수, 상대성과지수]], "periods": [[시작, 끝]],
    "periodsDown": 동일, "corr": {...}, "n": 공통 거래일 수} — 없으면 None.

    상대성과지수 = (MSTR 누적수익 / BTC 누적수익), 첫 공통 거래일 = 1.0.
    파일 크기를 위해 **주간 격자**(각 ISO 주 마지막 관측)로 추리고 소수 3자리로
    자른다. 구간 오프셋은 추리기 전 격자의 날짜라 시각화가 그대로 칠하면 된다.
    공통 구간이 90일(달력)도 안 되면 None — 롤링 창 하나도 못 채우는 데이터로
    구간·지수를 내면 없는 이야기를 만들게 된다."""
    if not mstr or not btc:
        return None
    # **채택일(2020-08-11)부터만 싣는다.** 스투크는 MSTR 이력을 2000년대부터
    # 주는데, 첫 공통 거래일(2010년)을 기준 1.0 으로 잡으면 BTC 가 80만 배
    # 오르는 동안의 비율이 2e-5 규모가 되고 — 소수 3자리 반올림이 전부 0.000
    # 으로 뭉개 차트 선이 조용히 끊긴다(적대적 검증이 실데이터 규모로 재현).
    # 차트 제목도 '첫 매수 이후'다 — 서사와 눈금이 같은 곳을 가리키게 한다.
    days = [d for d in sorted(set(mstr) & set(btc))
            if d >= ADOPTION and mstr[d] > 0 and btc[d] > 0]
    if not days or (days[-1] - days[0]).days < WINDOW_DAYS:
        return None
    d0 = days[0]
    rel = {d: (mstr[d] / mstr[d0]) / (btc[d] / btc[d0]) for d in days}
    pts = sorted(mc.weekly(rel).items())
    if pts and pts[0][0] != d0:
        pts.insert(0, (d0, 1.0))         # 차트가 시작점 1.0 에서 출발하도록
    op = outperformance(mstr, btc)
    a = analyse(mstr, btc)

    def sig(v: float) -> float:
        """유효숫자 4자리 — 절대 반올림(round 3)은 1 에서 먼 규모를 뭉갠다."""
        return float(f"{v:.4g}")

    return {
        "series": [[(d - base).days, sig(v)] for d, v in pts],
        "periods": [[(s - base).days, (e - base).days] for s, e, _ in op["up"]],
        "periodsDown": [[(s - base).days, (e - base).days] for s, e, _ in op["down"]],
        "corr": {"weekly": None if a["full_wk"] is None else round(a["full_wk"], 3),
                 "weeks": a["n_wk"]},
        "n": len(days),
    }


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------
def report(mstr: dict[date, float], btc: dict[date, float],
           nasdaq: Optional[dict[date, float]] = None) -> str:
    L: list[str] = []
    add = L.append
    add("=" * 78)
    add("  MSTR(스트래티지) ↔ 비트코인 — 상대성과와 상관")
    add("=" * 78)

    a = analyse(mstr, btc, nasdaq)
    add("\n[1] 주간 로그수익률 상관")
    add("-" * 78)
    if a["full_wk"] is not None:
        add(f"  전 구간 ρ = {a['full_wk']:+.2f}  ({a['n_wk']}주)")
    else:
        add("  전 구간 — (표본 부족)")
    if a["partial_ndq"] is not None:
        add(f"  나스닥 통제 부분상관 = {a['partial_ndq']:+.2f}"
            "   (기술주 베타를 넘어 BTC 와 독립으로 붙는 몫)")
    add("  국면별 (2020-08-11 첫 BTC 매입 공시 전후):")
    for name, (v, n) in a["eras"].items():
        add(f"    {name:<14} ρ {v:+.2f}   ({n}주)" if v is not None
            else f"    {name:<14} —   ({n}주)")
    if a["best_lag"] is not None:
        add(f"  최강 선행/후행: k={a['best_lag']:+d}개월  ρ {a['best_corr']:+.2f}"
            "  (k>0 = MSTR 이 앞섬)")
        add("    ※ 여러 시차에서 |ρ| 최대를 고른 값 — 선택 효과가 있다(docs/27).")

    op = outperformance(mstr, btc)
    add(f"\n[2] 90일 롤링 로그수익률 스프레드 (MSTR−BTC), 문턱 ±{OUTPERF_SPREAD}")
    add("-" * 78)
    add(f"  스프레드 {OUTPERF_SPREAD} ≈ 90일 총수익 기준 "
        f"×{math.exp(OUTPERF_SPREAD):.2f}배")
    if op["up"]:
        add("  MSTR 이 크게 앞선 구간:")
        for s, e, m in op["up"]:
            add(f"    {s} ~ {e}  ({(e - s).days + 1:>3}일)"
                f"  최대 {m:+.2f} (≈×{math.exp(m):.2f}배)")
    else:
        add("  MSTR 이 크게 앞선 구간: 없음")
    if op["down"]:
        add("  BTC 가 크게 앞선 구간:")
        for s, e, m in op["down"]:
            add(f"    {s} ~ {e}  ({(e - s).days + 1:>3}일)"
                f"  최심 {m:+.2f} (≈×{math.exp(m):.2f}배)")
    else:
        add("  BTC 가 크게 앞선 구간: 없음")

    b0 = sorted(btc)[0]
    pl = site_payload(mstr, btc, b0)
    if pl:
        add(f"\n[3] 사이트 payload — 기준일 {b0}(BTC 첫 관측)")
        add("-" * 78)
        add(f"  점 {len(pl['series'])}개(주간 격자) · 구간 up {len(pl['periods'])}"
            f"·down {len(pl['periodsDown'])} · 공통 거래일 {pl['n']}일")

    add("\n" + "=" * 78)
    add("  주의: 구간 경계는 창(90일)·문턱(0.5) 정의에 종속된 값이다 — '정확한")
    add("  시점'이 아니라 '이 잣대로 잰 구간'이다. 상대성과지수는 비율이라 BTC 가")
    add("  빠질 때 MSTR 가 덜 빠져도 오른다. 표본은 채택(2020-08) 후 사이클 두 번")
    add("  이 안 된다 — 방향 신호가 아니라 서사 점검·시각화 용도다.")
    add("=" * 78)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="MSTR ↔ 비트코인 상대성과·상관 분석")
    ap.add_argument("--stocks", default="data/stocks.csv", help="주가 CSV (mstr 열)")
    ap.add_argument("--csv", default="data/market.csv", help="비트코인 가격 CSV")
    ap.add_argument("--macro", default="data/macro.csv", help="거시 CSV (nasdaq 통제)")
    ap.add_argument("--out", default=None, help="리포트 저장 경로")
    args = ap.parse_args(argv)

    mstr = load_mstr(args.stocks)
    if not mstr:
        print(f"주가 데이터가 없습니다: {args.stocks}\n"
              "  tools/fetch_stock.py 가 CI 러너에서 받는다 — 이 환경에선 stooq·야후가"
              " 정책 403 이라 못 받는다.")
        return 0
    btc = mc.load_btc(args.csv) if Path(args.csv).exists() else {}
    if not btc:
        print(f"비트코인 가격 데이터가 없습니다: {args.csv}")
        return 0
    nasdaq = None
    if Path(args.macro).exists():
        nasdaq = mc.load_macro(args.macro).get("nasdaq")

    text = report(mstr, btc, nasdaq)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
