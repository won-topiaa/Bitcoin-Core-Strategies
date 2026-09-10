"""시각화 페이지 — 조각을 합쳐 굽는 과정이 조용히 깨지지 않게.

이 파일이 생긴 이유가 있다. 차트 엔진 한 벌이 페이지 세 장을 모두 그리는데,
그 과정에서 두 종류의 사고가 실제로 났다.

1. **자바스크립트 구문 오류.** 여러 줄 템플릿 문자열 안에서 괄호 하나가 안
   닫혔는데, 파이썬 쪽 테스트는 전부 통과했고 페이지는 **아무것도 안 그린 채**
   조용히 떴다.
2. **없는 요소 접근.** 한 장에만 있는 요소를 세 장이 공유하는 스크립트가
   무조건 건드려서, 나머지 두 장이 그리다 말고 멈췄다.

둘 다 브라우저를 띄워야 보이는 종류라 파이썬 테스트로는 안 잡힌다. 그래서
브라우저 없이 잡을 수 있는 만큼 여기서 잡는다 — node 파서로 구문을, 정적
검사로 가드 누락을, 빌드 결과로 자리표시자와 외부 의존을 본다.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import build_viz          # conftest 가 tools/ 를 경로에 넣는다

ROOT = Path(__file__).resolve().parents[1]
VIZ = ROOT / "viz"
# 본문 목록은 **build_viz.PAGES 가 소유한다.** 예전엔 이 파일에만 네 벌이 박혀
# 있어서, 페이지를 하나 더 붙이면 새 장이 어느 시험에도 안 걸렸다.
BODIES = tuple(Path(b).name for b, *_ in build_viz.PAGES)
PIECES = ("_head.html", "_script.html", "_rules_script.html", "_i18n.html",
          "_nav.html") + BODIES


def test_all_pieces_exist():
    """조각 하나가 사라지면 빌드가 죽는다. 어느 것인지 먼저 알려준다."""
    missing = [n for n in PIECES if not (VIZ / n).exists()]
    assert not missing, f"없는 조각: {missing}"


def read(name: str) -> str:
    p = VIZ / name
    if not p.exists():
        pytest.skip(f"{name} 없음")
    return p.read_text(encoding="utf-8")


def js_of(name: str) -> str:
    """<script> 껍데기를 벗긴 알맹이."""
    return re.sub(r"^<script>|</script>\s*$", "", read(name).strip())


def node_check(js: str) -> subprocess.CompletedProcess | None:
    """node 로 구문 검사. node 가 없으면 None."""
    try:
        return subprocess.run(["node", "--check", "-"], input=js, text=True,
                              capture_output=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


@pytest.mark.parametrize("name", ["_script.html", "_rules_script.html", "_i18n.html"])
def test_script_parses(name):
    """진짜 파서로 구문을 검사한다.

    처음에는 괄호 개수를 손으로 세는 검사를 짰다가 버렸다. 정규식 리터럴
    ``/[&<>"]/g`` 안의 따옴표를 문자열 시작으로 읽어서 **멀쩡한 코드를
    깨졌다고 신고**했다. 자바스크립트를 파싱하려면 자바스크립트 파서를 써야
    한다 — 반쯤 맞는 검사는 없는 검사보다 나쁘다.
    """
    r = node_check(js_of(name))
    if r is None:
        pytest.skip("node 없음 — 브라우저 없이는 구문을 확인할 수 없다")
    assert r.returncode == 0, r.stderr[:800]


@pytest.mark.parametrize("name", ["_script.html", "_rules_script.html", "_i18n.html"])
def test_no_unguarded_element_access(name):
    """`getElementById(...).무엇` 을 직접 쓰지 않는다.

    페이지마다 있는 요소가 다르므로 반드시 setText/setHTML 이나 null 검사를
    거쳐야 한다. 이 한 줄을 어기면 나머지 두 장이 그리다 만다.
    """
    if name != "_script.html":
        pytest.skip("세 장을 공유하는 것은 _script.html 뿐이다")
    js = read(name)
    bad = re.findall(r'getElementById\("[^"]+"\)\s*\.\s*(?!textContent\b)\w+', js)
    assert not bad, f"가드 없는 접근: {bad[:5]}"


def test_halving_since_days_are_not_hardcoded():
    """"과거 고점은 …일에 나왔습니다" 의 숫자는 전환점에서 계산해야 한다.

    예전엔 367·526·548·534 가 문자열에 박혀 있었고, docs/21 에서 전환점을
    실측으로 옮기자 실제(371·525·546·534)와 어긋났다. 화면이 지표와 다른 말을
    하고 있었던 것이다. 다시 박지 못하게 막는다.
    """
    # 주석은 뺀다 — 왜 이렇게 고쳤는지 설명하는 줄에 옛 숫자가 남아 있다.
    code = "\n".join(ln for ln in read("_script.html").splitlines()
                     if not ln.lstrip().startswith("//"))
    seq = re.compile(r"367[·, ].*?534|534[ ]?days after")
    assert not seq.search(code), \
        "반감기 경과일이 문자열에 박혀 있습니다 — D.tps 에서 계산하세요"


def test_svg_listeners_are_wired_exactly_once():
    """svg 요소는 **다시 그려도 살아남는다** (지워지는 것은 그 자식들이다).

    그래서 그릴 때마다 리스너를 붙이면 쌓이고, 화살표를 한 번 눌렀는데 두 칸,
    세 칸씩 움직인다. 창 크기를 바꾸거나 테마를 바꾼 뒤에야 드러나는 종류라
    여기서 못 박는다 — svg 에 리스너를 붙이는 코드는 전부 '한 번만' 가드 안에
    있어야 한다.
    """
    js = read("_script.html")
    guard = re.search(r"if \(!svg\.dataset\.kbWired\)\{(.*?)\n  \}", js, re.S)
    assert guard, "한 번만 붙이는 가드를 찾지 못했습니다"
    total = js.count("svg.addEventListener(")
    inside = guard.group(1).count("svg.addEventListener(")
    assert total and total == inside, (
        f"가드 밖에서 svg 에 리스너를 붙입니다 ({total - inside}곳)")


def test_the_nav_is_not_duplicated_in_the_bodies():
    """머리띠는 조각 하나가 소유한다. 세 본문에 복제되면 버튼을 하나 붙일 때
    세 곳을 똑같이 고쳐야 하고, 실제로 한 곳을 빠뜨린다."""
    for name in BODIES:
        body = read(name)
        assert "__NAV__" in body, f"{name}: 머리띠 자리표시자가 없습니다"
        assert 'class="pages"' not in body, f"{name}: 머리띠가 복제돼 있습니다"


def test_every_page_marks_which_page_it_is():
    """머리띠가 공용이라 '지금 어느 장인지'는 속성으로 와야 한다."""
    nav = read("_nav.html")
    assert "__PAGE__" in nav
    keys = set(re.findall(r'data-page="(\w+)"', nav))
    sys.path.insert(0, str(ROOT / "tools"))
    import build_viz

    assert {k for _, _, k, _, _ in build_viz.PAGES} == keys


def test_every_placeholder_gets_replaced():
    """빌드 결과에 __XXX__ 자리표시자가 남아 있으면 링크가 깨진 것이다."""
    import tempfile

    sys.path.insert(0, str(ROOT / "tools"))
    csv = ROOT / "data" / "market.csv"
    if not csv.exists():
        csv = ROOT / "data" / "sample_synthetic.csv"
    if not csv.exists():
        pytest.skip("입력 CSV 없음")

    import build_viz

    with tempfile.TemporaryDirectory() as tmp:
        paths = build_viz.build(str(csv), Path(tmp))
        assert len(paths) == len(build_viz.PAGES)
        for p in paths:
            html = p.read_text(encoding="utf-8")
            # 등록된 자리표시자가 남았는가 — 빌드가 이미 막지만 여기서 한 번 더.
            left = [w for w in build_viz.PLACEHOLDERS if w in html]
            assert not left, f"{p.name}: 남은 자리표시자 {left}"
            # 등록하지 않은 __XXX__ 를 본문에 새로 쓴 경우. 자바스크립트 전역은
            # __BCS 로 시작하는 것만 쓴다는 약속이라 그것만 빼고 본다.
            stray = {w for w in re.findall(r"__[A-Z_]+__", html)
                     if not w.startswith("__BCS") and w != "__DATA__"}
            assert not stray, f"{p.name}: 등록되지 않은 자리표시자 {stray}"
            assert "<title>" in html and "__TITLE__" not in html


def test_every_page_is_stamped_with_the_source_it_was_baked_from():
    """감시자의 '고쳤는데 안 구웠다' 검사는 **이 표식 하나**에 통째로 의존한다.

    표식이 없으면 site_stale.source_changed 가 늘 '판정하지 않습니다'로 빠져,
    감시가 조용히 꺼진 채로 며칠이 지나간다(그 상태를 실제로 겪었다). 여기서
    막지 않으면 build_viz 에서 한 줄만 사라져도 아무 테스트도 안 깨진다.
    """
    import tempfile

    sys.path.insert(0, str(ROOT / "tools"))
    csv = ROOT / "data" / "market.csv"
    if not csv.exists():
        csv = ROOT / "data" / "sample_synthetic.csv"
    if not csv.exists():
        pytest.skip("입력 CSV 없음")

    import build_viz
    import site_stale

    expected = site_stale.source_sha()      # 얕은 클론이면 None → 'unknown'
    with tempfile.TemporaryDirectory() as tmp:
        for path in build_viz.build(str(csv), Path(tmp)):
            html = path.read_text(encoding="utf-8")
            assert site_stale.STAMP_RE.search(html), (
                f"{path.name}: 소스 표식이 없습니다 — 감시자의 소스 검사가 꺼집니다")
            assert site_stale.read_stamp(html) == expected, (
                f"{path.name}: 표식이 지금 소스 SHA 와 다릅니다 "
                f"({site_stale.read_stamp(html)!r} vs {expected!r})")
            # 굽는 쪽과 읽는 쪽이 같은 형식을 쓰는지 — 형식이 갈라지면 감시가 눈을 감는다.
            assert html.rstrip().endswith(site_stale.stamp_line(expected)), (
                f"{path.name}: 표식이 페이지 맨 끝에 있지 않습니다")


def test_rank_is_measured_over_every_day_not_the_thinned_series():
    """첫 화면의 "아래에서 14% 지점" 이 이 함수에서 나온다.

    추린 시계열로 세면 최근 400일이 일 단위라 최근 구간이 과대 대표되고,
    그러면 그 한 줄이 조용히 틀린다. 전체 일 단위로 세는지 못 박는다.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import export_viz

    rows = [{"bcs": v} for v in range(100)]       # 0 … 99
    assert export_viz.rank_of(rows, -1) == 0.0    # 역대 최저보다 낮다
    assert export_viz.rank_of(rows, 100) == 1.0   # 역대 최고보다 높다
    assert export_viz.rank_of(rows, 50) == 0.5
    assert export_viz.rank_of([], 0.0) == 0.0     # 빈 입력에 죽지 않는다

    # 같은 값이 여럿이어도 "이보다 낮았던 날" 이라는 정의를 지킨다
    assert export_viz.rank_of([{"bcs": 5}] * 4 + [{"bcs": 9}], 5) == 0.0


def test_build_with_macro_populates_overlay_and_stays_json_safe():
    """일간 워크플로는 --macro 로 굽지만 모든 테스트가 macro_csv=None 이었다 —
    거시 오버레이(export_viz.macro_lead + payload['macro'] 분기)가 프로덕션에서만
    돌던 공백을 메운다. 겸사겸사 payload 가 NaN 없는(유효 JSON) 상태인지도 본다."""
    import json
    import math

    sys.path.insert(0, str(ROOT / "tools"))
    import export_viz
    from btc_core.config import load_config

    market = ROOT / "data" / "market.csv"
    macro = ROOT / "data" / "macro.csv"
    if not (market.exists() and macro.exists()):
        pytest.skip("data/market.csv 또는 macro.csv 없음")

    payload = export_viz.build(load_config(), str(market), macro_csv=str(macro))
    m = payload.get("macro")
    assert m, "macro 분기가 payload 에 실리지 않았다"
    assert m["m2"] and m["btc"], "중국 M2·BTC 오버레이 배열이 비었다"

    imp = m.get("impulse")
    assert imp is None or math.isfinite(imp), "임펄스가 비유한(NaN/inf)이다"

    # NaN/inf 가 하나라도 새면 브라우저의 JSON.parse 가 죽는다. allow_nan=False 로 강제.
    json.dumps(payload, allow_nan=False)


def test_pages_are_self_contained():
    """외부 요청이 하나라도 있으면 막힌 환경에서 빈 화면이 된다."""
    import tempfile

    sys.path.insert(0, str(ROOT / "tools"))
    csv = ROOT / "data" / "market.csv"
    if not csv.exists():
        pytest.skip("data/market.csv 없음")

    import build_viz

    with tempfile.TemporaryDirectory() as tmp:
        for p in build_viz.build(str(csv), Path(tmp)):
            html = p.read_text(encoding="utf-8")
            # 페이지끼리 거는 링크(<a href>)는 예외다. 리소스 로드만 본다.
            for pat in (r'<script[^>]+src=', r'<link[^>]+href=',
                        r'@import\s', r'url\(\s*["\']?https?:'):
                assert not re.search(pat, html), f"{p.name}: 외부 리소스 {pat}"


def test_payload_json_is_strictly_valid():
    """NaN/Infinity 는 유효 JSON 이 아니다 — 하나라도 새면 브라우저의 JSON.parse 가
    죽어 사이트가 통째로 백지가 된다. 굽는 두 경로 모두 allow_nan=False 인지 본다."""
    import json
    import tempfile

    sys.path.insert(0, str(ROOT / "tools"))
    csv = ROOT / "data" / "market.csv"
    if not csv.exists():
        pytest.skip("data/market.csv 없음")
    import build_viz
    import export_viz
    from btc_core.config import load_config

    macro = ROOT / "data" / "macro.csv"
    payload = export_viz.build(load_config(), str(csv),
                               macro_csv=str(macro) if macro.exists() else None)
    json.dumps(payload, allow_nan=False)                 # 원본 payload
    json.dumps(build_viz.compact(payload), allow_nan=False)   # 페이지에 박히는 축약본

    # 실제로 구운 페이지에서 __BCS__ 를 꺼내 파싱해 본다 — 최종 산출물 검문.
    with tempfile.TemporaryDirectory() as tmp:
        page = build_viz.build(str(csv), Path(tmp))[0]
        html = page.read_text(encoding="utf-8")
        m = re.search(r"window\.__BCS__ = /\*__DATA__\*/(.*?)/\*__DATA__\*/;", html, re.S)
        assert m, "구운 페이지에서 __BCS__ 데이터를 못 찾았다"
        blob = m.group(1)
        for tok in ("NaN", "Infinity", "-Infinity"):
            assert tok not in blob, f"payload 에 무효 JSON 토큰 {tok}"
        json.loads(blob)


def test_no_dangling_element_ids_between_script_and_bodies():
    """리팩터로 본문에서 지운 요소를 스크립트가 계속 부르면(또는 그 반대) 화면
    일부가 조용히 빈다. 실제로 그런 사고가 반복돼 여기서 못 박는다.

    스크립트가 $("...") 로 부르는 id 는 어느 본문에든 존재해야 한다(공유 스크립트라
    페이지마다 없을 수는 있지만, 네 장 어디에도 없으면 잔재다)."""
    script = read("_script.html")
    bodies = " ".join(read(n) for n in BODIES + ("_nav.html",))
    # 뒤에 문자열을 이어 붙여 만드는 동적 id("tp-th-f"+(i+1))는 접두사만 남으므로
    # 여기서 제외한다 — 닫는 따옴표 뒤에 ')' 가 오는(=완성된 id) 것만 센다.
    called = set(re.findall(r'\$\("([a-zA-Z][\w-]*)"\)', script))
    called |= set(re.findall(r'setText\("([a-zA-Z][\w-]*)"\s*,', script))
    called |= set(re.findall(r'setHTML\("([a-zA-Z][\w-]*)"\s*,', script))
    # 스크립트가 스스로 만들어 넣는 id(동적 생성)는 제외한다.
    dynamic = set(re.findall(r'id="([a-zA-Z][\w-]*)"', script))
    concat = set(re.findall(r'"([a-zA-Z][\w-]*)"\s*\+', script))   # "prefix" + i
    missing = sorted(i for i in called
                     if f'id="{i}"' not in bodies and i not in dynamic
                     and i not in concat and not i.startswith("why-"))
    assert not missing, f"본문 어디에도 없는 id 를 스크립트가 부릅니다: {missing}"


def test_css_has_no_orphaned_id_rules_for_removed_elements():
    """#foo 규칙만 남고 요소가 사라진 경우를 잡는다 — 죽은 CSS 는 다음 사람이
    '있는 줄 알고' 고치게 만든다(실제로 #macro-fold 가 그랬다)."""
    head = read("_head.html")
    bodies = " ".join(read(n) for n in BODIES + ("_nav.html",))
    script = read("_script.html")
    ids = set(re.findall(r"#([a-zA-Z][\w-]*)\s*[,{ ]", head))
    orphan = sorted(i for i in ids
                    if f'id="{i}"' not in bodies and f'id="{i}"' not in script
                    and f'"{i}"' not in script)
    assert not orphan, f"요소가 없는데 남아 있는 CSS id 규칙: {orphan}"


def test_css_has_no_orphaned_class_rules():
    """id 와 같은 이유인데 class 쪽은 비어 있었다 — 실제로 `.brand`·`.logo`(머리띠에서
    사라진 로고), `.chip.pos/.neg/.warn`(만들어지지 않는 변형), `.panelbody .formula`
    다섯이 남아 있었다. 죽은 CSS 는 다음 사람이 '있는 줄 알고' 고치게 만든다.

    선택자 뒤에 무엇이 오든(공백·쉼표·중괄호·조합) 이름만 뽑고, 본문·스크립트
    어디에도 그 이름이 안 나오면 잔재로 본다. 스크립트가 문자열을 이어 붙여
    클래스를 만드는 경우가 있어(`chip ${cool|warm|flat}`) 이름 단위로 훑는다."""
    head = read("_head.html")
    used = " ".join(read(n) for n in
                    BODIES + ("_nav.html", "_script.html", "_rules_script.html"))
    # CSS 의사클래스·의사요소는 마침표 뒤에 오지 않으므로 `.` 로 시작하는 것만 본다.
    names = set(re.findall(r"\.([a-zA-Z][\w-]*)", head))
    orphan = sorted(n for n in names if n not in used)
    assert not orphan, f"쓰이지 않는 CSS 클래스 규칙: {orphan}"


def test_the_liquidity_chart_constants_still_match_the_measurement():
    """유동성 장의 '11개월 · ρ+0.44' 는 export_viz 에 **상수로 박혀 있다.**

    일부러 그렇게 뒀다 — 겹쳐 그리는 당김을 매일 재측정하면 그림이 하루마다
    흔들리고, 산문("측정 11개월")도 같이 흔들려야 한다. 대신 **드리프트가
    조용하면 안 된다.** 데이터가 자라 최적 선행이 옮겨 가면 여기서 걸려야
    화면·문서·상수를 한꺼번에 고칠 기회가 생긴다.

    관련성 지도(05)는 반대 선택을 했다 — 거기는 표라서 매일 다시 계산해 굽는다.
    무엇을 굳히고 무엇을 흐르게 둘지는 화면마다 다르고, 굳힌 쪽에는 이런 가드가
    따라와야 한다.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import macro_correlation as mc

    market = ROOT / "data" / "market.csv"
    macro = ROOT / "data" / "macro.csv"
    if not (market.exists() and macro.exists()):
        pytest.skip("data/market.csv 또는 macro.csv 없음")

    btc = mc.load_btc(str(market))
    cols = mc.load_macro(str(macro))
    if "m2_cn" not in cols:
        pytest.skip("macro.csv 에 m2_cn 이 없음")
    res = mc.m2_lead_lag(mc.month_end(btc), cols["m2_cn"])

    src = (ROOT / "tools" / "export_viz.py").read_text(encoding="utf-8")
    lead = int(re.search(r'"lead":\s*(\d+)', src).group(1))
    corr = float(re.search(r'"corr":\s*([\d.]+)', src).group(1))

    assert res["best_lag"] == lead, (
        f"측정된 선행이 {res['best_lag']}개월인데 화면 상수는 {lead}개월입니다 — "
        f"export_viz.macro_lead 와 viz/_i18n.html 의 산문을 함께 고치세요")
    assert abs(res["best_corr"] - corr) < 0.03, (
        f"측정 ρ {res['best_corr']:+.3f} vs 화면 상수 {corr:+.2f} — 같이 고치세요")


# ---------------------------------------------------------------------------
# 한 화면에 날짜가 둘 — 사용자가 실제로 본 상태
# ---------------------------------------------------------------------------
def freshness_body() -> str:
    m = re.search(r"function freshness\(\)\{(.*?)\n\}", js_of("_script.html"), re.S)
    assert m, "freshness() 를 못 찾았습니다"
    return m.group(1)


def test_the_header_and_the_hero_show_the_same_as_of_date():
    """머리글은 latest, 히어로는 current 를 써서 하루가 갈렸던 자리.

    실현시총은 계산으로 대체할 수 없고 커뮤니티 티어가 하루 늦다. 그래서 마지막
    하루는 커버리지가 모자라 기준일에서 빠지는데(export_viz._last_full), 그날
    머리글은 "데이터 09-08 종가", 히어로는 "기준일 09-07" 로 갈렸다. 정작 표시된
    가격은 09-07 종가였다 — **화면에 없는 날짜를 데이터 날짜라고 적고 있었다.**

    이 페이지의 숫자는 전부 current 에서 오므로 날짜도 current 하나여야 한다.
    """
    src = freshness_body()
    ic, il = src.find("D.current"), src.find("D.latest")
    assert ic != -1, "freshness() 가 current 를 안 봅니다"
    assert il == -1 or ic < il, (
        "freshness() 가 latest 를 기준으로 삼습니다 — 머리글과 히어로의 날짜가 "
        "다시 갈라집니다")

    hero = re.search(r"const latSub = ([^;]+);", js_of("_script.html"))
    assert hero, "히어로의 기준일 계산을 못 찾았습니다"
    assert hero.group(1).strip().startswith("(D.current"), (
        f"히어로가 다른 기준을 씁니다: {hero.group(1)!r}")


def test_a_held_back_day_is_explained_where_the_dates_differ():
    """기준일이 자료 마지막 날보다 이르면 **그 자리에서** 이유를 말해야 한다.

    머리글 위에 '2010-07-18 → 2026-09-08' 이 붙어 있어서, 설명이 없으면 끝
    날짜와 기준일이 하루 어긋난 채로 남는다. 검증 페이지 각주(footLate)에만
    적혀 있으면 첫 화면을 보는 사람은 못 본다.
    """
    js = js_of("_script.html")
    assert "freshHeld" in freshness_body(), "freshness() 가 보류 사실을 안 알립니다"
    held = re.findall(r"freshHeld: d => `([^`]*)`", js)
    assert len(held) == 2, f"보류 설명이 두 언어에 다 있어야 합니다: {held}"
    assert "점수에서 뺐" in held[0], f"한국어 설명이 이상합니다: {held[0]!r}"
    assert "held back" in held[1], f"영어 설명이 이상합니다: {held[1]!r}"


def test_the_as_of_date_actually_matches_what_is_displayed():
    """머리글이 적는 날짜가 정말 **표시된 숫자의 날짜**인지 node 로 재현한다.

    소스 검사는 '읽는 필드가 같다'까지만 보장한다. 같은 문자열이 나오는지는
    돌려 봐야 안다.
    """
    harness = r"""
const DAY = 86400000;
const T = {
  freshToday: "오늘",
  freshDays: (n, d) => `데이터 ${d} 종가`,
  freshStale: (n, d) => `데이터 ${d} 종가 · ${n}일 전 — 멈춤`,
  freshHeld: d => ` · ${d} 보류`,
  freshAtSource: " — 원본도 이 날까지",
};
function t(k){ const v = T[k];
  return typeof v === "function" ? v.apply(null, [].slice.call(arguments, 1)) : v; }
let OUT = "";
const $ = () => ({ set textContent(v){ OUT = v; }, set className(v){} });
let D;
function run(cur, lat, nowIso){
  D = { current: cur ? {d: cur} : null, latest: lat ? {d: lat} : null };
  const real = Date.now; Date.now = () => Date.parse(nowIso);
  try { freshness(); } finally { Date.now = real; }
  return OUT;
}
"""
    cases = r"""
console.log(JSON.stringify([
  run("2026-09-07", "2026-09-08", "2026-09-09T00:30:00Z"),
  run("2026-09-08", "2026-09-08", "2026-09-09T21:00:00Z"),
  run("2026-09-01", "2026-09-01", "2026-09-09T21:00:00Z"),
]));
"""
    body = "function freshness(){" + freshness_body() + "\n}"
    try:
        r = subprocess.run(["node", "-e", harness + body + cases],
                           capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pytest.skip("node 없음")
    assert r.returncode == 0, r.stderr[:800]
    held, plain, stale = json.loads(r.stdout.strip())

    # 보류가 있어도 기준일은 current(09-07) — 히어로가 적는 날과 같아야 한다
    assert held.startswith("데이터 2026-09-07 종가"), held
    assert "2026-09-08 보류" in held, f"보류 사실을 안 알립니다: {held}"
    assert "데이터 2026-09-08 종가" not in held, (
        f"표시되지 않은 날짜를 기준일로 적었습니다: {held}")

    # 뺀 날이 없으면 '원본도 이 날까지' 를 붙인다 — 달력으로 이틀 차이인 것이
    # 고장이 아니라는 답이 화면에서 끝나야 한다("자동 갱신이 안 된다" 보고 3회).
    assert plain == "데이터 2026-09-08 종가 — 원본도 이 날까지", plain
    assert "멈춤" in stale and "2026-09-01" in stale, stale
    # 멈춤일 때는 '원본도 이 날까지' 를 말하면 안 된다 — 거짓이 된다
    assert "원본도" not in stale, stale
    # 뺀 날이 있을 때도 마찬가지(원본에는 더 최신이 있다)
    assert "원본도" not in held, held


def test_the_source_claim_is_only_made_when_the_data_is_at_its_floor():
    """'원본도 이 날까지' 는 **어제 종가일 때만** 참이다.

    매 실행이 전 구간을 새로 받으므로 뺀 날이 없으면 우리 마지막 날 = 원본의
    마지막 날이다. 하지만 갱신이 멈춰 데이터가 하루씩 늙어 가는 중에도 같은
    말을 하면 거짓이 된다. 그래서 days <= 1 일 때만 적는다 — 애매하면 말하지
    않는 쪽이다(감시자에서 세운 원칙과 같다).
    """
    harness = r"""
const DAY = 86400000;
const T = {
  freshToday: "오늘",
  freshDays: (n, d) => `데이터 ${d} 종가`,
  freshStale: (n, d) => `데이터 ${d} 종가 · ${n}일 전 — 멈춤`,
  freshHeld: d => ` · ${d} 보류`,
  freshAtSource: " — 원본도 이 날까지",
};
function t(k){ const v = T[k];
  return typeof v === "function" ? v.apply(null, [].slice.call(arguments, 1)) : v; }
let OUT = "";
const $ = () => ({ set textContent(v){ OUT = v; }, set className(v){} });
let D;
function run(cur, lat, nowIso){
  D = { current: {d: cur}, latest: {d: lat} };
  const real = Date.now; Date.now = () => Date.parse(nowIso);
  try { freshness(); } finally { Date.now = real; }
  return OUT;
}
"""
    cases = r"""
console.log(JSON.stringify([
  run("2026-09-08", "2026-09-08", "2026-09-09T23:38:00Z"),  // 어제 종가 = 최선
  run("2026-09-08", "2026-09-08", "2026-09-10T12:00:00Z"),  // 이틀째 — 늙는 중
  run("2026-09-08", "2026-09-08", "2026-09-13T12:00:00Z"),  // 멈춤
  run("2026-09-07", "2026-09-08", "2026-09-09T23:38:00Z"),  // 뺀 날 있음
]));
"""
    body = "function freshness(){" + freshness_body() + "\n}"
    try:
        r = subprocess.run(["node", "-e", harness + body + cases],
                           capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pytest.skip("node 없음")
    assert r.returncode == 0, r.stderr[:800]
    floor, aging, stopped, held = json.loads(r.stdout.strip())

    assert floor.endswith("— 원본도 이 날까지"), floor
    assert "원본도" not in aging, f"늙어 가는 중에도 최신이라 말합니다: {aging}"
    assert "원본도" not in stopped, f"멈췄는데 최신이라 말합니다: {stopped}"
    assert "원본도" not in held, f"원본에 더 최신이 있는데 최신이라 말합니다: {held}"


def test_rebuilding_the_same_data_produces_the_same_bytes():
    """시간마다 갱신이 공짜인 근거가 이것이다.

    데이터가 그대로면 산출물이 바이트 단위로 같아야 커밋도 배포도 안 일어난다.
    빌드 시각처럼 매번 바뀌는 값을 공용 페이로드에 넣으면 이 성질이 깨져 시간마다
    여섯 장이 전부 바뀐다 — 실제로 한 번 그렇게 만들었다가 되돌렸다.
    """
    import tempfile

    sys.path.insert(0, str(ROOT / "tools"))
    csv = ROOT / "data" / "market.csv"
    if not csv.exists():
        csv = ROOT / "data" / "sample_synthetic.csv"
    if not csv.exists():
        pytest.skip("입력 CSV 없음")

    import build_viz

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        a = {p.name: p.read_bytes() for p in build_viz.build(str(csv), t / "a")}
        b = {p.name: p.read_bytes() for p in build_viz.build(str(csv), t / "b")}
        diff = [n for n in a if a[n] != b[n]]
        assert not diff, (
            f"같은 데이터로 두 번 구웠는데 달라진 장: {diff} — 시간마다 커밋·배포가 납니다")
