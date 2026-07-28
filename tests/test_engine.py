"""엔진 통합 — 데이터부터 실행 계획까지 한 줄로."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import yaml

import fixtures
from btc_core.config import load_config
from btc_core.datasources.base import DataBundle
from btc_core.datasources.csv_source import load_csv_bundle, save_csv_bundle
from btc_core.datasources.manual import load_manual
from btc_core.engine import evaluate
from btc_core.indicators import MarketData
from btc_core.report import display_width, pad, render_console, render_json, render_markdown
from btc_core.strategy import ExecutionState


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def bundle():
    return DataBundle(market=fixtures.market_data(), origin="synthetic")


@pytest.fixture
def manual_path(tmp_path, bundle):
    m = dict(fixtures.MANUAL_FULL)
    m["as_of"] = bundle.market.price.last_date.isoformat()
    p = tmp_path / "manual.yaml"
    p.write_text(yaml.safe_dump(m, allow_unicode=True), encoding="utf-8")
    return p


# --- 기본 경로 -------------------------------------------------------------

def test_full_evaluation_produces_a_complete_snapshot(cfg, bundle, manual_path):
    ref = bundle.market.price.last_date
    snap, _ = evaluate(cfg, bundle=bundle,
                       manual=load_manual(manual_path, reference=ref), as_of=ref)
    assert snap.bcs is not None
    assert -100 <= snap.bcs <= 100
    assert snap.coverage == pytest.approx(1.0)
    assert snap.lrs is not None
    assert snap.plan is not None
    assert snap.consensus is not None
    assert snap.price == pytest.approx(bundle.market.price.last)
    assert len(snap.families) == 4
    assert all(f.available for f in snap.families)


def test_price_only_data_still_scores_but_flags_coverage(cfg):
    md = MarketData(price=fixtures.price_series())
    snap, _ = evaluate(cfg, bundle=DataBundle(market=md, origin="price-only"))
    assert snap.bcs is not None
    assert snap.coverage == pytest.approx(0.25)     # 가격 계열(25) 만 살아남는다
    price = next(f for f in snap.families if f.key == "price")
    assert price.effective_weight == pytest.approx(100.0)
    assert any("커버리지" in w for w in snap.warnings)


def test_low_coverage_blocks_the_ladder(cfg):
    """계열 하나만 살아있으면 어떤 극단값이 나와도 실행하지 않는다."""
    md = MarketData(price=fixtures.price_series())
    snap, _ = evaluate(cfg, bundle=DataBundle(market=md, origin="price-only"))
    ladder = [a for a in snap.plan.actions if a.kind in ("distribute", "accumulate")]
    assert all(not a.executable for a in ladder)
    if ladder:
        assert "커버리지" in ladder[0].blocked_by


def test_manual_only_evaluation_works_without_any_time_series(cfg, tmp_path):
    ref = date(2026, 7, 28)
    m = dict(fixtures.MANUAL_FULL)
    m["as_of"] = ref.isoformat()
    p = tmp_path / "fresh.yaml"
    p.write_text(yaml.safe_dump(m, allow_unicode=True), encoding="utf-8")

    snap, _ = evaluate(cfg, bundle=None, manual=load_manual(p, reference=ref), as_of=ref)
    assert snap.bcs is not None
    holder = next(f for f in snap.families if f.key == "holder")
    assert holder.available
    assert holder.effective_weight == pytest.approx(100.0)   # 유일하게 살아있는 계열
    assert next(f for f in snap.families if f.key == "price").available is False


def test_no_data_at_all_yields_no_score(cfg):
    snap, _ = evaluate(cfg)
    assert snap.bcs is None
    assert snap.coverage == pytest.approx(0.0)
    assert snap.plan.band_key == "unknown"


# --- 수동 입력 -------------------------------------------------------------

def test_stale_manual_values_are_warned_about(cfg, tmp_path, bundle):
    m = dict(fixtures.MANUAL_FULL)
    m["as_of"] = (date(2026, 7, 28) - timedelta(days=60)).isoformat()
    p = tmp_path / "stale.yaml"
    p.write_text(yaml.safe_dump(m, allow_unicode=True), encoding="utf-8")
    manual = load_manual(p, reference=date(2026, 7, 28))
    assert any("갱신을 권합니다" in w for w in manual.warnings)
    assert manual.indicators["rhodl"] is not None       # 경고만, 값은 살아 있다


def test_expired_manual_values_are_dropped(cfg, tmp_path):
    m = dict(fixtures.MANUAL_FULL)
    m["as_of"] = (date(2026, 7, 28) - timedelta(days=200)).isoformat()
    p = tmp_path / "expired.yaml"
    p.write_text(yaml.safe_dump(m, allow_unicode=True), encoding="utf-8")
    manual = load_manual(p, reference=date(2026, 7, 28))
    assert manual.indicators["rhodl"] is None
    assert any("만료" in w for w in manual.warnings)


def test_per_item_observation_dates_are_honoured(cfg, tmp_path):
    p = tmp_path / "dated.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "as_of": "2026-07-28",
                "indicators": {
                    "rhodl": 0.5,
                    "reserve_risk": {"value": 0.003, "observed_on": "2026-01-01"},
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    manual = load_manual(p, reference=date(2026, 7, 28))
    assert manual.indicators["rhodl"] == 0.5
    assert manual.indicators["reserve_risk"] is None      # 209일 경과 → 만료


def test_a_missing_manual_file_is_not_fatal(cfg, tmp_path):
    manual = load_manual(tmp_path / "nope.yaml")
    assert manual.indicators == {}
    assert any("없음" in w for w in manual.warnings)


def test_manual_can_fill_in_for_a_failed_auto_indicator(cfg):
    """자동 소스가 비면 수동 입력이 대신 들어갈 수 있어야 한다."""
    from btc_core.datasources.manual import ManualInput

    md = MarketData(price=fixtures.price_series())
    snap, _ = evaluate(
        cfg,
        bundle=DataBundle(market=md, origin="price-only"),
        manual=ManualInput(as_of=None, indicators={"mvrv_z": 2.5, "nupl": 0.55}),
    )
    valuation = next(f for f in snap.families if f.key == "valuation")
    assert valuation.available


# --- 상태 누적 -------------------------------------------------------------

def test_evaluation_records_bcs_history(cfg, bundle, manual_path):
    ref = bundle.market.price.last_date
    state = ExecutionState()
    manual = load_manual(manual_path, reference=ref)
    for i in range(3):
        _, state = evaluate(cfg, bundle=bundle, manual=manual, state=state,
                            as_of=ref - timedelta(days=2 - i))
    assert len(state.bcs_history) == 3


def test_no_record_leaves_history_untouched(cfg, bundle, manual_path):
    ref = bundle.market.price.last_date
    state = ExecutionState()
    evaluate(cfg, bundle=bundle, manual=load_manual(manual_path, reference=ref),
             state=state, as_of=ref, record=False)
    assert state.bcs_history == []


def test_adaptive_weight_zero_disables_percentile_blending(cfg, bundle, manual_path):
    ref = bundle.market.price.last_date
    manual = load_manual(manual_path, reference=ref)
    plain, _ = evaluate(cfg, bundle=bundle, manual=manual, as_of=ref, adaptive_weight=0.0)
    mixed, _ = evaluate(cfg, bundle=bundle, manual=manual, as_of=ref, adaptive_weight=0.9)
    assert plain.bcs != mixed.bcs


# --- CSV 왕복 --------------------------------------------------------------

def test_csv_round_trip_preserves_the_score(cfg, bundle, manual_path, tmp_path):
    ref = bundle.market.price.last_date
    manual = load_manual(manual_path, reference=ref)
    before, _ = evaluate(cfg, bundle=bundle, manual=manual, as_of=ref)

    path = save_csv_bundle(bundle, tmp_path / "market.csv")
    reloaded = load_csv_bundle(path)
    after, _ = evaluate(cfg, bundle=reloaded, manual=manual, as_of=ref)

    assert after.bcs == pytest.approx(before.bcs, abs=0.01)
    assert after.price == pytest.approx(before.price)


def test_csv_loader_rejects_a_file_without_a_date_column(tmp_path):
    from btc_core.datasources.base import FetchError

    p = tmp_path / "bad.csv"
    p.write_text("day,price\n2020-01-01,100\n", encoding="utf-8")
    with pytest.raises(FetchError, match="date"):
        load_csv_bundle(p)


def test_csv_loader_rejects_a_bad_date(tmp_path):
    from btc_core.datasources.base import FetchError

    p = tmp_path / "bad.csv"
    p.write_text("date,price\n2020/01/01,100\n", encoding="utf-8")
    with pytest.raises(FetchError, match="날짜 형식"):
        load_csv_bundle(p)


def test_csv_loader_handles_blank_cells(tmp_path):
    p = tmp_path / "sparse.csv"
    p.write_text(
        "date,price,realized_cap\n2020-01-01,100,\n2020-01-02,110,\n", encoding="utf-8"
    )
    b = load_csv_bundle(p)
    assert len(b.market.price) == 2
    assert b.market.realized_cap is None


# --- 리포트 ----------------------------------------------------------------

def test_console_report_contains_the_essentials(cfg, bundle, manual_path):
    ref = bundle.market.price.last_date
    snap, _ = evaluate(cfg, bundle=bundle, manual=load_manual(manual_path, reference=ref), as_of=ref)
    out = render_console(snap, cfg)
    assert "BCS" in out and "LRS" in out
    assert "합의 게이트" in out
    assert "실행 계획" in out
    assert "투자 자문이 아닙니다" in out


def test_markdown_report_is_valid_and_has_a_journal_prompt(cfg, bundle, manual_path):
    ref = bundle.market.price.last_date
    snap, _ = evaluate(cfg, bundle=bundle, manual=load_manual(manual_path, reference=ref), as_of=ref)
    out = render_markdown(snap, cfg)
    assert out.startswith("# BCS 스냅샷")
    assert "| 계열 |" in out
    assert "이번 판단 기록" in out          # 원문 6.4


def test_json_report_round_trips(cfg, bundle, manual_path):
    import json

    ref = bundle.market.price.last_date
    snap, _ = evaluate(cfg, bundle=bundle, manual=load_manual(manual_path, reference=ref), as_of=ref)
    data = json.loads(render_json(snap))
    assert data["bcs"] == snap.bcs
    assert len(data["families"]) == 4
    assert data["plan"]["band_key"] == snap.plan.band_key


def test_reports_survive_an_empty_snapshot(cfg):
    snap, _ = evaluate(cfg)
    assert render_console(snap, cfg)
    assert render_markdown(snap, cfg)
    assert render_json(snap)


def test_padding_accounts_for_wide_characters():
    assert display_width("밸류에이션") == 10
    assert display_width("MVRV") == 4
    assert len(pad("밸류에이션", 16)) == 5 + 6      # 문자 5개 + 공백 6개
    assert display_width(pad("밸류에이션", 16)) == 16
    assert display_width(pad("MVRV", 16)) == 16


def test_padding_truncates_overlong_labels():
    out = pad("아주아주아주아주긴한글라벨입니다", 10)
    assert display_width(out) == 10
    # 2칸 문자로는 10칸을 딱 채울 수 없어 끝에 한 칸이 남을 수 있다
    assert out.rstrip().endswith("…")
    assert "긴한글라벨" not in out


# --- 사이클 전체를 훑는 회귀 테스트 ----------------------------------------

def test_bcs_tracks_the_synthetic_cycle(cfg):
    """저평가 → 과열 → 저평가 순서를 BCS 가 따라가는지."""
    md = fixtures.market_data()
    scores = []
    for d in (1500, 2000, 2600):
        snap, _ = evaluate(
            cfg, bundle=DataBundle(market=fixtures.truncated(md, d), origin="synthetic")
        )
        scores.append(snap.bcs)
    assert scores[0] < scores[1], "바닥에서 고점으로 갈 때 BCS 가 올라야 한다"
    assert scores[2] < scores[1], "고점 이후 BCS 가 내려와야 한다"


def test_scores_stay_inside_the_declared_range_across_the_whole_cycle(cfg):
    md = fixtures.market_data()
    for d in range(1450, 2601, 150):
        snap, _ = evaluate(
            cfg, bundle=DataBundle(market=fixtures.truncated(md, d), origin="synthetic")
        )
        assert snap.bcs is None or -100.0 <= snap.bcs <= 100.0
        for f in snap.families:
            assert f.score is None or -1.0 <= f.score <= 1.0


def test_bottom_profile_reaches_the_reports(cfg, bundle, manual_path):
    ref = bundle.market.price.last_date
    snap, _ = evaluate(cfg, bundle=bundle,
                       manual=load_manual(manual_path, reference=ref), as_of=ref)
    bp = snap.plan.bottom_profile
    assert bp is not None and bp.evaluable
    assert bp.total == len(cfg.bottom_profile["conditions"])

    console = render_console(snap, cfg)
    assert "저점 프로파일" in console
    assert "점수에 반영되지 않습니다" in console

    md = render_markdown(snap, cfg)
    assert "저점 프로파일" in md

    import json
    data = json.loads(render_json(snap))
    assert data["plan"]["bottom_profile"]["total"] == bp.total


def test_bottom_profile_inputs_from_price_only_data(cfg):
    """가격만 있어도 낙폭·경과일 두 조건은 평가된다."""
    from btc_core.indicators import bottom_profile_inputs

    md = MarketData(price=fixtures.price_series())
    vals = bottom_profile_inputs(md)
    assert vals["drawdown_pct"] is not None
    assert vals["days_since_ath"] is not None
    assert vals["mvrv"] is None
