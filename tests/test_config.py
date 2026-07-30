"""설정 검증 — 전략이 스스로 모순되지 않는지 지키는 마지막 방어선."""

from __future__ import annotations

import copy

import pytest
import yaml

from btc_core.config import ConfigError, StrategyConfig, load_config, validate


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def mutated(cfg, mutate):
    raw = copy.deepcopy(dict(cfg.raw))
    mutate(raw)
    return StrategyConfig(raw=raw, path=cfg.path)


# --- 실제 설정이 유효한가 --------------------------------------------------

def test_shipped_config_is_valid(cfg):
    validate(cfg)          # 예외가 없으면 통과


def test_family_weights_sum_to_one_hundred(cfg):
    assert sum(f["weight"] for f in cfg.bcs_families.values()) == 100


def test_lrs_weights_sum_to_one_hundred(cfg):
    assert sum(c["weight"] for c in cfg.lrs_components.values()) == 100


def test_every_indicator_belongs_to_exactly_one_family(cfg):
    members = [m for f in cfg.bcs_families.values() for m in f["members"]]
    assert len(members) == len(set(members)) == len(cfg.indicators)


def test_no_single_family_holds_a_majority(cfg):
    """어떤 계열도 과반(50)을 넘지 않는다 — 한 아이디어가 단독으로 밴드를 정하지 못하게.

    4계열 시절 기준은 30 이었다. 계열이 3개가 되면서 그 기준은 산술적으로
    불가능해졌다(3 × 30 = 90 < 100). 그래서 '독점 금지'의 의미를 다시 정의했다:
    **한 계열이 나머지 전부를 합친 것보다 커서는 안 된다.**

    밸류에이션이 40 이고 집계가 max_abs 라, 그 안의 한 지표가 BCS 의 40% 를
    혼자 끌고 갈 수 있다. 이건 이번에 새로 생긴 위험이 아니라 **원래부터
    그랬던 것이 드러난 것**이다 — 보유자 행동은 자동 수집이 불가능해서 16년
    백테스트에서 한 번도 채워진 적이 없고, 그때 실효 가중치가 이미 40 이었다.
    """
    weights = [f["weight"] for f in cfg.bcs_families.values()]
    assert max(weights) < sum(weights) - max(weights) + 1e-9 or max(weights) <= 50
    assert max(weights) <= 50


def test_valuation_family_does_not_double_count_correlated_members(cfg):
    """MVRV Z 와 NUPL 은 계산 재료가 같으므로 합산이 아니라 극단값 채택이어야 한다."""
    assert cfg.bcs_families["valuation"]["aggregate"] == "max_abs"


def test_distribute_ladder_never_touches_the_core_holding(cfg):
    d = cfg.ladders["distribute"]
    assert sum(s["pct_of_holdings"] for s in d["steps"]) + d["core_hold_pct"] <= 100


def test_band_assignment_is_symmetric_around_zero(cfg):
    """+t 가 더 극단인 밴드로 들어가면 −t 도 그래야 한다."""
    for t in (20, 45, 70):
        up = cfg.band_for(float(t))
        down = cfg.band_for(float(-t))
        assert float(up["min"]) == t, f"+{t} 는 하한이 {t} 인 밴드에 속해야 한다"
        assert float(down["max"]) == -t, f"-{t} 는 상한이 -{t} 인 밴드에 속해야 한다"


def test_band_extremes_are_covered(cfg):
    assert cfg.band_for(100.0)["key"] == "euphoria"
    assert cfg.band_for(-100.0)["key"] == "capitulation"
    assert cfg.band_for(1000.0)["key"] == "euphoria"
    assert cfg.band_for(-1000.0)["key"] == "capitulation"


def test_lrs_bands_are_symmetric_too(cfg):
    assert cfg.lrs_band_for(40.0)["key"] == "easing"
    assert cfg.lrs_band_for(-40.0)["key"] == "tightening"


def test_invalidation_conditions_are_documented(cfg):
    """전략을 폐기할 조건을 미리 적어두지 않으면 사후에 합리화하게 된다."""
    assert len(cfg.invalidation) >= 3
    assert all(i.get("id") and i.get("text") for i in cfg.invalidation)


# --- 잘못된 설정을 잡아내는가 ----------------------------------------------

def test_rejects_family_weights_that_do_not_sum_to_one_hundred(cfg):
    bad = mutated(cfg, lambda r: r["bcs"]["families"]["price"].__setitem__("weight", 99))
    with pytest.raises(ConfigError, match="가중치 합"):
        validate(bad)


def test_rejects_an_indicator_in_the_wrong_family(cfg):
    bad = mutated(cfg, lambda r: r["indicators"]["puell"].__setitem__("family", "price"))
    with pytest.raises(ConfigError, match="family"):
        validate(bad)


def test_rejects_an_unknown_member(cfg):
    bad = mutated(cfg, lambda r: r["bcs"]["families"]["supply"]["members"].append("없는지표"))
    with pytest.raises(ConfigError, match="indicators 에 없습니다"):
        validate(bad)


def test_rejects_an_orphaned_indicator(cfg):
    bad = mutated(cfg, lambda r: r["bcs"]["families"]["supply"]["members"].remove("puell"))
    with pytest.raises(ConfigError, match="속하지 않은"):
        validate(bad)


def test_rejects_a_gap_between_bands(cfg):
    def poke(r):
        r["bands"][2]["min"] = 25       # upper_neutral 하한을 올려 틈을 만든다
    with pytest.raises(ConfigError, match="틈/겹침"):
        validate(mutated(cfg, poke))


def test_rejects_a_distribute_ladder_that_eats_the_core(cfg):
    def poke(r):
        r["ladders"]["distribute"]["steps"][0]["pct_of_holdings"] = 70
    with pytest.raises(ConfigError, match="매도 가능분"):
        validate(mutated(cfg, poke))


def test_rejects_an_accumulate_ladder_over_one_hundred_percent(cfg):
    def poke(r):
        r["ladders"]["accumulate"]["steps"][0]["pct_of_reserve"] = 90
    with pytest.raises(ConfigError, match="100%"):
        validate(mutated(cfg, poke))


def test_rejects_out_of_order_ladder_triggers(cfg):
    def poke(r):
        r["ladders"]["distribute"]["steps"][0]["trigger_bcs"] = 99
    with pytest.raises(ConfigError, match="증가 순서"):
        validate(mutated(cfg, poke))


def test_rejects_out_of_order_accumulate_triggers(cfg):
    def poke(r):
        r["ladders"]["accumulate"]["steps"][0]["trigger_bcs"] = -99
    with pytest.raises(ConfigError, match="감소 순서"):
        validate(mutated(cfg, poke))


def test_rejects_a_missing_dca_multiplier(cfg):
    def poke(r):
        del r["ladders"]["dca_multiplier"]["neutral"]
    with pytest.raises(ConfigError, match="dca_multiplier"):
        validate(mutated(cfg, poke))


def test_rejects_floor_weights_that_do_not_sum_to_one(cfg):
    def poke(r):
        r["floors"]["lines"][0]["weight"] = 0.9
    with pytest.raises(ConfigError, match="바닥선 가중치"):
        validate(mutated(cfg, poke))


def test_rejects_malformed_anchors(cfg):
    def poke(r):
        r["indicators"]["puell"]["anchors"] = [[1.0, 0.5], [0.5, 0.9]]
    with pytest.raises(Exception):
        validate(mutated(cfg, poke))


def test_rejects_a_categorical_indicator_without_states(cfg):
    def poke(r):
        del r["indicators"]["hash_ribbons"]["states"]
    with pytest.raises(ConfigError, match="states"):
        validate(mutated(cfg, poke))


def test_rejects_a_missing_section(cfg):
    with pytest.raises(ConfigError, match="필수 섹션"):
        validate(mutated(cfg, lambda r: r.pop("consensus")))


def test_load_config_reports_a_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="찾을 수 없습니다"):
        load_config(tmp_path / "nope.yaml")


def test_load_config_reads_an_alternate_file(cfg, tmp_path):
    p = tmp_path / "alt.yaml"
    p.write_text(yaml.safe_dump(dict(cfg.raw), allow_unicode=True), encoding="utf-8")
    alt = load_config(p)
    assert alt.bcs_families.keys() == cfg.bcs_families.keys()


# --- 기준 간 상호 모순 (tools/audit_config.py 와 같은 불변식) --------------

def test_config_audit_finds_no_contradictions():
    """설정 항목들 사이의 관계가 서로 모순되지 않아야 한다.

    validate() 는 한 항목 안의 형식만 본다. 이 검사는 항목 사이의 관계를 본다 —
    도달 불가능한 계열 조합, 수학적으로 동치인 저점 프로파일 조건,
    결측일수록 엄해지는 합의 게이트 같은 것들이다.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, str(root / "tools" / "audit_config.py")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_ladder_triggers_never_contradict_their_dca_multiplier(cfg):
    """분배 구간에서 적립하거나 매집 구간에서 적립을 멈추면 앞뒤가 안 맞는다."""
    dca = cfg.ladders["dca_multiplier"]
    for step in cfg.ladders["distribute"]["steps"]:
        band = cfg.band_for(float(step["trigger_bcs"]))
        assert dca[band["key"]] == 0.0, f"분배 {step['trigger_bcs']} — {band['label']}"
    for step in cfg.ladders["accumulate"]["steps"]:
        band = cfg.band_for(float(step["trigger_bcs"]))
        assert dca[band["key"]] > 0.0, f"매집 {step['trigger_bcs']} — {band['label']}"


def test_rearm_threshold_sits_between_the_two_ladders(cfg):
    """재무장 임계가 사다리 첫 계단 바깥이면 영원히 재무장되지 않거나 너무 쉽게 된다."""
    opp = float(cfg.hysteresis["rearm_opposite_bcs"])
    first_dist = min(float(s["trigger_bcs"]) for s in cfg.ladders["distribute"]["steps"])
    first_acc = max(float(s["trigger_bcs"]) for s in cfg.ladders["accumulate"]["steps"])
    assert -opp >= first_acc, "분배 재무장 임계가 매집 첫 계단보다 아래여야 한다"
    assert opp <= first_dist, "매집 재무장 임계가 분배 첫 계단보다 위여야 한다"


def test_invalidation_covers_family_independence(cfg):
    """합의 게이트가 계열 독립성을 전제하므로, 그게 깨질 조건을 미리 적어야 한다.

    실제로 Puell 이 GRM 과 ρ +0.89 였다 — 공급 계열에 가격 지표가 들어 있었고
    게이트는 그것을 '비가격 확인'으로 세고 있었다(docs/13).
    """
    ids = {i["id"] for i in cfg.invalidation}
    assert "INV-5" in ids
    inv5 = next(i for i in cfg.invalidation if i["id"] == "INV-5")
    assert "0.85" in inv5["text"]
