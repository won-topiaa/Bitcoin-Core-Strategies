"""언어 전환이 반쯤만 되는 상태를 막는다.

화면을 두 언어로 띄우기로 하면 새 실패 방식이 하나 생긴다 — **어긋남**이다.
지표를 하나 추가하고 영어 문자열을 잊으면 영어 화면 한가운데에 한국어가
남는다. 그건 번역하지 않은 화면보다 나쁘다. 그 어긋남이 생길 수 있는 곳이
셋이고, 여기서 셋 다 본다.

    1. 본문 산문      viz/*.body.html 의 data-t  ↔  viz/_i18n.html
    2. 화면 생성 문자열 viz/_script.html 의 TX.ko  ↔  TX.en
    3. 설정 문자열     config/strategy.yaml       ↔  config/strings.en.yaml

그리고 구조 하나를 못 박는다: **data-t 는 겹쳐 쓸 수 없다.** 언어를 바꿀 때
부모의 innerHTML 을 갈아 끼우면 그 안의 data-t 자식이 사라져서 한국어로
되돌릴 원본을 잃는다.
"""

from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VIZ = ROOT / "viz"
BODIES = ("index.body.html", "verify.body.html", "rules.body.html")
NAV = "_nav.html"
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "col", "area"}


def read(rel: str) -> str:
    p = VIZ / rel
    if not p.exists():
        pytest.skip(f"{rel} 없음")
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 1. 본문 산문
# --------------------------------------------------------------------------
def body_keys() -> dict[str, set[str]]:
    out = {}
    for name in BODIES + (NAV,):
        out[name] = set(re.findall(r'data-t="([^"]+)"', read(name)))
    return out


def i18n_table() -> dict:
    """_i18n.html 을 실제로 실행해서 사전을 꺼낸다.

    정규식으로 키를 세는 것도 해 봤는데, 여러 줄 템플릿 문자열이 섞여 있어서
    본문 한 줄을 키로 잘못 읽는 경우가 생긴다. 자바스크립트 객체를 읽으려면
    자바스크립트로 읽어야 한다.
    """
    js = re.sub(r"^<script>|</script>\s*$", "", read("_i18n.html").strip())
    prog = "var window={};\n" + js + "\nconsole.log(JSON.stringify(window.__I18N__));"
    try:
        r = subprocess.run(["node", "-e", prog], capture_output=True,
                           text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pytest.skip("node 없음 — 사전을 실행해 볼 수 없다")
    assert r.returncode == 0, r.stderr[:600]
    return json.loads(r.stdout)


def test_every_body_key_has_english():
    """data-t 를 붙였는데 사전에 없으면 그 자리는 영어에서도 한국어로 남는다."""
    table = i18n_table()
    missing = {}
    for name, keys in body_keys().items():
        gap = sorted(k for k in keys if k not in table)
        if gap:
            missing[name] = gap
    assert not missing, f"영어가 없는 키: {missing}"


def test_no_orphan_english_keys():
    """사전에만 있는 키는 지워진 문단의 잔해다 — 다음 사람을 헷갈리게 한다."""
    used = set().union(*body_keys().values())
    orphans = sorted(k for k in i18n_table() if k not in used)
    assert not orphans, f"쓰이지 않는 키: {orphans}"


def test_english_values_are_not_empty():
    empty = sorted(k for k, v in i18n_table().items() if not str(v).strip())
    assert not empty, f"빈 번역: {empty}"


def test_english_values_cannot_break_out_of_the_script_tag():
    """값 안에 </script> 가 있으면 그 지점에서 스크립트가 끊긴다."""
    bad = sorted(k for k, v in i18n_table().items()
                 if "</script" in str(v).lower())
    assert not bad, f"</script> 를 품은 값: {bad}"


class _Nesting(HTMLParser):
    """data-t 요소 안에 또 data-t 요소가 있는지 본다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None]] = []
        self.bad: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        key = dict(attrs).get("data-t")
        if key is not None:
            outer = next((k for _, k in reversed(self.stack) if k), None)
            if outer:
                self.bad.append((outer, key))
        self.stack.append((tag, key))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


@pytest.mark.parametrize("name", BODIES + (NAV,))
def test_data_t_never_nests(name):
    """부모를 갈아 끼우면 자식의 원본이 사라진다. 그러면 한국어로 못 돌아온다."""
    p = _Nesting()
    p.feed(read(name))
    assert not p.bad, f"{name}: 겹친 data-t {p.bad}"


# --------------------------------------------------------------------------
# 2. 화면 생성 문자열
# --------------------------------------------------------------------------
def tx_keys() -> tuple[set[str], set[str]]:
    js = read("_script.html")
    m = re.search(r"\nconst TX = \{\nko: \{(.*?)\n\},\nen: \{(.*?)\n\}\n\};",
                  js, re.S)
    assert m, "_script.html 의 TX 표를 찾지 못했습니다 — 형태가 바뀌었나요?"
    top = lambda part: set(re.findall(r"^  (\w+):", part, re.M))
    return top(m.group(1)), top(m.group(2))


def test_generated_strings_match_across_languages():
    """숫자가 끼어드는 문장은 언어마다 함수로 둔다. 한쪽만 고치면 여기서 걸린다."""
    ko, en = tx_keys()
    assert ko and en
    assert not (ko - en), f"영어에 없는 키: {sorted(ko - en)}"
    assert not (en - ko), f"한국어에 없는 키: {sorted(en - ko)}"


# --------------------------------------------------------------------------
# 3. 설정 문자열
# --------------------------------------------------------------------------
FIELDS = {
    "indicators": ("label", "explain", "explain_long"),
    "families": ("label", "explain"),
    "bands": ("label", "stance"),
}


def config_pairs() -> tuple[dict, dict]:
    cfg = yaml.safe_load((ROOT / "config" / "strategy.yaml").read_text(encoding="utf-8"))
    p = ROOT / "config" / "strings.en.yaml"
    if not p.exists():
        pytest.skip("config/strings.en.yaml 없음")
    return cfg, yaml.safe_load(p.read_text(encoding="utf-8"))


def wanted(cfg: dict) -> dict[str, dict[str, tuple[str, ...]]]:
    """번역이 필요한 (그룹, 키, 필드). BCS 에 실제로 쓰이는 것만 센다."""
    fams = cfg["bcs"]["families"]
    used = [m for f in fams.values() for m in f["members"]]
    out: dict[str, dict[str, tuple[str, ...]]] = {"indicators": {}, "families": {}, "bands": {}}
    for k in used:
        spec = cfg["indicators"][k]
        out["indicators"][k] = tuple(f for f in FIELDS["indicators"] if spec.get(f))
    for k, f in fams.items():
        out["families"][k] = tuple(g for g in FIELDS["families"] if f.get(g))
    for b in cfg["bands"]:
        out["bands"][b["key"]] = tuple(g for g in FIELDS["bands"] if b.get(g))
    return out


def test_every_config_string_has_english():
    """지표를 추가하고 여기를 잊으면 영어 화면에 한국어 지표 이름이 뜬다."""
    cfg, en = config_pairs()
    gaps = []
    for group, items in wanted(cfg).items():
        table = en.get(group) or {}
        for key, fields in items.items():
            row = table.get(key) or {}
            for f in fields:
                if not str(row.get(f, "")).strip():
                    gaps.append(f"{group}.{key}.{f}")
    assert not gaps, f"영어가 없는 설정 문자열: {gaps}"


def test_no_orphan_config_english():
    cfg, en = config_pairs()
    want = wanted(cfg)
    orphans = []
    for group in FIELDS:
        for key in (en.get(group) or {}):
            if key not in want[group]:
                orphans.append(f"{group}.{key}")
    assert not orphans, f"설정에 없는 영어 항목: {orphans}"


def state_specs(cfg: dict) -> dict[str, dict]:
    return {k: s for k, s in cfg["indicators"].items() if s.get("states")}


def test_states_have_korean_screen_names():
    """상태 키를 그대로 띄우면 화면에 "expansion" 이라고 적힌다 — 실제로 그랬다."""
    cfg, _en = config_pairs()
    gaps = []
    for key, spec in state_specs(cfg).items():
        labels = spec.get("state_labels") or {}
        for s in spec["states"]:
            if not str(labels.get(s, "")).strip():
                gaps.append(f"{key}.{s}")
    assert not gaps, f"한국어 이름이 없는 상태: {gaps}"


def test_states_have_english_screen_names():
    cfg, en = config_pairs()
    table = en.get("states") or {}
    want = {s for spec in state_specs(cfg).values() for s in spec["states"]}
    missing = sorted(s for s in want if not str(table.get(s, "")).strip())
    assert not missing, f"영어가 없는 상태: {missing} (있는 것: {sorted(table)})"
    orphans = sorted(s for s in table if s not in want)
    assert not orphans, f"설정에 없는 상태 이름: {orphans}"


# --------------------------------------------------------------------------
# 페이지에 실제로 실려 나가는가
# --------------------------------------------------------------------------
def test_built_pages_carry_both_languages():
    import sys
    import tempfile

    sys.path.insert(0, str(ROOT / "tools"))
    csv = ROOT / "data" / "market.csv"
    if not csv.exists():
        pytest.skip("data/market.csv 없음")
    import build_viz

    with tempfile.TemporaryDirectory() as tmp:
        for p in build_viz.build(str(csv), Path(tmp)):
            html = p.read_text(encoding="utf-8")
            assert "window.__I18N__" in html, f"{p.name}: 사전이 안 실렸습니다"
            assert 'id="lang-btn"' in html, f"{p.name}: 언어 버튼이 없습니다"
            assert 'id="theme-btn"' in html, f"{p.name}: 테마 버튼이 없습니다"
            # 첫 페인트 전에 테마를 적용하는 인라인 스크립트
            assert '"bcs.theme"' in html, f"{p.name}: 테마 선택을 읽는 코드가 없습니다"
            assert '"label_en"' in html, f"{p.name}: 설정 영어가 안 실렸습니다"
