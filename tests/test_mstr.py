"""MSTR 파이프라인·상대성과 분석 검증 — 데이터가 없어도 코드는 맞아야 한다.

이 환경에서는 stooq·야후가 403 이라 실제 MSTR 를 못 받는다. 그래도 파서·병합·
무결성 검사·분석 로직은 합성 데이터로 검증할 수 있다 — 답을 아는 데이터를
심어 두고(아웃퍼포먼스 구간·경계·상관), 도구가 그 답을 되찾는지 본다.

주의 깊게 심은 것: MSTR 는 주 5일, BTC 는 주 7일이다. 달력일로 나이브하게
짝지으면 주말마다 어긋난다 — 평일 격자 테스트가 그 함정을 못 박는다.
"""

from __future__ import annotations

import csv
import json
import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fetch_stock as fs   # noqa: E402
import mstr_study as ms    # noqa: E402


def days_between(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# ① 심어 둔 아웃퍼포먼스 구간 회수 — 평일(주 5일) 격자에서
# ---------------------------------------------------------------------------
def test_outperformance_recovers_planted_period_on_weekday_grid():
    """MSTR 평일만·BTC 매일 — 공통 거래일로 맞추면 심은 구간이 정확히 나온다.

    2021-06-01(화) 에 MSTR 만 e^0.8 로 계단 상승. 스프레드 0.8 은 창의 가장
    오래된 관측이 점프 전(≤ 2021-05-31 월)인 동안만 유지된다. d−90 이 5/31 을
    창에 담는 마지막 평일은 2021-08-27(금)이다 — 경계까지 못 박는다."""
    jump = date(2021, 6, 1)
    all_days = days_between(date(2021, 1, 1), date(2021, 12, 31))
    btc = {d: 30000.0 for d in all_days}
    mstr = {d: (100.0 if d < jump else 100.0 * math.exp(0.8))
            for d in all_days if d.weekday() < 5}

    op = ms.outperformance(mstr, btc)
    assert len(op["up"]) == 1, op["up"]
    s, e, m = op["up"][0]
    assert s == jump
    assert e == date(2021, 8, 27)
    assert abs(m - 0.8) < 1e-9
    assert op["down"] == []

    # 반대 방향 — 역할을 바꾸면 같은 경계의 down 구간이 나와야 한다
    op2 = ms.outperformance(btc, mstr)
    assert [(s2, e2) for s2, e2, _ in op2["down"]] == [(s, e)]
    assert abs(op2["down"][0][2] + 0.8) < 1e-9
    assert op2["up"] == []


# ---------------------------------------------------------------------------
# ② 구간 경계 정확성 — 7일 격자에서 정확히 [점프일, 점프일+89]
# ---------------------------------------------------------------------------
def test_outperformance_boundaries_exact_on_daily_grid():
    """둘 다 매일 관측이면 경계가 산술로 닫힌다: 점프일 D 에 시작해,
    창(90일)의 시작이 D 를 지나치기 직전인 D+89 에 끝난다."""
    jump = date(2021, 5, 1)
    all_days = days_between(date(2021, 1, 1), date(2021, 10, 31))
    btc = {d: 30000.0 for d in all_days}
    mstr = {d: (100.0 if d < jump else 100.0 * math.exp(0.6)) for d in all_days}

    op = ms.outperformance(mstr, btc)
    assert len(op["up"]) == 1
    s, e, m = op["up"][0]
    assert s == jump
    assert e == jump + timedelta(days=89)
    assert abs(m - 0.6) < 1e-9
    # 계열 시작부(창 80일 미만)는 스프레드 자체가 없다 — 반쪽 창 금지
    assert min(op["spread"]) >= date(2021, 1, 1) + timedelta(days=ms.MIN_SPAN_DAYS)


# ---------------------------------------------------------------------------
# ③ 데이터 없음 → None / CLI 는 말하고 0 으로 끝난다
# ---------------------------------------------------------------------------
def test_site_payload_returns_none_without_enough_data():
    base = date(2021, 1, 1)
    assert ms.site_payload({}, {}, base) is None
    # 공통 구간 45일 < 90일 — 롤링 창 하나도 못 채우면 payload 를 내지 않는다
    days = days_between(date(2021, 1, 1), date(2021, 2, 14))
    assert ms.site_payload({d: 100.0 for d in days},
                           {d: 200.0 for d in days}, base) is None


def test_load_mstr_returns_none_when_file_absent(tmp_path):
    assert ms.load_mstr(str(tmp_path / "absent.csv")) is None


def test_cli_says_no_data_and_exits_zero(tmp_path, capsys):
    rc = ms.main(["--stocks", str(tmp_path / "absent.csv")])
    assert rc == 0
    assert "없습니다" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ④ 분할 미조정 탐지 — 하루 −90% 는 경고, 0·비유한 값은 제거
# ---------------------------------------------------------------------------
def test_check_integrity_flags_unadjusted_split_and_drops_bad_values():
    series = {date(2024, 8, 5): 1600.0, date(2024, 8, 6): 1620.0,
              date(2024, 8, 7): 135.0,          # 미조정 10:1 분할이면 하루 −92%
              date(2024, 8, 8): 140.0,
              date(2024, 8, 9): 0.0}            # 양수 아님 → 제거 + 경고
    clean, warns = fs.check_integrity(series, "mstr")
    assert date(2024, 8, 9) not in clean
    assert any("분할" in w for w in warns), warns
    assert any("제거" in w for w in warns), warns

    # 정상(조정가) 시계열은 경고가 없어야 한다 — 매일 +1% 는 문턱을 못 넘는다
    ok = {date(2024, 8, 5) + timedelta(days=i): 100.0 * (1.01 ** i)
          for i in range(30)}
    clean2, warns2 = fs.check_integrity(ok, "mstr")
    assert warns2 == [] and len(clean2) == 30


# ---------------------------------------------------------------------------
# ⑤ site_payload 오프셋은 base 기준 — 주간 격자·시작 1.0·소수 3자리
# ---------------------------------------------------------------------------
def test_site_payload_offsets_are_relative_to_base():
    jump = date(2021, 5, 1)
    all_days = days_between(date(2021, 1, 1), date(2021, 10, 31))
    btc = {d: 30000.0 for d in all_days}
    mstr = {d: (100.0 if d < jump else 100.0 * math.exp(0.6)) for d in all_days}
    base = date(2020, 12, 1)                    # 첫 공통 거래일보다 앞선 기준일

    pl = ms.site_payload(mstr, btc, base)
    assert pl is not None
    # 첫 점 = 첫 공통 거래일(2021-01-01, base+31일), 지수 시작 = 1.0
    assert pl["series"][0] == [31, 1.0]
    offs = [o for o, _ in pl["series"]]
    assert offs == sorted(offs)
    assert len(offs) < len(all_days) // 4       # 주간 격자로 추렸다
    # 구간 오프셋도 base 기준 — ② 에서 못 박은 경계를 일수로 환산한 값
    assert pl["periods"] == [[(jump - base).days,
                              (jump + timedelta(days=89) - base).days]]
    assert pl["periodsDown"] == []
    assert pl["n"] == len(all_days)
    # 상대성과지수: 점프 뒤 = e^0.6 을 소수 3자리로 자른 값이 실려 있다
    assert round(math.exp(0.6), 3) in [v for _, v in pl["series"]]


# ---------------------------------------------------------------------------
# 상관 분석 — 심은 동행(채택 후)과 무관(채택 전)을 국면별로 가른다
# ---------------------------------------------------------------------------
def test_analyse_recovers_comovement_and_splits_eras():
    """채택(2020-08-11) 후 MSTR 일수익률 = BTC×1.5 + 잡음, 채택 전엔 독립으로
    심는다. 국면표가 그 단절을 보여야 한다 — 전체를 하나로 뭉개면 안 보인다."""
    rng = random.Random(21)
    all_days = days_between(date(2019, 1, 1), date(2022, 12, 31))
    btc, lb = {}, 20000.0
    for d in all_days:
        lb *= math.exp(rng.gauss(0, 0.03))
        btc[d] = lb
    mstr, lm, prev = {}, 120.0, None
    for d in all_days:
        if d.weekday() >= 5:                    # MSTR 는 평일만
            continue
        if prev is not None:
            span_ret = math.log(btc[d] / btc[prev])
            if d >= ms.ADOPTION:
                lm *= math.exp(1.5 * span_ret + rng.gauss(0, 0.01))
            else:
                lm *= math.exp(rng.gauss(0, 0.03))
        mstr[d] = lm
        prev = d

    a = ms.analyse(mstr, btc)
    assert a["full_wk"] is not None and a["full_wk"] > 0.4, a["full_wk"]
    pre_rho, pre_n = a["eras"]["채택 전(~2020-08)"]
    assert pre_n >= 60 and abs(pre_rho) < 0.35, (pre_rho, pre_n)
    for label in ("2021", "2022"):
        v, n = a["eras"][label]
        assert v is not None and v > 0.5, (label, v, n)


# ---------------------------------------------------------------------------
# fetch_stock — 파서(픽스처)·병합·완전 실패 시 원본 불변
# ---------------------------------------------------------------------------
def test_parsers_read_stooq_csv_and_yahoo_json():
    text = ("Date,Open,High,Low,Close,Volume\n"
            "2024-08-06,132,140,130,138.5,1000\n"
            "bad,row\n")
    assert fs.parse_stooq(text) == {date(2024, 8, 6): 138.5}
    # 요청 한도 초과 등 CSV 아닌 본문 → None (호출부가 야후 폴백으로 간다)
    assert fs.parse_stooq("Exceeded the daily hits limit") is None

    t1 = int(datetime(2024, 8, 6, 14, 30, tzinfo=timezone.utc).timestamp())
    t2 = int(datetime(2024, 8, 7, 14, 30, tzinfo=timezone.utc).timestamp())
    payload = {"chart": {"result": [{
        "timestamp": [t1, t2],
        "indicators": {"quote": [{"close": [135.0, None]}],
                       "adjclose": [{"adjclose": [135.2, None]}]}}]}}
    # adjclose 우선, 결측(null)은 건너뛴다
    assert fs.parse_yahoo(json.dumps(payload)) == {date(2024, 8, 6): 135.2}
    assert fs.parse_yahoo("not json") is None


def test_fetch_stock_merges_and_leaves_file_intact_on_total_failure(
        tmp_path, monkeypatch):
    p = tmp_path / "stocks.csv"
    fs.merge_to_csv({"mstr": {date(2024, 1, 2): 500.0}}, p)
    # 둘째 run: 새 날짜 추가 + 같은 날짜는 새 값이 이긴다 (열 단위 병합)
    fs.merge_to_csv({"mstr": {date(2024, 1, 3): 510.0,
                              date(2024, 1, 2): 501.0}}, p)
    with p.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["date"] for r in rows] == ["2024-01-02", "2024-01-03"]
    assert float(rows[0]["mstr"]) == 501.0
    before = p.read_bytes()

    # 정책 403(둘 다) → 구분해 보고하고 0, 기존 파일 불변
    def blocked(*a, **k):
        raise fs.PolicyBlocked("stooq.com")
    monkeypatch.setattr(fs, "fetch_stooq", blocked)
    monkeypatch.setattr(fs, "fetch_yahoo", blocked)
    assert fs.main(["--out", str(p)]) == 0
    assert p.read_bytes() == before

    # 정책 아닌 완전 실패 → 1, 역시 기존 파일 불변·임시파일 잔재 없음
    monkeypatch.setattr(fs, "fetch_stooq", lambda *a, **k: None)
    monkeypatch.setattr(fs, "fetch_yahoo", lambda *a, **k: None)
    assert fs.main(["--out", str(p)]) == 1
    assert p.read_bytes() == before
    assert not p.with_name(p.name + ".tmp").exists()


def test_cli_report_prints_planted_period_from_files(tmp_path, capsys):
    """CSV 파일 → 리포트까지 한 줄로 이어지는지 — ② 의 구간이 본문에 실린다."""
    jump = date(2021, 5, 1)
    all_days = days_between(date(2021, 1, 1), date(2021, 10, 31))
    sp = tmp_path / "stocks.csv"
    fs.merge_to_csv({"mstr": {d: (100.0 if d < jump else 100.0 * math.exp(0.6))
                              for d in all_days}}, sp)
    mp = tmp_path / "market.csv"
    with mp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "price"])
        for d in all_days:
            w.writerow([d.isoformat(), "30000.0"])

    rc = ms.main(["--stocks", str(sp), "--csv", str(mp),
                  "--macro", str(tmp_path / "no-macro.csv")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2021-05-01" in out and "MSTR 이 크게 앞선 구간" in out


def test_site_payload_survives_real_scale_history_from_2010():
    """스투크는 MSTR 이력을 2000년대부터 준다. 첫 공통 거래일(2010년)을 기준으로
    잡으면 비율이 2e-5 규모가 되고, 소수 3자리 반올림이 전부 0.000 으로 뭉개
    **차트 선이 조용히 끊긴다** — 적대적 검증이 실데이터 규모로 재현한 HIGH.
    채택일(2020-08-11) 절단 + 유효숫자 반올림이 막는다."""
    from datetime import date, timedelta
    import math
    base = date(2010, 7, 19)
    n = (date(2026, 7, 30) - base).days
    # BTC 8십만 배, MSTR 40배 — 실데이터의 자릿수를 흉내 낸다
    btc = {base + timedelta(days=i): 0.08 * math.exp(i / 425) for i in range(n)}
    mstr = {d: 8.5 * math.exp(i / 1600)
            for i, d in enumerate(base + timedelta(days=i) for i in range(n))
            if d.weekday() < 5}
    pl = ms.site_payload(mstr, btc, base)
    assert pl is not None
    # 채택일 이전은 실리지 않는다
    adoption_off = (ms.ADOPTION - base).days
    assert all(off >= adoption_off for off, _ in pl["series"]), "채택 전 점이 실렸다"
    # 어떤 점도 0 으로 뭉개지지 않는다 — 뭉개지면 차트가 그 구간을 버린다
    assert all(v > 0 for _, v in pl["series"]), "반올림이 값을 0 으로 뭉갰다"
    # 시작점은 1.0
    assert pl["series"][0][1] == 1.0


def test_parse_yahoo_null_timestamp_and_all_null_adjclose():
    """적대적 검증 발견 2건: ① timestamp 에 null 이 섞이면 int(None) 으로 죽었다
    ② adjclose 키는 있는데 전부 null 이면 close 가 멀쩡해도 빈손을 돌려줬다."""
    import json as _json
    import fetch_stock as fs
    body = _json.dumps({"chart": {"result": [{
        "timestamp": [1720000000, None, 1720172800],
        "indicators": {"quote": [{"close": [10.0, 11.0, 12.0]}],
                       "adjclose": [{"adjclose": [None, None, None]}]},
    }]}})
    out = fs.parse_yahoo(body)          # 죽지 않아야 한다
    assert out is not None and len(out) == 2, out
    assert sorted(out.values()) == [10.0, 12.0], "close 폴백이 안 됐다"
