"""관련성 지도 — '무엇과 관련 있는가'를 재는 도구가 조용히 틀리지 않게.

이 도구는 다른 검정 도구들과 답하는 질문이 다르다(docs/33). 그래서 여기서 지키는
것도 다르다 — 얼마나 잘 맞히는가가 아니라 **무엇을 재고 무엇을 안 재는가**다.

특히 하나는 실제로 깨졌던 것이라 못 박아 둔다: 화면이 두 언어인데 영어 필드를
``labelEn`` 으로 적어서 **영어 화면에 한국어가 그대로 남았다.** 화면의
``pick(o, "label")`` 은 ``o["label_en"]`` 을 찾는다.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

import relationship_map as rm      # conftest 가 tools/ 를 경로에 넣는다


def _series(n: int, fn, start=date(2013, 1, 1), step: int = 1) -> dict:
    return {start + timedelta(days=i * step): fn(i) for i in range(n)}


@pytest.fixture(scope="module")
def toy():
    """가격 하나와 거시 셋. 나스닥은 가격과 붙여 두고 원유는 무관하게 둔다."""
    n = 1400
    price = _series(n, lambda i: 1000 * math.exp(i / 900 + 0.25 * math.sin(i / 60)))
    market = {"price": price}
    macro = {
        # 나스닥: 가격과 같은 흔들림 → 상관이 커야 한다
        "nasdaq": _series(n, lambda i: 3000 * math.exp(i / 2500 + 0.18 * math.sin(i / 60))),
        # VIX: 반대 부호
        "vix": _series(n, lambda i: 20 - 6 * math.sin(i / 60)),
        # 원유: 다른 주기 → 관련 없어야 한다
        "oil": _series(n, lambda i: 60 + 12 * math.sin(i / 17.3)),
    }
    return market, macro


# --------------------------------------------------------------------------
# 두 언어
# --------------------------------------------------------------------------
def _walk(node):
    """페이로드 안의 모든 사전을 훑는다."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_english_fields_use_the_suffix_the_page_actually_reads(toy):
    """``labelEn`` 으로 적으면 영어 화면에 한국어가 남는다 — 실제로 그랬다.

    화면의 pick() 은 ``<field>_en`` 만 본다. camelCase 는 조용히 무시되고 한국어로
    폴백하기 때문에, 브라우저를 띄우기 전에는 아무도 모른다."""
    pl = rm.payload(*toy)
    bad = [k for d in _walk(pl) for k in d
           if len(k) > 2 and k.endswith("En") and k[-3].islower()]
    assert not bad, f"pick() 이 못 읽는 camelCase 영어 필드: {sorted(set(bad))}"


def test_every_translatable_field_has_both_languages(toy):
    """한쪽만 있으면 반쯤 번역된 화면이 된다 — 번역하지 않은 화면보다 나쁘다."""
    gaps = []
    for d in _walk(rm.payload(*toy)):
        for k, v in d.items():
            if k.endswith("_en") and not str(v).strip():
                gaps.append(k)
            if f"{k}_en" not in d and k in ("label", "kind", "band"):
                gaps.append(f"{k}(영어 없음)")
    assert not gaps, f"영어가 빠진 필드: {sorted(set(gaps))}"


def test_the_label_tables_cover_every_value_they_translate():
    """구분·등급을 하나 늘리고 사전을 잊으면 KeyError 로 빌드가 죽는다.
    죽는 것 자체는 옳지만, 왜 죽는지는 여기서 먼저 말해 준다."""
    kinds = {k for *_, k, _ in rm.MACRO_SERIES} | {k for *_, k in rm.MARKET_SERIES} | {"파생"}
    assert not (kinds - set(rm.KIND_EN)), f"영어 이름이 없는 구분: {kinds - set(rm.KIND_EN)}"
    words = {rm.band(v) for v in (0.9, 0.3, 0.2, 0.01, None)}
    assert not (words - set(rm.BAND_EN)), f"영어 이름이 없는 등급: {words - set(rm.BAND_EN)}"


# --------------------------------------------------------------------------
# 무엇을 재는가
# --------------------------------------------------------------------------
def test_the_era_table_is_chosen_by_a_flag_not_by_list_order():
    """예전엔 ``MACRO_SERIES[:8]`` 이었다. 목록 순서를 바꾸면 국면별 표의 내용이
    말없이 바뀐다 — 표시로 골라야 그런 일이 없다."""
    flagged = [c for c, _ko, _en, _k, in_era in rm.MACRO_SERIES if in_era]
    assert "nasdaq" in flagged and "vix" in flagged, "핵심 두 계열이 빠졌습니다"
    # 화면이 이 둘을 키로 골라 강조한다. 이름이 바뀌면 강조가 조용히 사라진다.
    assert flagged[0] == "nasdaq"


def test_ranked_is_sorted_by_absolute_correlation(toy):
    pl = rm.payload(*toy)
    mags = [abs(r["rho"]) for r in pl["ranked"]]
    assert mags == sorted(mags, reverse=True), "|rho| 내림차순이 아닙니다"


def test_price_derived_rows_are_marked_so_they_cannot_be_read_as_a_finding(toy):
    """MVRV·드로다운은 분자가 가격이라 상관이 높은 것이 **산술**이다.
    표시를 빠뜨리면 화면에서 '가장 관련 있는 것'으로 읽힌다."""
    market, macro = toy
    market = dict(market)
    market["market_cap"] = {d: v * 19e6 for d, v in market["price"].items()}
    market["realized_cap"] = {d: v * 19e6 * 0.6 for d, v in market["price"].items()}
    pl = rm.payload(market, macro)
    kinds = {r["key"]: r["kind"] for r in pl["ranked"]}
    assert kinds.get("drawdown") == "파생", "드로다운이 '파생'으로 표시되지 않았습니다"
    assert kinds.get("mvrv") == "파생", "MVRV 가 '파생'으로 표시되지 않았습니다"


def test_a_related_series_scores_higher_than_an_unrelated_one(toy):
    """도구가 실제로 관련성을 재는지 — 붙여 놓은 계열이 무관한 계열보다 커야 한다."""
    by = {r["key"]: abs(r["rho"]) for r in rm.payload(*toy)["ranked"]}
    assert by["nasdaq"] > by["oil"], f"나스닥 {by['nasdaq']:.3f} vs 원유 {by['oil']:.3f}"


def test_vix_comes_out_with_the_opposite_sign(toy):
    """부호가 뒤집히면 '주가와 같이, 공포와 반대로'라는 서술 전체가 무너진다."""
    by = {r["key"]: r["rho"] for r in rm.payload(*toy)["ranked"]}
    assert by["nasdaq"] > 0 > by["vix"], by


def test_levels_are_never_correlated_directly(toy):
    """이 저장소의 제1원칙(docs/25 2절) — 수준끼리 재면 '같이 올랐다'만 나온다.

    둘 다 **우상향**이되 흔들림의 주기는 서로 다르게 만든다. 수준끼리 재면 추세
    때문에 +1 에 붙고, 변화율로 재면 흔들림이 안 맞아 0 근처가 되어야 한다."""
    n = 900
    market = {"price": _series(n, lambda i: 100 * math.exp(i / 400 + 0.06 * math.sin(i / 7.0)))}
    macro = {"oil": _series(n, lambda i: 50 * math.exp(i / 700 + 0.06 * math.sin(i / 11.3)))}
    # 먼저 이 표본이 실제로 '수준끼리는 붙어 있는' 표본인지 확인한다 —
    # 그렇지 않으면 아래 단언이 통과해도 아무것도 증명하지 못한다.
    lv = mc_pearson_levels(market["price"], macro["oil"])
    assert lv > 0.95, f"표본이 잘못됐습니다 — 수준 상관이 {lv:+.3f}"
    rho = {r["key"]: r["rho"] for r in rm.payload(market, macro)["ranked"]}["oil"]
    assert abs(rho) < 0.3, f"수준 상관을 재고 있습니다 (rho={rho:+.3f})"


def mc_pearson_levels(a: dict, b: dict) -> float:
    import macro_correlation as mc
    ks = sorted(set(a) & set(b))
    return mc.pearson([a[k] for k in ks], [b[k] for k in ks])


# --------------------------------------------------------------------------
# 퇴행 입력
# --------------------------------------------------------------------------
def test_empty_inputs_do_not_raise():
    """표본 CSV 로 사이트를 빌드할 때 실제로 밟는 경로다. 빈 지도를 내야 한다.

    모양 전체를 못 박지는 않는다 — 그러면 칸을 하나 더할 때마다 여기가 깨지고,
    깨진 이유가 '기능이 늘었다'뿐이면 그 시험은 아무것도 안 지킨 것이다."""
    pl = rm.payload({}, {})
    assert pl["ranked"] == [] and pl["eras"] == []
    assert pl["eraLabels"], "국면 라벨은 데이터 없이도 나와야 한다"


def test_a_series_with_no_observations_is_skipped_not_crashed():
    """빈 계열은 min() 에서 터진다. 건너뛰어야 한다."""
    market = {"price": _series(400, lambda i: 100 + i)}
    pl = rm.payload(market, {"oil": {}, "vix": {}})
    assert all(r["key"] != "oil" for r in pl["ranked"])


def test_too_short_a_history_yields_no_era_values():
    """국면마다 표본이 모자라면 값을 만들어 내지 말고 비워야 한다 — 없는 관측을
    있는 것처럼 그리면 화면의 선이 거짓말을 한다."""
    market = {"price": _series(40, lambda i: 100 + i)}
    pl = rm.payload(market, {"nasdaq": _series(40, lambda i: 200 + i * 0.5)})
    for row in pl["eras"]:
        assert all(v is None for v in row["v"]), row


def test_band_words_follow_the_documented_cuts():
    """|rho| 등급은 문서·리포트·화면이 같은 표를 써야 한다."""
    assert rm.band(0.41) == "뚜렷함" and rm.band(0.40) == "뚜렷함"
    assert rm.band(0.39) == "있음" and rm.band(0.25) == "있음"
    assert rm.band(0.24) == "약함" and rm.band(0.15) == "약함"
    assert rm.band(0.14) == "거의 없음" and rm.band(-0.9) == "뚜렷함"
    assert rm.band(None) == "—"


def test_render_survives_an_empty_map():
    """데이터가 없을 때도 리포트는 떠야 한다 — 빈 표라도 형식은 남는다."""
    text = rm.render(rm.payload({}, {}))
    assert "관련성 지도" in text and "국면별" in text


def test_a_short_sample_is_flagged_so_it_is_not_read_like_the_others():
    """이 저장소에서 **가장 큰 외부 상관이 하필 가장 짧은 표본** 위에 있다
    (신용 스프레드 — 원자료가 2023-08 부터만 온다). 그 사실이 표에 안 보이면
    순위표가 사람을 오도한다. 표시는 데이터에서 나와야 하고, 특정 계열 이름을
    코드에 박아서는 안 된다."""
    full, n = 2400, 200                        # 6.6년 vs 0.5년 — 문턱은 5년
    price = _series(full, lambda i: 1000 * math.exp(i / 900 + 0.25 * math.sin(i / 60)))
    short = _series(n, lambda i: 50 + 10 * math.sin(i / 9.0),
                    start=date(2013, 1, 1) + timedelta(days=full - n))
    long_ = _series(full, lambda i: 60 + 12 * math.sin(i / 17.3))
    by = lambda pl: {r["key"]: r for r in pl["ranked"]}
    hit = by(rm.payload({"price": price}, {"oil": short}))["oil"]
    assert hit["short"] is True and hit["years"] < rm.SHORT_YEARS, hit
    miss = by(rm.payload({"price": price}, {"oil": long_}))["oil"]
    assert miss["short"] is False, miss


def test_the_span_is_reported_in_years_not_just_row_count():
    """주간 156개가 3년인지 3주인지 n 만으로는 알 수 없다."""
    price = _series(1400, lambda i: 1000 * math.exp(i / 900 + 0.2 * math.sin(i / 60)))
    pl = rm.payload({"price": price}, {"oil": _series(1400, lambda i: 60 + 12 * math.sin(i / 17.3))})
    yrs = next(r["years"] for r in pl["ranked"] if r["key"] == "oil")
    assert 3.7 < yrs < 3.9, f"1400일 ≈ 3.8년이어야 하는데 {yrs}"


def test_the_short_flag_threshold_is_a_named_constant_not_a_literal():
    """문턱을 화면과 리포트가 각각 적어 두면 둘이 갈라진다. 한 곳에서만 정한다."""
    assert rm.payload({}, {})["shortYears"] == rm.SHORT_YEARS
