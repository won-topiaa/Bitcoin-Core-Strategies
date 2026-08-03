"""온체인 검정 로직 — 순환논증을 실제로 걸러내는지 합성 데이터로 검증한다.

docs/29 의 핵심 발견을 못 박는다: **가격에서 파생된 지표는 ΔVIX·나스닥 통제를
그대로 통과한다**(가격의 그림자는 나스닥과 무관하니까). 그래서 온체인에는
**가격 자신을 통제하는 검정**이 필요하다.
"""

from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import onchain_correlation as oc  # noqa: E402


def _days(n: int, start=date(2014, 1, 31)) -> list[date]:
    return [start + timedelta(days=7 * i) for i in range(n)]


def _walk(ds, seed=1, drift=0.0):
    import random
    rng = random.Random(seed)
    out, lv = {}, 100.0
    for d in ds:
        lv *= math.exp(drift + rng.gauss(0, 0.05))
        out[d] = lv
    return out


def test_forward_test_is_immune_to_circularity():
    """가격을 그대로 베낀 지표라도 **전방** 예측력은 0 이어야 한다.

    동시점으로 재면 ρ≈1 이 나오지만(항등식), 전방은 미래를 알 수 없으므로 0 이다.
    이것이 전방 프레임을 주요 프레임으로 삼는 이유다."""
    ds = _days(400)
    price = _walk(ds, seed=7)
    clone = {d: v * 3.0 for d, v in price.items()}      # 가격의 상수배 = 완전 순환

    r_fwd, n = oc.forward(clone, price, 3, level=False)
    assert n > 50
    assert abs(r_fwd) < 0.25, f"완전 순환 지표인데 전방 예측력이 생겼다: {r_fwd}"


def test_momentum_control_erases_a_pure_price_shadow():
    """'가격의 그림자' — 당월 수익률에 비례하는 지표는 모멘텀을 통제하면 사라진다.

    실현시총 유입률이 정확히 이 모습이었다(docs/29): 전방 상관이 뜨지만 그 정체는
    가격 모멘텀이었다."""
    ds = _days(400)
    price = _walk(ds, seed=11, drift=0.002)
    pm = sorted(price)
    shadow = {}                       # 지표[t] = 그 주 가격수익률 (+ 약간의 잡음)
    import random
    rng = random.Random(3)
    for a, b in zip(pm, pm[1:]):
        shadow[b] = math.log(price[b] / price[a]) + rng.gauss(0, 0.005)

    ex = oc.forward_ex_momentum(shadow, price, 3, level=True)
    assert ex is not None
    assert abs(ex) < 0.2, f"모멘텀을 통제하면 그림자는 사라져야 한다: {ex}"


def test_momentum_control_keeps_a_genuine_leading_signal():
    """반대로 **진짜 선행 신호**는 모멘텀을 통제해도 살아남아야 한다 — 통제가
    무엇이든 지우는 둔기가 아님을 확인한다."""
    ds = _days(400)
    import random
    rng = random.Random(5)
    # 지표[t] 는 **이후 13주 전체**를 민다. 한 주만 밀게 만들면 3개월 창(13주 합산)
    # 안에서 1/13 로 희석돼, 검정이 아니라 합성 데이터가 실패한다(실제로 그랬다).
    sig = {d: rng.gauss(0, 1.0) for d in ds}
    price, lv = {}, 100.0
    for i, d in enumerate(ds):
        window = [sig[ds[j]] for j in range(max(0, i - 13), i)]
        drive = sum(window) / len(window) if window else 0.0
        lv *= math.exp(0.02 * drive + rng.gauss(0, 0.005))
        price[d] = lv

    ex = oc.forward_ex_momentum(sig, price, 3, level=True)
    assert ex is not None
    assert abs(ex) > 0.2, f"진짜 선행 신호가 통제에 지워졌다: {ex}"


def test_load_market_rejects_non_finite_cells():
    """NaN/inf 가 섞이면 지표가 통째로 오염된다 — 읽는 단계에서 버린다."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8", newline="") as fh:
        fh.write("date,price,hashrate\n")
        fh.write("2020-01-01,100,5\n")
        fh.write("2020-01-02,nan,6\n")
        fh.write("2020-01-03,120,inf\n")
        p = fh.name
    got = oc.load_market(p)
    assert set(got["price"]) == {date(2020, 1, 1), date(2020, 1, 3)}
    assert set(got["hashrate"]) == {date(2020, 1, 1), date(2020, 1, 2)}


def test_real_data_report_runs_and_flags_nothing_as_passing():
    """실데이터로 리포트가 돌고, 현재 판정(통과 0)이 유지되는지 — 회귀 가드."""
    import pytest
    csv_path = ROOT / "data" / "market.csv"
    if not csv_path.exists():
        pytest.skip("data/market.csv 없음")
    market = oc.load_market(str(csv_path))
    try:
        import macro_correlation as mc
        macro = mc.load_macro(str(ROOT / "data" / "macro.csv"))
    except SystemExit:
        macro = {}
    rows = oc.analyse(market, macro)
    assert rows, "후보가 하나도 생성되지 않았다"
    text = oc.report(rows)
    assert "통과" in text
    # 판정이 바뀌면(무언가 통과하면) 사람이 반드시 다시 보게 만든다
    passed = [r["name"] for r in rows if r["passed"]]
    assert not passed, (
        f"통과 후보가 생겼다 — 데이터가 바뀐 것인지 검정이 느슨해진 것인지 "
        f"확인이 필요하다: {passed}")
