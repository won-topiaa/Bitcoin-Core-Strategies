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


def test_no_single_family_dominates(cfg):
    """어떤 계열도 30을 넘지 않는다 — 한 아이디어가 판단을 독점하지 못하게."""
    assert max(f["weight"] for f in cfg.bcs_families.values()) <= 30


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
