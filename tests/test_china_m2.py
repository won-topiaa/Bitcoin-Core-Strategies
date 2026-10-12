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


# --------------------------------------------------------------------------
# 2026-10 검토에서 확인된 경계
# --------------------------------------------------------------------------
def test_a_truncated_reply_is_caught_and_the_next_source_is_asked(monkeypatch):
    """IncompleteRead 는 OSError 가 아니다 — 예전엔 collect 밖으로 새어 fetch_macro 를 통째로
    죽여, 이미 받은 FRED·GitHub 열까지 그 실행에서 버렸다."""
    from http.client import IncompleteRead
    stored = stored_until(date(2026, 5, 1))
    full = dict(stored)
    full.update(PBOC_2026)

    def get(url, **kw):
        if "pbc.gov.cn" in url:
            raise IncompleteRead(b"x" * 24, 976)
        raise cm.SourceError("막힘")
    monkeypatch.setattr(cm, "fetch_nbs", lambda today, g, sleep=None: dict(full))
    got, src = cm.collect(stored, TODAY, get=get, log=lambda *a: None)
    assert src == "NBS"


def test_nbs_retries_a_truncated_body():
    from http.client import IncompleteRead
    body = (FX / "nbs_esdata.json").read_bytes()
    n = {"i": 0}

    def get(url, **kw):
        n["i"] += 1
        if n["i"] == 1:
            raise IncompleteRead(b"x", 10)
        return body
    assert cm.fetch_nbs(TODAY, get, sleep=lambda s: None)[date(2026, 8, 1)] == 3568083.60


@pytest.mark.parametrize("body", [b"null", b"[]", b'{"success":true,"data":[null]}',
                                  b'{"success":true,"data":[{"code":"202608MM","values":[null]}]}'])
def test_odd_nbs_shapes_are_source_errors(body):
    with pytest.raises(cm.SourceError):
        cm.parse_nbs(json.loads(body))


def test_a_china_m2_crash_never_loses_the_other_macro_columns(tmp_path, monkeypatch):
    import fetch_macro as fm
    d = date(2026, 10, 9)
    monkeypatch.setattr(fm, "fetch_github", lambda *a, **k: {"vix": {d: 99.0}})
    monkeypatch.setattr(fm, "fetch_series", lambda fid, timeout=40: None)

    def boom(*a, **k):
        raise RuntimeError("예상 못 한 실패")
    monkeypatch.setattr(fm, "fetch_china_m2", boom)
    saved = {}
    monkeypatch.setattr(fm, "merge_to_csv", lambda cols, path: saved.update(cols))
    assert fm.main(["--github", "--global", "--out", str(tmp_path / "m.csv")]) == 0
    assert saved.get("vix") == {d: 99.0}, "중국 M2 실패가 다른 열의 병합을 막았습니다"


def test_a_source_with_no_new_month_does_not_stop_the_chain(monkeypatch):
    """인민은행 표가 낡은 파일을 계속 가리켜도 '성공'으로 받으면 국가통계국은 영영 묻지
    않는다 — chinadata 가 다섯 달 멈춘 것과 같은 꼴이다."""
    stored = stored_until(date(2026, 8, 1))
    stale_pboc = {d: v for d, v in PBOC_2026.items()}            # 2026-08 까지뿐
    full = dict(stored)
    full[date(2026, 9, 1)] = 3580000.0                            # 국가통계국에는 9월이 있다
    monkeypatch.setattr(cm, "fetch_pboc", lambda today, get: dict(stale_pboc))
    monkeypatch.setattr(cm, "fetch_nbs", lambda today, get, sleep=None: dict(full))
    got, src = cm.collect(stored, TODAY, get=Web({}), log=lambda *a: None)
    assert src == "NBS" and date(2026, 9, 1) in got


def test_when_nobody_has_the_new_month_the_answer_is_kept_and_late_is_flagged(monkeypatch):
    stored = stored_until(date(2026, 8, 1))
    monkeypatch.setattr(cm, "fetch_pboc", lambda today, get: dict(PBOC_2026))
    monkeypatch.setattr(cm, "fetch_nbs", lambda today, get, sleep=None: (_ for _ in ()).throw(cm.SourceError("x")))
    monkeypatch.setattr(cm, "fetch_oecd", lambda get: {date(2026, 7, 1): 3555077.0})
    early, late = [], []
    got, src = cm.collect(stored, date(2026, 10, 12), get=Web({}), log=early.append)
    assert src == "PBoC(새 달 없음)" and got == PBOC_2026
    assert not any(l.startswith("::warning::") for l in early), "발표 창(10~15일)에 매시간 울립니다"
    cm.collect(stored, date(2026, 10, 21), get=Web({}), log=late.append)
    assert any("나아가지 않습니다" in l for l in late), "20일이 넘도록 새 달이 없는데 조용합니다"


def test_a_partial_source_that_leaves_a_hole_is_refused(monkeypatch):
    """저장값이 2024-10 에서 끝났는데 인민은행 2년치(2025-01~)만 들이면 달은 최신인데
    임펄스는 몇 해 동안 None 이 된다 — 화면은 '최신'이라 하면서 읽기 줄만 빈다."""
    stored = stored_until(date(2024, 10, 1))
    pboc = {d: v for d, v in stored_until(date(2026, 8, 1)).items() if d >= date(2025, 1, 1)}
    pboc.update(PBOC_2026)
    lines = []
    monkeypatch.setattr(cm, "fetch_pboc", lambda today, get: dict(pboc))
    monkeypatch.setattr(cm, "fetch_nbs", lambda today, get, sleep=None: (_ for _ in ()).throw(cm.SourceError("x")))
    monkeypatch.setattr(cm, "fetch_oecd", lambda get: (_ for _ in ()).throw(cm.SourceError("x")))
    got, src = cm.collect(stored, TODAY, get=Web({}), log=lines.append)
    assert got == {} and any("이어지지 않습니다" in l for l in lines), lines


def test_with_no_history_only_a_full_history_source_is_used(monkeypatch):
    monkeypatch.setattr(cm, "fetch_pboc", lambda today, get: dict(PBOC_2026))
    monkeypatch.setattr(cm, "fetch_nbs", lambda today, get, sleep=None: (_ for _ in ()).throw(cm.SourceError("x")))
    monkeypatch.setattr(cm, "fetch_oecd", lambda get: dict(PBOC_2026))
    got, src = cm.collect({}, TODAY, get=Web({}), log=lambda *a: None)
    assert got == {}, "2년치·OECD 로 빈 이력을 채웠습니다(재작성 전 2017 년·24개월 창 부족)"


def test_a_future_dated_cell_does_not_freeze_the_fetch(monkeypatch):
    stored = stored_until(date(2026, 5, 1))
    stored[date(2031, 1, 1)] = 3600000.0
    lines = []
    monkeypatch.setattr(cm, "fetch_pboc", lambda today, get: dict(PBOC_2026))
    got, src = cm.collect(stored, TODAY, get=Web({}), log=lines.append)
    assert src == "PBoC" and date(2026, 8, 1) in got
    assert any("미래 달" in l for l in lines)


def test_a_blank_middle_cell_does_not_shift_later_months():
    """빈 칸을 버리고 순서로 짝지으면 뒤의 값이 전부 한 달씩 밀린다."""
    raw = cm._decode((FX / "pbc_m2_2026.htm").read_bytes())
    i = raw.index("3492159.91")
    holed = raw[:i] + raw[i + len("3492159.91"):]                 # 2026-02 칸만 비운다
    got = cm.parse_pboc_table(holed)
    assert date(2026, 2, 1) not in got
    assert got[date(2026, 3, 1)] == PBOC_2026[date(2026, 3, 1)]
    assert got[date(2026, 8, 1)] == PBOC_2026[date(2026, 8, 1)]


def test_the_supply_link_search_stays_inside_its_table():
    ov = (FX / "pbc_2026_hbtjgl.html").read_text(encoding="utf-8")
    no_htm = ov.replace('2026091417313352505.htm', '2026091417313352505.pdf')
    assert cm.pboc_supply_table_link(no_htm) is None, "옆 표(汇率报表)의 htm 을 집었습니다"


def test_one_year_page_failing_keeps_the_other_years_table():
    routes = pboc_routes()
    routes["/5570903/index.html"] = lambda: (_ for _ in ()).throw(ConnectionResetError(104, "reset"))
    assert cm.fetch_pboc(TODAY, Web(routes)) == PBOC_2026
