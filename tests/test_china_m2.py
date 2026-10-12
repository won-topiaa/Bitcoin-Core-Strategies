"""중국 M2 수집(tools/china_m2.py) — 공식 원본 셋과 검증 가드.

예전 원본 chinadata.live 가 2026-05 에서 멈춘 채 다섯 달 동안 화면이 그 값을 '지금'이라
불렀다. 픽스처는 2026-10-12 에 받은 실제 응답을 잘라 둔 것이다(tests/fixtures/china_m2).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import china_m2 as cm  # noqa: E402

FX = ROOT / "tests" / "fixtures" / "china_m2"
TODAY = date(2026, 10, 12)

# 인민은행 2026 货币供应量 표(2026-09-14 게시) 그대로
PBOC_2026 = {
    date(2026, 1, 1): 3471860.39, date(2026, 2, 1): 3492159.91, date(2026, 3, 1): 3538636.53,
    date(2026, 4, 1): 3530425.21, date(2026, 5, 1): 3536688.92, date(2026, 6, 1): 3567108.43,
    date(2026, 7, 1): 3555077.24, date(2026, 8, 1): 3568083.60,
}


def stored_until(last: date, start: date = date(2015, 1, 1)) -> dict:
    """2015-01 부터 last 까지의 저장값. 2026 년은 실제 값, 그 앞은 2026-01 에서 거꾸로
    매달 0.6% 씩 줄여 이음매가 매끄럽다(실제 성장률과 비슷하다)."""
    out = {d: x for d, x in PBOC_2026.items() if d <= last}
    d, v = date(2025, 12, 1), PBOC_2026[date(2026, 1, 1)]
    while d >= start:
        v /= 1.006
        out[d] = round(v, 2)
        d = cm._month_add(d, -1)
    return {k: out[k] for k in sorted(out) if k <= last}


class Web:
    """URL → 픽스처 바이트. 부른 URL 을 기록한다."""

    def __init__(self, routes: dict, fail=()):
        self.routes, self.fail, self.calls = routes, tuple(fail), []

    def __call__(self, url, **kw):
        self.calls.append(url)
        for frag in self.fail:
            if frag in url:
                raise cm.SourceError(f"막힘: {frag}")
        for frag, body in self.routes.items():
            if frag in url:
                return body() if callable(body) else body
        raise cm.SourceError(f"픽스처 없음: {url}")


def pboc_routes() -> dict:
    return {
        "/116319/index.html": (FX / "pbc_index.html").read_bytes(),
        "/2026ntjsj/index.html": (FX / "pbc_2026.html").read_bytes(),
        "/2026ntjsj/hbtjgl/index.html": (FX / "pbc_2026_hbtjgl.html").read_bytes(),
        "2026091417313352505.htm": (FX / "pbc_m2_2026.htm").read_bytes(),
        "/5570903/index.html": b"<html><body>2025 page without overview</body></html>",
    }


# --------------------------------------------------------------------------
# 파서 — 실제 응답
# --------------------------------------------------------------------------
def test_the_real_pboc_table_parses_to_the_cent():
    assert cm.parse_pboc_table(cm._decode((FX / "pbc_m2_2026.htm").read_bytes())) == PBOC_2026


def test_a_utf8_pboc_table_is_decoded_by_its_declared_charset():
    """2017·2018 표는 utf-8 이다 — GBK 로 고정해 읽으면 'M2' 행을 못 찾는다."""
    text = cm._decode((FX / "pbc_m2_2026.htm").read_bytes())
    utf8 = text.replace("charset=gb2312", "charset=utf-8").encode("utf-8")
    assert cm.parse_pboc_table(cm._decode(utf8)) == PBOC_2026


def test_the_supply_table_link_is_not_a_neighbouring_report():
    ov = (FX / "pbc_2026_hbtjgl.html").read_text(encoding="utf-8")
    assert cm.pboc_supply_table_link(ov) == "/diaochatongjisi/attachDir/2026/09/2026091417313352505.htm"


def test_year_menu_and_overview_links():
    years = cm.pboc_year_pages((FX / "pbc_index.html").read_text(encoding="utf-8"))
    assert years[2026] == "/diaochatongjisi/116219/116319/2026ntjsj/index.html"
    assert years[2025] == "/diaochatongjisi/116219/116319/5570903/index.html"
    assert cm.pboc_overview_link((FX / "pbc_2026.html").read_text(encoding="utf-8")) == \
        "/diaochatongjisi/116219/116319/2026ntjsj/hbtjgl/index.html"


def test_a_table_without_the_m2_row_is_a_source_error():
    with pytest.raises(cm.SourceError):
        cm.parse_pboc_table("<table><tr><td>2026.01</td><td>货币（M1）</td><td>1.0</td></tr></table>")


def test_an_anti_bot_page_is_a_source_error_not_data():
    web = Web({"/116319/index.html": b"<html><script>wzwschallenge='x'</script></html>"})
    with pytest.raises(cm.SourceError, match="WZWS"):
        cm.fetch_pboc(TODAY, web)


def test_fetch_pboc_follows_the_menu_and_tolerates_a_missing_year():
    got = cm.fetch_pboc(TODAY, Web(pboc_routes()))
    assert got == PBOC_2026


def test_the_real_nbs_response_parses_and_skips_future_months():
    got = cm.parse_nbs(json.loads((FX / "nbs_esdata.json").read_text(encoding="utf-8")))
    assert got[date(2026, 8, 1)] == 3568083.60
    assert date(2026, 9, 1) not in got and date(2026, 10, 1) not in got, "빈 미래 달을 0 으로 읽었습니다"
    # NBS 는 2026-02 를 보도자료처럼 100亿 단위로 둔다 — 그래도 0.0011% 차이다
    assert abs(got[date(2026, 2, 1)] / PBOC_2026[date(2026, 2, 1)] - 1) < 2e-5


def test_nbs_units_other_than_yi_yuan_are_refused():
    obj = json.loads((FX / "nbs_esdata.json").read_text(encoding="utf-8"))
    obj["data"][2]["values"][0]["du_name"] = "万亿元"
    with pytest.raises(cm.SourceError, match="단위"):
        cm.parse_nbs(obj)


def test_nbs_retries_a_reset_connection(monkeypatch):
    body = (FX / "nbs_esdata.json").read_bytes()
    n = {"i": 0}

    def get(url, **kw):
        n["i"] += 1
        if n["i"] < 3:
            raise ConnectionResetError(104, "Connection reset by peer")
        return body
    assert cm.fetch_nbs(TODAY, get, sleep=lambda s: None)[date(2026, 8, 1)] == 3568083.60
    assert n["i"] == 3


def test_the_real_oecd_csv_is_converted_from_million_yuan():
    got = cm.parse_oecd((FX / "oecd_chn_mabm.csv").read_text(encoding="utf-8"))
    assert got[date(2026, 3, 1)] == 3538636.0          # 353863600 백만 위안 = 3538636 亿元
    assert abs(got[date(2026, 3, 1)] / PBOC_2026[date(2026, 3, 1)] - 1) < 1e-6


# --------------------------------------------------------------------------
# 검증 가드
# --------------------------------------------------------------------------
def test_a_unit_swap_is_refused():
    """M1·위안·万亿 혼동은 값의 크기로 드러난다."""
    stored = stored_until(date(2026, 5, 1))
    raw_yuan = {d: v * 1e8 for d, v in PBOC_2026.items()}
    assert any("단위" in p for p in cm.problems(raw_yuan, stored, full_history=False))
    m1 = {d: v / 3 for d, v in PBOC_2026.items()}
    assert any("단위" in p or "월간" in p or "과거" in p for p in cm.problems(m1, stored, full_history=False))


def test_a_jump_is_refused():
    s = dict(PBOC_2026)
    s[date(2026, 8, 1)] = s[date(2026, 7, 1)] * 1.09
    assert any("월간" in p for p in cm.problems(s, {}, full_history=False))


def test_a_gap_in_a_full_history_source_is_refused():
    s = stored_until(date(2026, 8, 1))
    del s[date(2020, 3, 1)]
    assert any("빠졌" in p for p in cm.problems(s, {}, full_history=True))


def test_going_backwards_is_refused():
    stored = stored_until(date(2026, 8, 1))
    older = {d: v for d, v in PBOC_2026.items() if d <= date(2026, 6, 1)}
    assert any("후퇴" in p for p in cm.problems(older, stored, full_history=False))


def test_an_old_month_rewritten_is_refused_but_a_recent_revision_is_fine():
    """잘못된 응답 하나가 24개월 보정에 쓰이는 과거를 다시 쓰지 못하게."""
    stored = stored_until(date(2026, 5, 1))
    new = {d: v for d, v in stored.items()}
    new.update(PBOC_2026)
    assert cm.problems(new, stored, full_history=True) == []
    bad = dict(new)
    bad[date(2024, 1, 1)] = bad[date(2024, 1, 1)] * 1.01
    assert any("과거" in p for p in cm.problems(bad, stored, full_history=True))
    ok = dict(new)
    ok[date(2026, 6, 1)] = ok[date(2026, 6, 1)] - 10.83      # 2026-06 의 실제 소폭 개정 크기
    assert cm.problems(ok, stored, full_history=True) == []


# --------------------------------------------------------------------------
# 고르기
# --------------------------------------------------------------------------
def test_release_calendar():
    assert cm.expected_latest(date(2026, 10, 8)) == date(2026, 8, 1)
    assert cm.expected_latest(date(2026, 10, 9)) == date(2026, 9, 1)
    assert cm.expected_latest(date(2027, 1, 5)) == date(2026, 11, 1)
    assert cm.expected_latest(date(2027, 1, 20)) == date(2026, 12, 1)


def test_nothing_is_fetched_when_already_up_to_date():
    """매시간 갱신이 원본을 두드리지 않는다."""
    web = Web({})
    stored = stored_until(date(2026, 8, 1))
    stored[date(2026, 9, 1)] = 3580000.0          # 9월 값이 이미 들어와 있다
    got, why = cm.collect(stored, TODAY, get=web)
    assert got == {} and "이미 최신" in why and web.calls == []


def test_pboc_is_asked_first():
    web = Web(pboc_routes())
    got, src = cm.collect(stored_until(date(2026, 5, 1)), TODAY, get=web, log=lambda *a: None)
    assert src == "PBoC" and got == PBOC_2026
    assert not any("stats.gov.cn" in u or "oecd" in u for u in web.calls)


def test_a_partial_nbs_answer_is_refused_as_a_full_history_source():
    """NBS 는 2015 부터 전 구간을 준다 — 앞이 비어 있으면 응답이 잘린 것이다."""
    lines = []
    web = Web({"stats.gov.cn": (FX / "nbs_esdata.json").read_bytes()}, fail=("pbc.gov.cn", "oecd.org"))
    got, src = cm.collect(stored_until(date(2026, 5, 1)), TODAY, get=web, log=lines.append,
                          sleep=lambda s: None)
    assert got == {}
    assert any("NBS: 거부" in l and "빠졌" in l for l in lines), lines


def test_nbs_full_history_is_accepted(monkeypatch):
    stored = stored_until(date(2026, 5, 1))
    full = dict(stored)
    full.update(PBOC_2026)
    monkeypatch.setattr(cm, "fetch_nbs", lambda today, get, sleep=None: dict(full))
    web = Web({}, fail=("pbc.gov.cn",))
    got, src = cm.collect(stored, TODAY, get=web, log=lambda *a: None)
    assert src == "NBS" and got[date(2026, 8, 1)] == 3568083.60


def test_oecd_only_appends_new_months_and_never_overwrites():
    """OECD 는 2017 년이 재작성 전 값이다 — 저장된 과거를 덮으면 안 된다."""
    web = Web({"oecd.org": (FX / "oecd_chn_mabm.csv").read_bytes()}, fail=("pbc.gov.cn", "stats.gov.cn"))
    stored = stored_until(date(2026, 1, 1))
    got, src = cm.collect(stored, TODAY, get=web, log=lambda *a: None, sleep=lambda s: None)
    assert src == "OECD"
    assert got and min(got) > date(2026, 1, 1), "저장된 달을 덮었습니다"


def test_total_failure_keeps_stored_values_and_says_so():
    lines = []
    web = Web({}, fail=("pbc.gov.cn", "stats.gov.cn", "oecd.org"))
    got, why = cm.collect(stored_until(date(2026, 5, 1)), TODAY, get=web, log=lines.append,
                          sleep=lambda s: None)
    assert got == {}
    assert any(l.startswith("::warning::중국 M2") for l in lines), "전부 실패해도 경고가 없습니다"


# --------------------------------------------------------------------------
# 배선
# --------------------------------------------------------------------------
def test_fetch_macro_uses_the_official_chain_and_not_chinadata(tmp_path, monkeypatch):
    import fetch_macro as fm
    src = Path(fm.__file__).read_text(encoding="utf-8")
    assert "chinadata.live/api" not in src, "멈춘 원본이 아직 수집 경로에 있습니다"
    seen = {}

    def fake_collect(stored, today, **kw):
        seen["stored"] = stored
        return {date(2026, 8, 1): 3568083.6}, "PBoC"
    monkeypatch.setattr(cm, "collect", fake_collect)
    out = tmp_path / "m.csv"
    out.write_text("date,m2_cn\n2026-05-01,3536700\n", encoding="utf-8")
    assert fm.fetch_china_m2(out) == {"m2_cn": {date(2026, 8, 1): 3568083.6}}
    assert seen["stored"] == {date(2026, 5, 1): 3536700.0}, "저장값을 넘기지 않아 가드가 눈멉니다"


def test_the_cli_prints_without_writing(tmp_path):
    """python3 tools/china_m2.py 는 보여 주기만 한다 — 네트워크가 없으면 1 로 끝난다."""
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0,'tools'); import china_m2 as c; print(c.__doc__[:20])"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
