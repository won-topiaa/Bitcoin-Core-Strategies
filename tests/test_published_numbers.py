"""여러 문서에 **같은 숫자가 복사돼 있는 자리**를 못 박는다.

이 저장소의 결론은 한 곳에서 계산되지만, 읽는 사람을 위해 여러 곳에 옮겨 적힌다 —
도구 출력 → 리포트 → 검정 문서 → README → 사이트(한국어·영어). 옮겨 적은 값은
원본이 바뀌어도 따라 바뀌지 않는다. 실제로 그렇게 갈라졌다:

  · LPPLS 최소오차 배수: 리포트를 다시 뽑아 3.4배가 됐는데 docs/20·README·사이트는
    3.5배로 남았다. 사이트는 **배포된 페이지에 그대로 실려 나갔다.**
  · docs/20 의 임계 격자에서 '저점 2022' 가 ≤10% 에 O 로 남아, 같은 문서 25줄 아래
    기저율 표(저점 1/4)와 정면으로 모순됐다.
  · 트레저리 검정 독스트링의 시대별 건수(8건)가 실제 목록(11건)과 달랐다.

전부 '누가 봐도 사소한' 불일치지만, 이 저장소가 파는 것은 숫자의 신뢰성이다.
사람이 눈으로 맞추는 대신 여기서 기계가 맞춘다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "lppls-2026-07.md"
DOC20 = ROOT / "docs" / "20-LPPLS-검정.md"
README = ROOT / "README.md"
RULES_KO = ROOT / "viz" / "rules.body.html"
I18N = ROOT / "viz" / "_i18n.html"


def read(p: Path) -> str:
    if not p.exists():
        pytest.skip(f"{p.name} 없음")
    return p.read_text(encoding="utf-8")


def peak_min_errors() -> list[float]:
    """리포트 1절 표의 고점 네 곳 '양의 거품' 최소 오차 — 이 값이 원본이다."""
    out = []
    for line in read(REPORT).splitlines():
        if not line.startswith("| 고점 |"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        for c in cells[3:]:                     # 국면·날짜·가격 다음이 양의 거품
            if re.fullmatch(r"\d+\.\d+", c):
                out.append(float(c))
                break
    return out


def test_the_lppls_error_ratio_is_the_same_everywhere():
    """'N배 차이' 는 리포트 1절 표에서 나온 값이다. 표를 다시 뽑으면 전부 따라와야 한다."""
    peaks = peak_min_errors()
    assert len(peaks) == 4, f"리포트 1절에서 고점 네 줄을 못 읽었습니다: {peaks}"
    ratio = max(peaks) / min(peaks)
    want = f"{ratio:.1f}"

    places = {
        "reports/lppls-2026-07.md": (read(REPORT), r"오차가 (\d+\.\d+)배"),
        "docs/20-LPPLS-검정.md": (read(DOC20), r"\*\*(\d+\.\d+)배 차이다"),
        "README.md": (read(README), r"로 (\d+\.\d+)배 차이라"),
        "viz/rules.body.html": (read(RULES_KO), r"오차가 (\d+\.\d+)배 벌어집니다"),
        "viz/_i18n.html": (read(I18N), r"error spreads by a factor of (\d+\.\d+)"),
    }
    bad = []
    for name, (text, pat) in places.items():
        found = set(re.findall(pat, text))
        if not found:
            bad.append(f"{name}: 배수 표현을 못 찾음")
        elif found != {want}:
            bad.append(f"{name}: {sorted(found)} (실측 {want})")
    assert not bad, (
        "LPPLS 최소오차 배수가 문서마다 다릅니다 — 리포트를 다시 뽑으면 "
        f"전부 {want}배 여야 합니다:\n  " + "\n  ".join(bad))


def test_the_lppls_error_range_in_the_readme_matches_the_table():
    """README 는 배수뿐 아니라 범위(0.09 ~ 0.31)도 적는다."""
    peaks = peak_min_errors()
    m = re.search(r"(\d+\.\d+) ~ (\d+\.\d+) 로 \d+\.\d+배 차이라", read(README))
    assert m, "README 에서 LPPLS 오차 범위 문장을 못 찾았습니다"
    lo, hi = float(m.group(1)), float(m.group(2))
    assert (round(min(peaks), 2), round(max(peaks), 2)) == (lo, hi), (
        f"README 범위 {lo}~{hi} 가 실측 {min(peaks):.2f}~{max(peaks):.2f} 와 다릅니다")


def grid_of(text: str, row_prefix) -> dict[str, tuple[str, ...]]:
    """마크다운 임계 격자에서 {국면: (·/O 다섯 칸)} 를 뽑는다."""
    out = {}
    for line in text.splitlines():
        key = row_prefix(line)
        if key is None:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        marks = tuple(c for c in cells if c in ("·", "O"))
        if len(marks) == 5:
            out[key] = marks
    return out


def test_doc20_threshold_grid_matches_the_report():
    """docs/20 의 격자는 리포트 2절 격자를 요약한 것이다. 갈라지면 같은 문서 안에서
    기저율 표와 모순된다 — 실제로 '저점 2022' 가 ≤10% 에 O 로 남아 그랬다."""
    def rep_key(line):
        if not (line.startswith("| 고점 |") or line.startswith("| 저점 |")):
            return None
        c = [x.strip() for x in line.strip("|").split("|")]
        if len(c) < 7 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", c[1]):
            return None
        return f"{c[0]} {c[1][:4]}"

    def doc_key(line):
        m = re.match(r"\|\s*(고점|저점)\s+(\d{4})\s*\|", line)
        return f"{m.group(1)} {m.group(2)}" if m else None

    rep = grid_of(read(REPORT), rep_key)
    doc = grid_of(read(DOC20), doc_key)
    if not rep or not doc:
        pytest.skip("격자를 못 읽었습니다 (형식이 바뀌었을 수 있음)")
    shared = set(rep) & set(doc)
    assert len(shared) >= 6, f"맞대 볼 행이 부족합니다: {sorted(shared)}"
    bad = [f"{k}: 문서 {doc[k]} vs 리포트 {rep[k]}" for k in sorted(shared) if doc[k] != rep[k]]
    assert not bad, "docs/20 의 임계 격자가 리포트와 다릅니다:\n  " + "\n  ".join(bad)


def test_the_treasury_docstring_counts_match_the_event_list():
    """'2020~2021 에 N건' 같은 서술은 목록이 늘면 조용히 틀린다."""
    import treasury_study as ts

    m = re.search(r"(\d+)~(\d+) 강세장에 (\d+)건, (\d+)~(\d+) 강세장에 (\d+)건",
                  ts.__doc__ or "")
    assert m, "treasury_study 독스트링에서 시대별 건수 문장을 못 찾았습니다"
    for lo, hi, said in ((m.group(1), m.group(2), m.group(3)),
                         (m.group(4), m.group(5), m.group(6))):
        real = sum(1 for d, *_ in ts.EVENTS if int(lo) <= int(d[:4]) <= int(hi))
        assert real == int(said), (
            f"{lo}~{hi} 건수: 독스트링 {said} vs 실제 목록 {real}")
