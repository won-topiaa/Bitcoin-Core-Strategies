"""자동 갱신이 조용히 데이터를 망가뜨리지 않게.

이 파일이 검사하는 것은 "잘 이어 붙이는가"가 아니라 **"잘못 이어 붙이기를
거부하는가"** 다. data/market.csv 는 16년치 관측이고 이 시스템의 유일한
입력인데, 자동 갱신을 붙이는 순간 사람이 안 보는 동안 그 파일을 덮어쓰는 코드가
생긴다. 그러니 검사해야 할 것은 정상 경로가 아니라 **거부 경로**다.

CSV 가 .gitignore 에 있어서 git 으로 되돌릴 수도 없다. 그래서 잘못 쓴 뒤에
고치는 게 아니라 애초에 쓰지 않는 쪽이어야 한다.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import refresh_data as rd  # noqa: E402

from btc_core.datasources.csv_source import read_cells, write_cells  # noqa: E402

D0 = date(2026, 1, 1)


def cells(n: int = 40, price: float = 60000.0, step: float = 1.0) -> dict:
    """가격만 있는 깨끗한 시계열."""
    return {D0 + timedelta(days=i): {"price": price + i * step} for i in range(n)}


# --------------------------------------------------------------------------
# 받은 것 자체가 온전한가
# --------------------------------------------------------------------------
def test_empty_fetch_is_refused():
    with pytest.raises(rd.RefuseToWrite):
        rd.check_fetched({})


def test_fetch_without_price_is_refused():
    only_caps = {D0: {"market_cap": 1.0}, D0 + timedelta(days=1): {"supply": 2.0}}
    with pytest.raises(rd.RefuseToWrite):
        rd.check_fetched(only_caps)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_nonsense_price_is_refused(bad):
    c = cells(10)
    c[D0 + timedelta(days=5)]["price"] = bad
    with pytest.raises(rd.RefuseToWrite):
        rd.check_fetched(c)


def test_future_dates_are_refused():
    c = {date.today() + timedelta(days=9): {"price": 1.0},
         date.today(): {"price": 1.0}}
    with pytest.raises(rd.RefuseToWrite):
        rd.check_fetched(c, today=date.today())


def test_unit_change_is_refused():
    """센트로 바뀌거나 1000배가 되는 응답. 하루에 5배는 데이터 오류다."""
    c = cells(10)
    c[D0 + timedelta(days=5)]["price"] = 6_000_000.0
    with pytest.raises(rd.RefuseToWrite) as e:
        rd.check_fetched(c, today=date(2026, 3, 1))
    assert "단위" in str(e.value)


def test_a_real_crash_day_only_warns():
    """실제 비트코인의 최악 하루는 −49% 다. 그날 갱신이 멈추면 안 된다."""
    c = cells(10)
    for i in range(5, 10):
        c[D0 + timedelta(days=i)]["price"] = 30_000.0
    notes = rd.check_fetched(c, today=date(2026, 3, 1))
    assert any("움직였" in n for n in notes)


def test_gap_in_dates_is_not_treated_as_a_jump():
    """구멍 건너뛴 두 날을 하루 변동으로 세면 안 된다."""
    c = {D0: {"price": 100.0}, D0 + timedelta(days=400): {"price": 60_000.0}}
    assert rd.check_fetched(c, today=date(2027, 6, 1)) == []


# --------------------------------------------------------------------------
# 겹침 대조 — 이 도구의 핵심
# --------------------------------------------------------------------------
def test_disagreeing_overlap_is_refused():
    old = cells(40)
    new = {d: {"price": v["price"] * 20} for d, v in cells(40).items()}
    with pytest.raises(rd.RefuseToWrite) as e:
        rd.check_overlap(old, new)
    assert "다른 자산" in str(e.value)


def test_thin_overlap_is_refused_when_the_file_could_give_more():
    """창을 좁게 잡아 겹침을 못 만든 경우 — 늘리면 된다고 말해야 한다."""
    old = cells(40)
    new = {d: c for d, c in cells(42).items() if d >= D0 + timedelta(days=38)}
    with pytest.raises(rd.RefuseToWrite) as e:
        rd.check_overlap(old, new)
    assert "--days" in str(e.value)


def test_a_short_existing_file_is_compared_with_what_it_has():
    """기존 파일이 3일뿐이면 5일을 겹치라고 요구할 수 없다. 늘려도 안 되는 것을
    늘리라고 말하는 셈이다."""
    old = {d: c for d, c in cells(3).items()}
    notes = rd.check_overlap(old, cells(40))
    assert any("3일뿐이라" in n for n in notes)
    assert any("대조 통과" in n for n in notes)


def test_no_shared_dates_at_all_is_refused():
    old = cells(3)
    new = {d + timedelta(days=900): c for d, c in cells(40).items()}
    with pytest.raises(rd.RefuseToWrite) as e:
        rd.check_overlap(old, new)
    assert "겹치지 않습니다" in str(e.value)


def test_a_short_overlap_allows_no_disagreement():
    """겹침 1일에 3일까지 허용하면 그 1일이 20배 달라도 통과한다."""
    old = cells(2)
    new = {d: {"price": c["price"] * 20} for d, c in cells(40).items()}
    with pytest.raises(rd.RefuseToWrite) as e:
        rd.check_overlap(old, new)
    assert "허용 0일" in str(e.value)


def test_first_collection_needs_no_overlap():
    notes = rd.check_overlap({}, cells(40))
    assert any("첫 수집" in n for n in notes)


def test_a_few_revised_days_are_allowed_and_reported():
    """공급자가 최근 며칠을 고쳐 주는 것은 정상이다. 다만 조용히 넘기지 않는다."""
    old = cells(40)
    new = {d: dict(c) for d, c in cells(40).items()}
    for i in (37, 38):
        new[D0 + timedelta(days=i)]["price"] *= 1.10
    notes = rd.check_overlap(old, new)
    assert any("갱신됐습니다" in n for n in notes)


def test_wholesale_revision_is_refused():
    old = cells(40)
    new = {d: dict(c) for d, c in cells(40).items()}
    for i in range(30, 40):
        new[D0 + timedelta(days=i)]["price"] *= 1.10
    with pytest.raises(rd.RefuseToWrite):
        rd.check_overlap(old, new)


# --------------------------------------------------------------------------
# 병합 — 이미 있는 값을 잃지 않는다
# --------------------------------------------------------------------------
def test_merge_never_blanks_an_existing_value():
    """커뮤니티 티어는 최신 하루의 실현시총이 빈다. 어제 받아 둔 값이
    오늘 사라지면 커버리지가 날마다 들쭉날쭉해진다."""
    d = D0 + timedelta(days=5)
    old = cells(10)
    old[d]["realized_cap"] = 1.0e12
    new = {d: {"price": old[d]["price"]}}          # realized_cap 없음
    m = rd.merge(old, new)
    assert m.cells[d]["realized_cap"] == 1.0e12


def test_merge_fills_a_hole_and_says_so():
    d = D0 + timedelta(days=5)
    old = cells(10)
    new = {d: {"price": old[d]["price"], "market_cap": 9.0e11}}
    m = rd.merge(old, new)
    assert m.cells[d]["market_cap"] == 9.0e11
    assert m.filled.get("market_cap") == 1
    assert not m.new_dates


def test_merge_counts_new_dates_separately_from_holes():
    """새 날짜의 빈 칸을 '채웠다'고 세면 매일 열 칸을 채운 것처럼 보인다."""
    old = cells(10)
    nd = D0 + timedelta(days=10)
    m = rd.merge(old, {nd: {"price": 61_000.0, "market_cap": 1.0}})
    assert m.new_dates == [nd]
    assert not m.filled


def test_merge_reports_a_changed_value():
    d = D0 + timedelta(days=3)
    old = cells(10)
    m = rd.merge(old, {d: {"price": old[d]["price"] + 1}})
    assert m.changed.get("price") == 1


def test_untouched_merge_is_not_written():
    old = cells(10)
    m = rd.merge(old, {d: dict(c) for d, c in old.items()})
    assert not m.touched
    assert m.summary() == "바뀐 것이 없습니다"


def test_merged_result_must_not_lose_dates():
    old = cells(10)
    m = rd.merge(old, {})
    del m.cells[D0 + timedelta(days=4)]
    with pytest.raises(rd.RefuseToWrite) as e:
        rd.check_merged(old, m)
    assert "사라졌습니다" in str(e.value)


def test_merged_result_must_not_lose_cells():
    old = cells(10)
    old[D0]["hashrate"] = 5.0
    m = rd.merge(old, {})
    m.cells[D0].pop("hashrate")
    with pytest.raises(rd.RefuseToWrite):
        rd.check_merged(old, m)


# --------------------------------------------------------------------------
# 쓰기
# --------------------------------------------------------------------------
def test_install_round_trips_and_keeps_a_backup(tmp_path):
    p = tmp_path / "market.csv"
    write_cells(cells(30), p)
    before = p.read_bytes()

    c = cells(31)
    bak = rd.install(c, p)
    assert bak is not None and bak.read_bytes() == before
    assert not (tmp_path / "market.csv.tmp").exists()
    assert len(read_cells(p)) == 31


def test_install_leaves_the_original_alone_when_the_new_file_is_bad(tmp_path):
    p = tmp_path / "market.csv"
    write_cells(cells(30), p)
    before = p.read_bytes()
    with pytest.raises(Exception):
        rd.install({}, p)                 # 가격이 없는 파일
    assert p.read_bytes() == before
    assert not (tmp_path / "market.csv.tmp").exists()


def test_cells_round_trip_through_csv(tmp_path):
    p = tmp_path / "m.csv"
    src = cells(5)
    src[D0]["hashrate"] = 1.25e20
    src[D0 + timedelta(days=1)]["exchange_inflow"] = 0.0     # 0 은 결측이 아니다
    write_cells(src, p)
    back = read_cells(p)
    assert back == src


def test_read_cells_on_a_missing_file_is_empty(tmp_path):
    assert read_cells(tmp_path / "nope.csv") == {}


def test_an_unknown_column_is_reported_not_swallowed(tmp_path):
    """read_cells → write_cells 왕복은 모르는 열을 지운다. 지우기 전에 말해야 한다."""
    p = tmp_path / "m.csv"
    p.write_text("date,price,my_note\n2026-01-01,100.0,7.5\n", encoding="utf-8")
    seen: dict = {}
    read_cells(p, out=seen)
    assert seen["unknown"] == ["my_note"]
    assert seen["unparsed"] == []


def test_a_cell_that_is_not_a_number_is_reported(tmp_path):
    """쉼표 하나가 든 칸을 조용히 결측으로 넘기면 갱신이 그 칸을 비워 버린다."""
    p = tmp_path / "m.csv"
    p.write_text('date,price,hashrate\n2026-01-01,"60,000.0",1e20\n', encoding="utf-8")
    seen: dict = {}
    cells_read = read_cells(p, out=seen)
    assert "price" not in cells_read[date(2026, 1, 1)]
    assert len(seen["unparsed"]) == 1 and "60,000.0" in seen["unparsed"][0]


def test_refresh_refuses_a_csv_with_an_unparseable_cell(tmp_path, capsys):
    p = tmp_path / "m.csv"
    p.write_text('date,price\n2026-01-01,"60,000.0"\n', encoding="utf-8")
    before = p.read_bytes()

    class Args:
        csv = str(p)
        check = False
        max_age = 3
        days = 400
        full = False
        dry_run = False
        build = False
        out_dir = "viz/site"
        timeout = 5

    assert rd.run(Args()) == 2
    assert "숫자로 읽히지 않는 칸" in capsys.readouterr().out
    assert p.read_bytes() == before


def test_refresh_refuses_a_csv_with_an_unknown_column(tmp_path, capsys):
    p = tmp_path / "m.csv"
    p.write_text("date,price,my_note\n2026-01-01,100.0,7.5\n", encoding="utf-8")
    before = p.read_bytes()

    class Args:
        csv = str(p)
        check = False
        max_age = 3
        days = 400
        full = False
        dry_run = False
        build = False
        out_dir = "viz/site"
        timeout = 5

    assert rd.run(Args()) == 2
    assert "my_note" in capsys.readouterr().out
    assert p.read_bytes() == before          # 손대지 않았다


# --------------------------------------------------------------------------
# 신선도
# --------------------------------------------------------------------------
def test_staleness_counts_from_the_last_priced_day():
    c = cells(10)
    c[D0 + timedelta(days=10)] = {"market_cap": 1.0}      # 가격 없는 행
    last, age = rd.staleness(c, today=D0 + timedelta(days=12))
    assert last == D0 + timedelta(days=9)
    assert age == 3


def test_staleness_without_any_price_is_maximally_stale():
    last, age = rd.staleness({D0: {"supply": 1.0}}, today=D0)
    assert last is None and age > 10_000


# --------------------------------------------------------------------------
# 두 경로가 같은 파일을 만드는가
# --------------------------------------------------------------------------
def test_full_and_incremental_agree(tmp_path):
    """전 구간 새로 받기와 증분으로 이어 붙이기가 같은 결과여야 한다.

    이 성질이 성립하니까 CI 는 매번 전 구간을 받아도 되고, 되돌릴 때도
    "다시 받으면 된다"고 말할 수 있다.
    """
    full = cells(60)
    incr_path = tmp_path / "incr.csv"
    write_cells({d: c for d, c in full.items() if d < D0 + timedelta(days=50)},
                incr_path)

    old = read_cells(incr_path)
    window = {d: c for d, c in full.items() if d >= D0 + timedelta(days=20)}
    rd.check_fetched(window, today=D0 + timedelta(days=61))
    rd.check_overlap(old, window)
    m = rd.merge(old, window)
    rd.check_merged(old, m)
    rd.install(m.cells, incr_path)

    full_path = tmp_path / "full.csv"
    write_cells(full, full_path)
    assert incr_path.read_text(encoding="utf-8") == full_path.read_text(encoding="utf-8")
