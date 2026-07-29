"""CLI — 명령이 실제로 끝까지 돌아가는지."""

from __future__ import annotations

import json
from datetime import date

import pytest
import yaml

import fixtures
from btc_core.cli import main
from btc_core.datasources.base import DataBundle
from btc_core.datasources.csv_source import save_csv_bundle
from btc_core.strategy import ExecutionState


@pytest.fixture(scope="module")
def market_csv(tmp_path_factory):
    p = tmp_path_factory.mktemp("data") / "market.csv"
    save_csv_bundle(DataBundle(market=fixtures.market_data(), origin="synthetic"), p)
    return p


@pytest.fixture
def manual_file(tmp_path, market_csv):
    from btc_core.datasources.csv_source import load_csv_bundle

    ref = load_csv_bundle(market_csv).market.price.last_date
    m = dict(fixtures.MANUAL_FULL)
    m["as_of"] = ref.isoformat()
    p = tmp_path / "manual.yaml"
    p.write_text(yaml.safe_dump(m, allow_unicode=True), encoding="utf-8")
    return p


def run(*argv):
    return main(list(argv))


# --- score -----------------------------------------------------------------

def test_score_runs_and_prints_a_report(market_csv, manual_file, tmp_path, capsys):
    code = run("score", "--csv", str(market_csv), "--manual", str(manual_file),
               "--state", str(tmp_path / "state.json"))
    assert code == 0
    out = capsys.readouterr().out
    assert "BCS" in out and "실행 계획" in out


def test_score_writes_markdown_to_a_file(market_csv, manual_file, tmp_path):
    out_path = tmp_path / "snap.md"
    code = run("score", "--csv", str(market_csv), "--manual", str(manual_file),
               "--state", str(tmp_path / "state.json"),
               "--format", "markdown", "--out", str(out_path))
    assert code == 0
    text = out_path.read_text(encoding="utf-8")
    assert text.startswith("# BCS 스냅샷")


def test_score_emits_parseable_json(market_csv, manual_file, tmp_path, capsys):
    code = run("score", "--csv", str(market_csv), "--manual", str(manual_file),
               "--state", str(tmp_path / "state.json"), "--format", "json")
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert "bcs" in data and "plan" in data
    assert len(data["families"]) == 3


def test_score_persists_state_between_runs(market_csv, manual_file, tmp_path):
    state_path = tmp_path / "state.json"
    run("score", "--csv", str(market_csv), "--manual", str(manual_file),
        "--state", str(state_path), "--as-of", "2025-02-10")
    run("score", "--csv", str(market_csv), "--manual", str(manual_file),
        "--state", str(state_path), "--as-of", "2025-02-11")
    st = ExecutionState.load(state_path)
    assert len(st.bcs_history) == 2


def test_score_survives_a_missing_csv(tmp_path, capsys):
    code = run("score", "--csv", str(tmp_path / "nope.csv"),
               "--manual", str(tmp_path / "none.yaml"),
               "--state", str(tmp_path / "state.json"))
    assert code == 0                       # 데이터가 없어도 리포트는 나온다
    assert "산출 불가" in capsys.readouterr().out


def test_score_works_with_no_manual_file(market_csv, tmp_path, capsys):
    code = run("score", "--csv", str(market_csv),
               "--manual", str(tmp_path / "absent.yaml"),
               "--state", str(tmp_path / "state.json"))
    assert code == 0
    assert "커버리지" in capsys.readouterr().out


# --- init ------------------------------------------------------------------

def test_init_creates_a_template(tmp_path, capsys):
    p = tmp_path / "manual_input.yaml"
    assert run("init", "--path", str(p)) == 0
    assert p.exists()
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "macro" in data and "floors" in data


def test_init_refuses_to_clobber_without_force(tmp_path):
    p = tmp_path / "manual_input.yaml"
    run("init", "--path", str(p))
    assert run("init", "--path", str(p)) == 1
    assert run("init", "--path", str(p), "--force") == 0


def test_generated_template_is_a_valid_manual_input(tmp_path):
    from btc_core.datasources.manual import load_manual

    p = tmp_path / "m.yaml"
    run("init", "--path", str(p))
    manual = load_manual(p, reference=date(2026, 7, 28))
    assert manual.floors.get("cvdd") is not None
    assert manual.macro.get("fed_stance") == "pause_easing"


# --- explain / history -----------------------------------------------------

def test_explain_prints_one_indicator(capsys):
    assert run("explain", "mvrv_z") == 0
    out = capsys.readouterr().out
    assert "MVRV Z-Score" in out and "원시값 → 점수" in out


def test_explain_prints_categorical_states(capsys):
    assert run("explain", "hash_ribbons") == 0
    out = capsys.readouterr().out
    assert "recovery" in out and "상태 → 점수" in out


def test_explain_lists_everything_by_default(capsys):
    assert run("explain") == 0
    out = capsys.readouterr().out
    assert "Puell Multiple" in out and "MVRV Z-Score" in out


def test_explain_rejects_an_unknown_indicator(capsys):
    assert run("explain", "없는지표") == 1
    assert "알 수 없는 지표" in capsys.readouterr().err


def test_history_is_empty_on_a_fresh_state(tmp_path, capsys):
    assert run("history", "--state", str(tmp_path / "state.json")) == 0
    assert "실행 이력이 없습니다" in capsys.readouterr().out


# --- commit ----------------------------------------------------------------

def test_commit_records_a_confirmed_step(market_csv, manual_file, tmp_path, capsys):
    """3일 연속 임계 유지 후에야 계단을 기록할 수 있다."""
    state_path = tmp_path / "state.json"
    st = ExecutionState()
    for d in ("2025-02-08", "2025-02-09", "2025-02-10"):
        st.record_bcs(date.fromisoformat(d), -60.0)
    st.save(state_path)

    code = run("commit", "--ladder", "accumulate", "--csv", str(market_csv),
               "--manual", str(manual_file), "--state", str(state_path),
               "--on", "2025-02-10", "--note", "CLI 테스트", "--force")
    assert code == 0
    assert "기록" in capsys.readouterr().out
    assert ExecutionState.load(state_path).cumulative("accumulate") > 0


def test_commit_refuses_a_blocked_step_without_force(market_csv, manual_file, tmp_path, capsys):
    code = run("commit", "--ladder", "accumulate", "--csv", str(market_csv),
               "--manual", str(manual_file), "--state", str(tmp_path / "state.json"),
               "--on", "2025-02-12")
    assert code == 1
    assert "보류" in capsys.readouterr().err


def test_commit_reports_when_no_rung_applies(market_csv, manual_file, tmp_path, capsys):
    code = run("commit", "--ladder", "distribute", "--csv", str(market_csv),
               "--manual", str(manual_file), "--state", str(tmp_path / "state.json"),
               "--on", "2025-02-12")
    assert code == 1
    assert "계단이 없습니다" in capsys.readouterr().err


def test_history_shows_a_committed_step(market_csv, manual_file, tmp_path, capsys):
    state_path = tmp_path / "state.json"
    st = ExecutionState()
    for d in ("2025-02-08", "2025-02-09", "2025-02-10"):
        st.record_bcs(date.fromisoformat(d), -60.0)
    st.save(state_path)
    run("commit", "--ladder", "accumulate", "--csv", str(market_csv),
        "--manual", str(manual_file), "--state", str(state_path),
        "--on", "2025-02-10", "--force")

    assert run("history", "--state", str(state_path)) == 0
    out = capsys.readouterr().out
    assert "accumulate" in out and "누적" in out


# --- 설정 오류 -------------------------------------------------------------

def test_a_broken_config_exits_with_a_message(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("meta: {name: x}\n", encoding="utf-8")
    assert run("--config", str(bad), "explain") == 2
    assert "설정 오류" in capsys.readouterr().err
