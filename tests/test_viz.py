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

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VIZ = ROOT / "viz"
PIECES = ("_head.html", "_script.html", "_rules_script.html", "_i18n.html",
          "_nav.html", "index.body.html", "verify.body.html", "rules.body.html")


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
    for name in ("index.body.html", "verify.body.html", "rules.body.html"):
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
        assert len(paths) == 3
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
