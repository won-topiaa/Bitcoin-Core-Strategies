#!/usr/bin/env python3
"""후보 지표를 선별 관문 6개에 한 번에 통과시켜 본다.

지금까지 후보를 검정할 때마다 임시 스크립트를 새로 썼다. 그러다 보니 매번
같은 실수를 할 위험이 있었고, 실제로 두 번은 검정을 덜 해서 잘못된 결론에
갔다가 나중에 뒤집었다. 그 교훈을 관문으로 못 박아 이 파일에 넣었다.

    # 기존 지표를 같은 관문에 통과시켜 본다 (기준선 확인)
    python3 tools/screen_candidate.py --csv data/market.csv

    # 새 후보 파일을 검정한다
    python3 tools/screen_candidate.py --csv data/market.csv --candidates mine.py

후보 파일은 함수 하나만 있으면 된다.

    def build(m, h):
        # m: MarketData (일간 연속으로 정렬됨)
        # h: 도우미 묶음 (h.Series, h.sma, h.ratio, h.pct 등)
        nvt = m.market_cap.map_with(h.sma(m.active_addresses, 90),
                                    lambda c, a: c / a if a else None)
        return {"활성주소당 시총": nvt}

## 관문 6개

1. **미래 참조 금지** — 퍼센타일은 그 시점까지의 이력만 쓴다(확장/이동창).
   전체 표본으로 적합한 뒤 과거를 채점하면 어떤 지표든 좋아 보인다.
2. **선행 수익률 순위상관** 180 / 365 / 730일.
3. **십분위 검정** — 값으로 10등분해 구간별 이후 1년 수익 중앙값을 본다.
   *이 관문이 없으면 속는다.* 해시프라이스는 네 사이클 저점에서 4/4 정확했는데
   가장 극단인 십분위의 이후 1년 중앙값이 −45%였다. 이야기와 정반대였다.
4. **전환점 안정성** — 고점 4곳·저점 4곳에서 원시값과 4년 퍼센타일을 함께 본다.
   원시값이 사이클마다 붕괴하면 고정 앵커가 불가능하다는 뜻이다.
5. **중복도** — 기존 지표 전부와 순위상관. |ρ| ≥ 0.85 면 새 정보가 없다.
6. **사이클별 부호 일관성** — 네 사이클 각각에서 선행수익률 상관의 부호가
   같아야 한다. 전 구간 상관이 좋아도 사이클 내부에서 뒤집히면 추세 인공물이다.

## 왜 관문을 통과해도 편입이 아닌가

활성주소당 시총은 이 여섯 관문을 전부 통과하고도 실제 백테스트에서 분배를
나쁘게 만들었다(2025 사이클 고점대비 63% → 51%). 관문은 **노이즈를 걸러내는
장치**이고, 편입 여부는 시스템에 넣어 돌려 봐야 안다. docs/11 참조.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from btc_core.datasources.csv_source import load_csv_bundle   # noqa: E402
from btc_core.normalize import percentile_rank                # noqa: E402
from btc_core.series import Series, ratio, sma                 # noqa: E402
from screen_indicator import spearman                          # noqa: E402

HORIZONS = (180, 365, 730)
FAMILIES: dict[str, str] = {}
PERCENTILE_YEARS = 4.0
REDUNDANCY_LIMIT = 0.85

# 실측된 사이클 전환점. 마지막 저점은 미완성 사이클의 최저점이라 확정이 아니다.
TOPS = (date(2013, 11, 30), date(2017, 12, 17), date(2021, 11, 10), date(2025, 10, 6))
BOTTOMS = (date(2015, 1, 14), date(2018, 12, 15), date(2022, 11, 21), date(2026, 6, 6))
NEAR_DAYS = 45

CYCLES = (
    ("2013", date(2011, 1, 1), date(2015, 6, 1)),
    ("2017", date(2015, 6, 1), date(2019, 6, 1)),
    ("2021", date(2019, 6, 1), date(2023, 3, 1)),
    ("2025", date(2023, 3, 1), date(2026, 12, 31)),
)


# ---------------------------------------------------------------------------
# 후보 파일에 넘겨주는 도우미
# ---------------------------------------------------------------------------

class Helpers:
    """후보 정의에서 쓸 수 있는 것들. 임시 스크립트에서 매번 다시 쓰던 것들이다."""

    Series = Series
    sma = staticmethod(sma)
    ratio = staticmethod(ratio)

    @staticmethod
    def pct(series: Series, years: float = PERCENTILE_YEARS,
            min_points: int = 90) -> Series:
        """이동창 퍼센타일 [-1,+1]. **미래를 보지 않는다.**

        각 날짜에서 그 날짜까지의 years 년 이력만으로 순위를 매긴다.
        절대 수준에 구조적 추세가 있는 지표(서멀캡, 시총 배수 등)는 이 형태로만
        쓸 수 있다.
        """
        out: list[Optional[float]] = []
        dates, vals = list(series.dates), list(series.values)
        window: list[tuple[date, float]] = []
        head = 0
        for i, v in enumerate(vals):
            if v is None:
                out.append(None)
                continue
            window.append((dates[i], v))
            cut = dates[i] - timedelta(days=int(years * 365.25))
            while head < len(window) and window[head][0] < cut:
                head += 1
            hist = [x for _, x in window[head:]]
            out.append(percentile_rank(v, hist) if len(hist) >= min_points else None)
        return Series(series.dates, tuple(out), f"pct({series.name})")

    @staticmethod
    def change(series: Series, periods: int) -> Series:
        """N기간 전 대비 배수. 추세 제거용."""
        vals = list(series.values)
        out: list[Optional[float]] = [None] * len(vals)
        for i in range(periods, len(vals)):
            a, b = vals[i], vals[i - periods]
            if a is not None and b:
                out[i] = a / b
        return Series(series.dates, tuple(out), f"chg{periods}({series.name})")

    @staticmethod
    def cumulative(series: Series) -> Series:
        """누적합. 서멀캡처럼 '지금까지 총합'이 분모인 지표용."""
        out: list[Optional[float]] = []
        total = 0.0
        for v in series.values:
            if v is not None:
                total += v
            out.append(total if total > 0 else None)
        return Series(series.dates, tuple(out), f"cum({series.name})")


# ---------------------------------------------------------------------------
# 관문
# ---------------------------------------------------------------------------

@dataclass
class Result:
    name: str
    rho: dict[int, Optional[float]]
    deciles: list[float]
    tops_raw: list[Optional[float]]
    bottoms_raw: list[Optional[float]]
    tops_pct: list[Optional[float]]
    bottoms_pct: list[Optional[float]]
    same_family: tuple[str, float]
    cross_family: tuple[str, float]
    per_cycle: list[tuple[str, Optional[float]]]
    n: int

    @property
    def decile_spread(self) -> Optional[float]:
        return self.deciles[0] - self.deciles[-1] if len(self.deciles) == 10 else None

    @property
    def monotone_steps(self) -> int:
        return sum(1 for a, b in zip(self.deciles, self.deciles[1:]) if b <= a)

    @property
    def best_rho(self) -> Optional[float]:
        vals = [v for v in self.rho.values() if v is not None]
        return max(vals, key=abs) if vals else None

    @property
    def turning_gap(self) -> Optional[float]:
        tp = [v for v in self.tops_pct if v is not None]
        bp = [v for v in self.bottoms_pct if v is not None]
        if not tp or not bp:
            return None
        return sum(tp) / len(tp) - sum(bp) / len(bp)

    def hard_failures(self) -> list[str]:
        """기각 사유. 이 중 하나라도 걸리면 편입 후보가 아니다."""
        bad: list[str] = []

        # 계열이 다른 지표와 0.85 이상 겹치면 '독립 확인'이라는 전제가 깨진다.
        # 같은 계열 안의 중복은 설계상 허용된다 — 그래서 평균으로 묶고 계열
        # 가중치에 상한을 둔다. 두 경우를 섞어 판정하면 안 된다.
        if abs(self.cross_family[1]) >= REDUNDANCY_LIMIT:
            bad.append(f"계열 간 중복 {self.cross_family[0]} {self.cross_family[1]:+.2f} "
                       f"≥ {REDUNDANCY_LIMIT} — 다른 계열의 확인으로 셀 수 없다")

        # 지평선마다 부호가 다르면 무엇을 재는지 알 수 없다.
        signs = {1 if v > 0 else -1 for v in self.rho.values() if v is not None and abs(v) >= 0.10}
        if len(signs) > 1:
            bad.append("지평선 간 부호 불일치 — 180/365/730일이 서로 반대")

        # 전 구간에서 정보가 전혀 없으면 8개 전환점만 맞은 것이다.
        if self.best_rho is None or abs(self.best_rho) < 0.15:
            bad.append(f"최대 선행상관 {self.best_rho if self.best_rho is None else round(self.best_rho, 2)}"
                       " — 전 구간에 정보 없음")

        # 가장 극단인 십분위가 지표의 전제와 반대면(예: 가장 싼 구간의 이후
        # 수익이 최악) 그 지표는 이야기와 다른 것을 재고 있다.
        if len(self.deciles) == 10 and self.best_rho is not None:
            lo, hi = self.deciles[0], self.deciles[-1]
            if self.best_rho < 0 and lo < hi:
                bad.append(f"극단 십분위 역전 — 최저 구간 {lo:+.0f}% < 최고 구간 {hi:+.0f}%")
            if self.best_rho > 0 and lo > hi:
                bad.append(f"극단 십분위 역전 — 최저 구간 {lo:+.0f}% > 최고 구간 {hi:+.0f}%")

        # 고점과 저점을 구별하지 못하면 사이클 위치 지표가 아니다.
        gap = self.turning_gap
        if gap is not None and abs(gap) < 0.4:
            bad.append(f"전환점 퍼센타일 분리 {gap:+.2f} — 고·저점 구별 못 함")
        return bad

    def weak_flags(self) -> list[str]:
        """기각은 아니지만 알고 써야 하는 것들."""
        soft: list[str] = []
        if abs(self.same_family[1]) >= REDUNDANCY_LIMIT:
            soft.append(f"같은 계열 {self.same_family[0]} {self.same_family[1]:+.2f} — "
                        "설계상 허용이지만 계열 안에서 한 표로만 세야 한다")
        if self.decile_spread is not None and abs(self.decile_spread) < 60:
            soft.append(f"십분위 분리도 {self.decile_spread:+.0f}%p — 약한 판별력")
        if self.monotone_steps <= 4:
            soft.append(f"십분위 단조성 {self.monotone_steps}/9 — 중간 구간이 뒤섞임")
        signs = [1 if v > 0 else -1 for _, v in self.per_cycle if v is not None]
        if len(signs) >= 3 and len(set(signs)) > 1:
            odd = [lab for lab, v in self.per_cycle
                   if v is not None and (1 if v > 0 else -1) != max(set(signs), key=signs.count)]
            soft.append(f"사이클별 부호 불일치 ({', '.join(odd)}) — "
                        "2013 사이클은 거의 단조 상승이라 흔히 어긋난다")
        pct_ok = sum(1 for v in self.tops_pct + self.bottoms_pct if v is not None)
        if pct_ok < 8:
            soft.append(f"전환점 {8 - pct_ok}곳에서 퍼센타일 산출 불가 — 이력 부족")
        return soft

    @property
    def verdict(self) -> str:
        if self.hard_failures():
            return "기각"
        return "통과(주의)" if self.weak_flags() else "통과"


def _near(lookup: dict, target: date) -> Optional[float]:
    for off in range(NEAR_DAYS + 1):
        for d in (target - timedelta(days=off), target + timedelta(days=off)):
            if lookup.get(d) is not None:
                return lookup[d]
    return None


def _forward(price: Series, horizon: int) -> dict[date, float]:
    px = dict(zip(price.dates, price.values))
    out = {}
    for d, v in zip(price.dates, price.values):
        nxt = px.get(d + timedelta(days=horizon))
        if v and nxt:
            out[d] = (nxt / v - 1.0) * 100.0
    return out


def screen(name: str, series: Series, price: Series,
           existing: dict[str, Series],
           families: Optional[dict[str, str]] = None,
           own_family: Optional[str] = None) -> Result:
    lookup = dict(zip(series.dates, series.values))
    pct = Helpers.pct(series)
    pct_lookup = dict(zip(pct.dates, pct.values))

    # 채점에 쓰는 형태로 상관을 본다. 퍼센타일이 산출되면 그쪽을 쓴다 —
    # 절대 수준에 추세가 있으면 원시값 상관은 추세를 재는 것이 된다.
    usable_pct = sum(1 for v in pct.values if v is not None)
    scored = pct if usable_pct >= 0.5 * sum(1 for v in series.values if v is not None) else series
    scored_lookup = dict(zip(scored.dates, scored.values))

    rho: dict[int, Optional[float]] = {}
    for h in HORIZONS:
        fwd = _forward(price, h)
        xs = [v for d, v in scored_lookup.items() if v is not None and d in fwd]
        ys = [fwd[d] for d, v in scored_lookup.items() if v is not None and d in fwd]
        rho[h] = spearman(xs, ys) if len(xs) >= 30 else None

    fwd365 = _forward(price, 365)
    pairs = sorted((v, fwd365[d]) for d, v in scored_lookup.items()
                   if v is not None and d in fwd365)
    deciles: list[float] = []
    if len(pairs) >= 100:
        q = len(pairs) // 10
        for i in range(10):
            chunk = sorted(r for _, r in pairs[i * q:(i + 1) * q])
            deciles.append(chunk[len(chunk) // 2])

    families = families or {}
    same: tuple[str, float] = ("—", 0.0)
    cross: tuple[str, float] = ("—", 0.0)
    for key, other in existing.items():
        o = dict(zip(other.dates, other.values))
        xs, ys = [], []
        for d, v in lookup.items():
            w = o.get(d)
            if v is not None and w is not None and not isinstance(w, str):
                xs.append(v)
                ys.append(w)
        r = spearman(xs, ys) if len(xs) >= 200 else None
        if r is None:
            continue
        is_same = own_family is not None and families.get(key) == own_family
        slot = same if is_same else cross
        if abs(r) > abs(slot[1]):
            if is_same:
                same = (key, r)
            else:
                cross = (key, r)

    per_cycle: list[tuple[str, Optional[float]]] = []
    for label, lo, hi in CYCLES:
        xs = [v for d, v in scored_lookup.items()
              if v is not None and lo <= d < hi and d in fwd365]
        ys = [fwd365[d] for d, v in scored_lookup.items()
              if v is not None and lo <= d < hi and d in fwd365]
        per_cycle.append((label, spearman(xs, ys) if len(xs) >= 60 else None))

    return Result(
        name=name, rho=rho, deciles=deciles,
        tops_raw=[_near(lookup, t) for t in TOPS],
        bottoms_raw=[_near(lookup, b) for b in BOTTOMS],
        tops_pct=[_near(pct_lookup, t) for t in TOPS],
        bottoms_pct=[_near(pct_lookup, b) for b in BOTTOMS],
        same_family=same, cross_family=cross, per_cycle=per_cycle,
        n=sum(1 for v in series.values if v is not None),
    )


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

def report(results: list[Result]) -> str:
    L: list[str] = []
    add = L.append
    add("=" * 96)
    add("  선별 관문 — 통과해도 편입은 아니다. 시스템에 넣고 백테스트해 봐야 안다(docs/11).")
    add("=" * 96)
    for r in results:
        add("")
        add(f"### {r.name}   (관측 {r.n:,}일)")
        add("  선행수익률 ρ   " + "  ".join(
            f"{h}d {r.rho[h]:+.2f}" if r.rho[h] is not None else f"{h}d —" for h in HORIZONS))
        if r.deciles:
            add("  십분위 이후1년 " + " ".join(f"{x:+5.0f}" for x in r.deciles))
            add(f"  D1−D10 {r.decile_spread:+.0f}%p    단조감소 {r.monotone_steps}/9")
        f = lambda xs, w=8, p=2: " ".join(
            f"{x:{w}.{p}f}" if x is not None else " " * (w - 1) + "—" for x in xs)
        add(f"  원시 고점  {f(r.tops_raw)}")
        add(f"  원시 저점  {f(r.bottoms_raw)}")
        add(f"  4y%ile 고점 {f(r.tops_pct, 7)}")
        add(f"  4y%ile 저점 {f(r.bottoms_pct, 7)}")
        add("  사이클별 ρ  " + "  ".join(
            f"{lab} {v:+.2f}" if v is not None else f"{lab} —" for lab, v in r.per_cycle))
        add(f"  중복  계열내 {r.same_family[0]} {r.same_family[1]:+.2f}   "
            f"계열간 {r.cross_family[0]} {r.cross_family[1]:+.2f}")
        bad, soft = r.hard_failures(), r.weak_flags()
        if bad:
            add(f"  판정  ✘ 기각 ({len(bad)}건)")
        elif soft:
            add("  판정  ✔ 관문 통과 (주의사항 있음) — 시스템에 넣고 사이클별 실행 품질 비교")
        else:
            add("  판정  ✔ 관문 통과 — 시스템에 넣고 사이클별 실행 품질 비교")
        for b in bad:
            add(f"        ✘ {b}")
        for s in soft:
            add(f"        ! {s}")
    add("")
    add("=" * 96)
    passed = [r.name for r in results if not r.hard_failures()]
    add(f"  {len(results)}개 중 관문 통과 {len(passed)}개" + (f": {', '.join(passed)}" if passed else ""))
    add("  통과는 '노이즈가 아니다'라는 뜻이고 편입 승인이 아니다.")
    add("=" * 96)
    return "\n".join(L)


def load_candidates(path: Path) -> Callable:
    spec = importlib.util.spec_from_file_location("candidates", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"후보 파일을 읽을 수 없습니다: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "build"):
        raise SystemExit(f"{path}: build(m, h) 함수가 필요합니다.")
    return mod.build


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--candidates", default=None,
                    help="build(m, h) 를 정의한 파이썬 파일. 없으면 기존 지표를 재검정한다.")
    ap.add_argument("--only", default=None, help="이름에 이 문자열이 든 후보만")
    ap.add_argument("--family", default=None,
                    help="후보가 들어갈 계열. 지정하면 그 계열 안의 중복은 '계열내'로 분류된다.")
    args = ap.parse_args(argv)

    import backtest as bt

    from btc_core.config import load_config
    cfg = load_config()
    global FAMILIES
    FAMILIES = {k: spec.get("family", "") for k, spec in cfg.indicators.items()}

    market = load_csv_bundle(args.csv).market
    m = market.normalized()
    existing = {k: v for k, v in bt.build_indicator_series(m).items()
                if k != "hash_ribbons"}

    if args.candidates:
        build = load_candidates(Path(args.candidates))
        cands = build(m, Helpers)
    else:
        # 기준선 — 기존 지표를 같은 관문에 통과시켜 본다.
        # 통과 기준이 현실적인지 확인하는 용도이고, 몇 개는 실제로 기각된다.
        # 그게 이 시스템이 알고 있는 약점이다(docs/11 고점 탐지력 붕괴).
        cands = dict(existing)

    if args.only:
        cands = {k: v for k, v in cands.items() if args.only in k}
    if not cands:
        raise SystemExit("검정할 후보가 없습니다.")

    results = []
    for name, s in cands.items():
        if s is None or not len(s):
            print(f"  건너뜀 — {name}: 시계열이 비어 있습니다", file=sys.stderr)
            continue
        if any(isinstance(v, str) for v in s.values):
            print(f"  건너뜀 — {name}: 범주형은 순위상관 대상이 아닙니다", file=sys.stderr)
            continue
        results.append(screen(
            name, s, m.price,
            {k: v for k, v in existing.items() if k != name},
            families=FAMILIES, own_family=FAMILIES.get(name, args.family),
        ))

    print(report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
