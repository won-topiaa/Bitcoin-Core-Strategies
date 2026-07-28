#!/usr/bin/env python3
"""기준들이 서로 모순되지 않는지 감사한다.

    python3 tools/audit_config.py
    python3 tools/audit_config.py --config config/strategy.yaml

`config.validate()` 는 **한 항목 안의 형식**을 본다 (가중치 합이 100인가,
앵커가 순증가인가). 이 도구는 **항목들 사이의 관계**를 본다.

    "커버리지 하한 때문에 도달 불가능한 계열 조합이 있는가"
    "저점 프로파일 조건 중 서로 수학적으로 같은 것이 있는가"
    "합의 게이트가 데이터가 적을수록 엄해지지는 않는가"

이런 것들은 개별 값만 봐서는 안 보이고, 값을 하나 고칠 때 조용히 깨진다.
설정을 손볼 때마다 돌리면 된다.

종료 코드: 0 = 이상 없음, 1 = 문제 발견
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc_core.config import load_config          # noqa: E402
from btc_core.models import FamilyScore          # noqa: E402
from btc_core.score import _requirements, PRICE_FAMILY  # noqa: E402

# 서로 수학적으로 동치인 저점 프로파일 조건 쌍.
#   NUPL = 1 − 1/MVRV  이므로  NUPL < 0  ⟺  MVRV < 1
EQUIVALENT_CONDITIONS = [
    ({"mvrv", "nupl"}, "MVRV < 1 과 NUPL < 0 은 완전히 같은 조건이다 (NUPL = 1 − 1/MVRV)"),
]


class Audit:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.notes: list[str] = []

    def fail(self, section: str, msg: str) -> None:
        self.problems.append(f"[{section}] {msg}")

    def note(self, section: str, msg: str) -> None:
        self.notes.append(f"[{section}] {msg}")


def _families(cfg, keys):
    return [FamilyScore(k, k, float(cfg.bcs_families[k]["weight"]), 0, 0.5) for k in keys]


def check_consensus(cfg, a: Audit) -> None:
    """합의 게이트의 안전 속성."""
    keys = list(cfg.bcs_families)
    full = _families(cfg, keys)
    base_total, base_np, _ = _requirements(cfg, cfg.consensus, full)

    for r in range(1, len(keys) + 1):
        for combo in itertools.combinations(keys, r):
            need, need_np, _ = _requirements(cfg, cfg.consensus, _families(cfg, combo))
            n_np = sum(1 for k in combo if k != PRICE_FAMILY)

            if need > len(combo):
                a.fail("합의", f"{combo}: 요구 {need} > 생존 계열 {len(combo)} — 영구 미달")
            if r < len(keys):
                if need > base_total or need_np > base_np:
                    a.fail("합의", f"{combo}: 결측인데 기준이 더 엄해진다 "
                                   f"({need}/{need_np} > {base_total}/{base_np})")
            price_only = PRICE_FAMILY in combo and n_np == 0
            if price_only and need_np == 0:
                a.fail("합의", f"{combo}: 가격 계열 단독으로 게이트를 통과한다 "
                               "— 이 게이트의 존재 이유가 사라진다")
            if PRICE_FAMILY not in combo and need_np > 0:
                a.fail("합의", f"{combo}: 가격 계열이 없는데 비가격 {need_np}개를 요구한다 "
                               "— 막으려는 위험이 없는데 더 엄격하다")
            # 가격 단독은 '충족 불가능하게 두는 것'이 의도이므로 예외다.
            if need_np > n_np and not price_only:
                a.fail("합의", f"{combo}: 비가격 요구 {need_np} > 생존 비가격 {n_np}")


def check_coverage_reachability(cfg, a: Audit) -> None:
    """커버리지 하한으로 실제 도달 가능한 조합."""
    minc = float(cfg.coverage.get("min_coverage_for_action", 0.0))
    weights = {k: float(v["weight"]) for k, v in cfg.bcs_families.items()}
    total = sum(weights.values())

    reachable = []
    for r in range(1, len(weights) + 1):
        for combo in itertools.combinations(weights, r):
            if sum(weights[c] for c in combo) / total >= minc:
                reachable.append(combo)
    if not reachable:
        a.fail("커버리지", f"하한 {minc:.0%} 로는 어떤 조합도 실행할 수 없다")
        return
    smallest = min(len(c) for c in reachable)
    a.note("커버리지", f"하한 {minc:.0%} → 실행 가능한 최소 계열 수 {smallest}, "
                       f"가능 조합 {len(reachable)}/{2 ** len(weights) - 1}개")

    # 가격 계열 단독이 실행 가능하면 커버리지가 게이트를 못 받쳐준다
    if (PRICE_FAMILY,) in reachable:
        a.note("커버리지", "가격 계열 단독이 커버리지를 통과한다 — 합의 게이트가 유일한 방어선이다")


def check_bands_and_ladders(cfg, a: Audit) -> None:
    """사다리 트리거가 밴드·DCA 와 어긋나지 않는지."""
    dca = cfg.ladders["dca_multiplier"]
    for step in cfg.ladders["distribute"]["steps"]:
        t = float(step["trigger_bcs"])
        band = cfg.band_for(t)
        if dca[band["key"]] > 0:
            a.fail("사다리", f"분배 {t:+.0f} 이 속한 밴드 '{band['label']}' 의 "
                             f"DCA 배수가 {dca[band['key']]} 다 — 팔면서 동시에 산다")
    for step in cfg.ladders["accumulate"]["steps"]:
        t = float(step["trigger_bcs"])
        band = cfg.band_for(t)
        if dca[band["key"]] <= 0:
            a.fail("사다리", f"매집 {t:+.0f} 이 속한 밴드 '{band['label']}' 의 "
                             f"DCA 배수가 0 이다 — 사면서 적립은 멈춘다")

    # 재무장 임계가 사다리 첫 계단보다 바깥이면 영원히 재무장되지 않는다
    opp = float(cfg.hysteresis.get("rearm_opposite_bcs", 0))
    if opp > 0:
        d0 = min(float(s["trigger_bcs"]) for s in cfg.ladders["distribute"]["steps"])
        a0 = max(float(s["trigger_bcs"]) for s in cfg.ladders["accumulate"]["steps"])
        if -opp < a0:
            a.fail("히스테리시스",
                   f"분배 재무장 임계 {-opp:+.0f} 가 매집 첫 계단 {a0:+.0f} 보다 위다 "
                   "— 매집이 시작되기도 전에 분배가 재무장된다")
        if opp > d0:
            a.fail("히스테리시스",
                   f"매집 재무장 임계 {opp:+.0f} 가 분배 첫 계단 {d0:+.0f} 보다 위다 "
                   "— 분배가 시작되기도 전에는 매집이 재무장되지 않는다")


def check_bottom_profile(cfg, a: Audit) -> None:
    """저점 프로파일 조건이 서로 중복되지 않는지."""
    spec = cfg.bottom_profile
    if not spec.get("enabled"):
        return
    keys = {c["key"] for c in spec.get("conditions", [])}

    for pair, why in EQUIVALENT_CONDITIONS:
        if pair <= keys:
            a.fail("저점 프로파일",
                   f"{sorted(pair)} 가 함께 들어 있다 — {why}. "
                   "하나의 사실이 두 표를 행사하게 된다")

    n = len(spec.get("conditions", []))
    readings = sorted(spec.get("readings", []), key=lambda r: -int(r["min_hits"]))
    if readings and int(readings[0]["min_hits"]) != n:
        a.fail("저점 프로파일",
               f"최상위 해석의 min_hits({readings[0]['min_hits']}) 가 "
               f"조건 수({n}) 와 다르다 — 만점이 영원히 안 나오거나 너무 쉽게 나온다")
    hits = [int(r["min_hits"]) for r in readings]
    if hits != sorted(hits, reverse=True) or len(set(hits)) != len(hits):
        a.fail("저점 프로파일", f"해석 구간의 min_hits 가 내림차순 유일값이 아니다: {hits}")


def check_lrs(cfg, a: Audit) -> None:
    """LRS 밴드와 조정 규칙의 임계가 일치하는지."""
    bands = {b["key"]: b for b in cfg.lrs_bands}
    mod = cfg.lrs_modifier
    if not mod.get("enabled"):
        return
    easing = float(bands["easing"]["min"]) if "easing" in bands else None
    tightening = float(bands["tightening"]["max"]) if "tightening" in bands else None
    for rule in mod.get("rules", []):
        if "lrs_min" in rule and easing is not None and float(rule["lrs_min"]) != easing:
            a.note("LRS", f"조정 규칙의 lrs_min({rule['lrs_min']}) 이 "
                          f"'완화' 밴드 시작({easing:.0f}) 과 다르다")
        if "lrs_max" in rule and tightening is not None and float(rule["lrs_max"]) != tightening:
            a.note("LRS", f"조정 규칙의 lrs_max({rule['lrs_max']}) 이 "
                          f"'긴축' 밴드 끝({tightening:.0f}) 과 다르다")
    lo, hi = mod.get("bounds", [1.0, 1.0])
    for rule in mod.get("rules", []):
        m = float(rule["multiplier"])
        if not (float(lo) <= m <= float(hi)):
            a.fail("LRS", f"조정 배수 {m} 가 허용 범위 [{lo}, {hi}] 밖이다")


def check_indicator_shares(cfg, a: Audit) -> None:
    """지표 하나가 가질 수 있는 최대 지분."""
    worst = []
    for fam_key, fam in cfg.bcs_families.items():
        w = float(fam["weight"])
        n = len(fam["members"])
        share = w if fam.get("aggregate") == "max_abs" else w / n
        worst.append((share, fam_key, fam.get("aggregate", "mean")))
    worst.sort(reverse=True)
    top = worst[0]
    a.note("지분", f"단일 지표 최대 지분 {top[0]:.1f}% ({top[1]}, {top[2]})")

    band_step = min(
        abs(float(b["max"]) - float(b["min"]))
        for b in cfg.bands if abs(float(b["min"])) < 100 and abs(float(b["max"])) < 100
    )
    if top[0] > band_step:
        a.note("지분", f"그 지표가 −1 에서 +1 로 가면 BCS 가 {top[0] * 2:.0f}점 움직여 "
                       f"최소 밴드 폭({band_step:.0f}점)을 혼자 넘길 수 있다")


def main() -> int:
    ap = argparse.ArgumentParser(description="기준 간 상호 모순 감사")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)        # 형식 검증은 여기서 이미 돈다
    a = Audit()
    for check in (check_consensus, check_coverage_reachability, check_bands_and_ladders,
                  check_bottom_profile, check_lrs, check_indicator_shares):
        check(cfg, a)

    print(f"감사 대상: {cfg.path}")
    print()
    if a.notes:
        print("참고")
        for n in a.notes:
            print(f"  · {n}")
        print()
    if a.problems:
        print(f"문제 {len(a.problems)}건")
        for p in a.problems:
            print(f"  ! {p}")
        return 1
    print("모순 없음.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
