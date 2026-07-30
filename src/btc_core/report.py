"""스냅샷을 사람이 읽는 형태로.

콘솔(터미널), 마크다운(저널에 붙여넣기), JSON(다른 도구로 넘기기) 세 가지.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Optional

from .config import StrategyConfig
from .models import Reading, Snapshot

BAR_WIDTH = 21          # 홀수라야 가운데(0)가 딱 떨어진다


def gauge(score: Optional[float], width: int = BAR_WIDTH) -> str:
    """[-1,+1] 을 텍스트 막대로. 가운데가 0."""
    if score is None:
        return "·" * width
    mid = width // 2
    pos = int(round((score + 1.0) / 2.0 * (width - 1)))
    pos = max(0, min(width - 1, pos))
    cells = ["─"] * width
    cells[mid] = "┼"
    cells[pos] = "●"
    return "".join(cells)


def range_gauge(low: Optional[float], high: Optional[float], point: Optional[float],
                width: int = BAR_WIDTH) -> str:
    """구간을 막대로. 양 끝이 ├ ┤ 이고 점이 ● 다.

    점 하나만 찍으면 사람은 그 소수점을 믿는다. 구간을 그리면 못 믿는다 —
    그게 이 함수가 존재하는 이유다.
    """
    if low is None or high is None:
        return "·" * width

    def cell(v: float) -> int:
        return max(0, min(width - 1, int(round((v + 1.0) / 2.0 * (width - 1)))))

    lo, hi = cell(low), cell(high)
    if lo > hi:
        lo, hi = hi, lo
    cells = ["─"] * width
    cells[width // 2] = "┼"
    for i in range(lo, hi + 1):
        cells[i] = "━"
    cells[lo] = "├"
    cells[hi] = "┤"
    if point is not None:
        cells[cell(point)] = "●"
    return "".join(cells)


def display_width(s: str) -> int:
    """한글·한자는 터미널에서 두 칸을 먹는다. str.ljust 는 그걸 모른다."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def pad(s: str, width: int, align: str = "<") -> str:
    """동아시아 문자 폭을 반영한 정렬. 넘치면 잘라내고 … 를 붙인다."""
    if display_width(s) > width:
        out = ""
        for ch in s:
            if display_width(out + ch) > width - 1:
                break
            out += ch
        s = out + "…"
    gap = " " * max(0, width - display_width(s))
    return s + gap if align == "<" else gap + s


def fmt_condition_value(value: Optional[float], unit: str) -> str:
    """프로파일 값 표기. 일수는 정수, 나머지는 소수 둘째 자리."""
    if value is None:
        return "—"
    if unit == "일":
        return f"{value:,.0f}일"
    if unit == "%":
        return f"{value:,.1f}%"
    return f"{value:,.2f}{unit}"


def fmt_condition_target(cond) -> str:
    """기준 표기. between 은 구간이라 두 값을 함께 보여준다."""
    th, unit = cond.threshold, cond.unit
    if cond.op == "between" and isinstance(th, (list, tuple)):
        return f"{th[0]:g}~{th[1]:g}{unit}"
    return f"{cond.op} {th:g}{unit}"


def short_label(label: str) -> str:
    """괄호 안 계산식은 표에서 떼어낸다. 'MVRV Z-Score (…)' → 'MVRV Z-Score'"""
    return label.split(" (")[0].strip()


def _fmt_raw(r: Reading) -> str:
    if r.raw is None:
        return "—"
    if isinstance(r.raw, str):
        return r.raw
    return f"{r.raw:,.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# 콘솔
# ---------------------------------------------------------------------------

def render_console(snap: Snapshot, cfg: StrategyConfig) -> str:
    L: list[str] = []
    add = L.append

    add("═" * 78)
    add(f"  BCS / LRS 스냅샷   기준일 {snap.as_of}")
    add("═" * 78)

    price = f"${snap.price:,.0f}" if snap.price else "—"
    add(f"  현재가 {price}    커버리지 {snap.coverage:.0%}")
    add("")

    # --- 점수 ---
    # BCS 는 점이 아니라 구간으로 쓴다. 지표 하나를 빼고 다시 계산한 값들의
    # 범위이고, 16년 실측 중위 폭이 16.6점이다 — 소수점을 믿을 수 없다는 뜻이
    # 아니라 **믿으면 안 된다는 뜻**이다.
    iv = snap.bcs_interval
    if iv is not None:
        bcs_txt = f"{iv.low:+.0f} ~ {iv.high:+.0f}"
        bar = range_gauge(iv.low / 100, iv.high / 100, iv.point / 100)
    else:
        bcs_txt = f"{snap.bcs:+.1f}" if snap.bcs is not None else "산출 불가"
        # 결측을 0 으로 바꿔 넘기면 "산출 불가"라고 써놓고 바늘은 중립을 가리킨다.
        bar = gauge(snap.bcs / 100 if snap.bcs is not None else None)
    add(f"  BCS  사이클 위치   {bcs_txt:>10}   {bar}")

    if iv is not None and not iv.stable:
        lo_label = cfg.band_for(iv.low).get("label", "")
        hi_label = cfg.band_for(iv.high).get("label", "")
        add(f"       {lo_label} ~ {hi_label} — 두 국면 사이 (점 {iv.point:+.1f})")
        if snap.plan:
            add(f"       실행은 {snap.plan.band_label} 기준 — {snap.plan.stance}")
    elif snap.plan:
        point_txt = f" (점 {iv.point:+.1f})" if iv is not None else ""
        add(f"       {snap.plan.band_label} — {snap.plan.stance}{point_txt}")
    if iv is not None and iv.dropped_low and iv.dropped_high:
        add(f"       폭 {iv.width:.0f}점 — 하단은 '{short_label(iv.dropped_low)}' 제외 시, "
            f"상단은 '{short_label(iv.dropped_high)}' 제외 시")
    lrs_txt = f"{snap.lrs:+.1f}" if snap.lrs is not None else "산출 불가"
    lrs_label = cfg.lrs_band_for(snap.lrs).get("label", "") if snap.lrs is not None else ""
    add(f"  LRS  유동성 레짐   {lrs_txt:>10}   "
        f"{gauge(snap.lrs / 100 if snap.lrs is not None else None)}")
    if lrs_label:
        add(f"       {lrs_label}")
    add("")

    # --- 계열 ---
    add("─" * 78)
    add("  계열별 점수")
    add("─" * 78)
    for f in snap.families:
        score = f"{f.score:+.2f}" if f.available else "결측"
        w = f"{f.effective_weight:.0f}%" if f.available else f"({f.weight:.0f}%)"
        add(f"  {pad(f.label, 16)} {score:>7}  가중 {w:>5}  {gauge(f.score)}")
        for m in f.members:
            ms = f"{m.score:+.2f}" if m.available else "  —  "
            flag = "" if m.available else "  ✗ 결측"
            add(f"      {pad(short_label(m.label), 34)} {_fmt_raw(m):>12}  → {ms}{flag}")
        add("")

    # --- 합의 ---
    add("─" * 78)
    c = snap.consensus
    if c:
        mark = "✔ 통과" if c.passed else "✘ 미달"
        add(f"  합의 게이트  {mark}")
        add(f"    {c.reason}")
        if c.agreeing:
            add(f"    동의 계열: {', '.join(c.agreeing)}")
    add("")

    # --- 실행 계획 ---
    if snap.plan:
        p = snap.plan
        add("─" * 78)
        add("  실행 계획")
        add("─" * 78)
        for a in p.actions:
            mark = "▶" if a.executable else "⏸"
            add(f"  {mark} {a.label}")
            if a.blocked_by:
                add(f"      보류: {a.blocked_by}")
        add("")

        if p.floors:
            add("  바닥선")
            for fl in p.floors:
                shown = f"${fl.price:,.0f}" if fl.price else "—"
                add(f"    {pad(fl.label, 22)} {shown:>12}   (가중 {fl.weight:.0%})")
            if p.reference_floor:
                shown = f"${p.reference_floor:,.0f}"
                add(f"    {pad('기준 바닥 (가중평균)', 22)} {shown:>12}")
            add("")

        if p.buy_zones:
            add("  분할 매수 구간 (예비 현금 기준)")
            for z in p.buy_zones:
                mark = "◆" if z.reached else " "
                shown = f"${z.price:,.0f}"
                dist = f"{z.distance_pct:+7.1f}%" if z.distance_pct is not None else "      —"
                add(f"    {mark} {pad(z.label, 34)} {shown:>12}  "
                    f"{dist}  →  {z.pct_of_reserve:>3.0f}%")
            add("")

        for prof, title in ((p.bottom_profile, "저점"), (p.top_profile, "고점")):
            if prof is None or not prof.evaluable:
                continue
            add(f"  {title} 프로파일  {prof.hits}/{prof.total} — {prof.label}")
            add(f"  (사이클 {title}의 공통 조건. 참고용이며 점수에 반영되지 않습니다)")
            for c in prof.conditions:
                mark = "O" if c.met else "X"
                shown = fmt_condition_value(c.value, c.unit)
                add(f"    {mark} {pad(short_label(c.label), 30)} {shown:>11}   "
                    f"기준 {fmt_condition_target(c)}")
            if prof.detail:
                add(f"    → {prof.detail}")
            add("")

        if p.notes:
            add("  메모")
            for n in p.notes:
                add(f"    · {n}")
            add("")

    if snap.warnings:
        add("─" * 78)
        add("  경고")
        for w in snap.warnings:
            add(f"    ! {w}")
        add("")

    add("═" * 78)
    add("  이 출력은 투자 자문이 아닙니다. 판단과 결과의 책임은 본인에게 있습니다.")
    add("═" * 78)
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 마크다운 — 저널에 그대로 붙여넣는 용도
# ---------------------------------------------------------------------------

def render_markdown(snap: Snapshot, cfg: StrategyConfig) -> str:
    L: list[str] = []
    add = L.append

    add(f"# BCS 스냅샷 — {snap.as_of}")
    add("")
    add(f"- 현재가: **{('$' + format(snap.price, ',.0f')) if snap.price else '—'}**")
    iv = snap.bcs_interval
    if iv is not None:
        add(f"- **BCS {iv.low:+.0f} ~ {iv.high:+.0f}** (점 {iv.point:+.1f}, 폭 {iv.width:.0f})")
    elif snap.bcs is not None:
        add(f"- **BCS {snap.bcs:+.1f}**")
    else:
        add("- **BCS 산출 불가**")
    if iv is not None and not iv.stable:
        add(f"- 밴드: **{cfg.band_for(iv.low).get('label','')} ~ "
            f"{cfg.band_for(iv.high).get('label','')}** — 두 국면 사이")
        if snap.plan:
            add(f"  - 실행은 {snap.plan.band_label} 기준 — {snap.plan.stance}")
    elif snap.plan:
        add(f"- 밴드: **{snap.plan.band_label}** — {snap.plan.stance}")
    if snap.lrs is not None:
        add(f"- LRS {snap.lrs:+.1f} — {cfg.lrs_band_for(snap.lrs).get('label','')}")
    add(f"- 커버리지: {snap.coverage:.0%}")
    add("")

    add("## 계열별 점수")
    add("")
    add("| 계열 | 점수 | 실효 가중 | 구성 지표 |")
    add("|---|---:|---:|---|")
    for f in snap.families:
        members = ", ".join(
            f"{short_label(m.label)} {m.score:+.2f}" if m.available
            else f"~~{short_label(m.label)}~~"
            for m in f.members
        )
        score = f"{f.score:+.2f}" if f.available else "결측"
        add(f"| {f.label} | {score} | {f.effective_weight:.0f}% | {members} |")
    add("")

    if snap.lrs_readings:
        add("## 거시 (LRS)")
        add("")
        add("| 구성요소 | 입력값 | 점수 |")
        add("|---|---:|---:|")
        for r in snap.lrs_readings:
            add(f"| {short_label(r.label)} | {_fmt_raw(r)} | "
                f"{f'{r.score:+.2f}' if r.available else '결측'} |")
        add("")

    if snap.consensus:
        c = snap.consensus
        add("## 합의 게이트")
        add("")
        add(f"**{'통과' if c.passed else '미달'}** — {c.reason}")
        add("")

    if snap.plan:
        p = snap.plan
        add("## 실행 계획")
        add("")
        for a in p.actions:
            mark = "**실행**" if a.executable else "보류"
            add(f"- [{mark}] {a.label}")
            if a.blocked_by:
                add(f"  - 사유: {a.blocked_by}")
        add("")

        if p.buy_zones:
            add("### 분할 매수 구간")
            add("")
            add("| 구간 | 가격 | 현재가 대비 | 예비현금 배분 | 도달 |")
            add("|---|---:|---:|---:|:--:|")
            for z in p.buy_zones:
                dist = f"{z.distance_pct:+.1f}%" if z.distance_pct is not None else "—"
                add(f"| {z.label} | ${z.price:,.0f} | {dist} | "
                    f"{z.pct_of_reserve:.0f}% | {'◆' if z.reached else ''} |")
            add("")

        for prof, title in ((p.bottom_profile, "저점"), (p.top_profile, "고점")):
            if prof is None or not prof.evaluable:
                continue
            add(f"### {title} 프로파일 — {prof.hits}/{prof.total} ({prof.label})")
            add("")
            add(f"사이클 {title}의 공통 조건. **점수에 반영되지 않는 참고 정보다.**")
            add("")
            add(f"| | 조건 | 현재값 | 기준 | 과거 {title} 관측값 |")
            add("|:--:|---|---:|---|---|")
            for c in prof.conditions:
                mark = "O" if c.met else "X"
                shown = fmt_condition_value(c.value, c.unit)
                add(f"| {mark} | {c.label} | {shown} | {fmt_condition_target(c)} "
                    f"| {c.observed} |")
            add("")
            if prof.detail:
                add(f"> {prof.detail}")
                add("")

        if p.notes:
            add("### 메모")
            add("")
            for n in p.notes:
                add(f"- {n}")
            add("")

    if snap.warnings:
        add("### 경고")
        add("")
        for w in snap.warnings:
            add(f"- {w}")
        add("")

    add("---")
    add("")
    add("### 이번 판단 기록 (직접 채울 것)")
    add("")
    add("- 무엇을 근거로 어떻게 판단했나:")
    add("- 실제로 실행한 것:")
    add("- 이 판단이 틀렸다면 무엇 때문일 것 같은가:")
    add("")
    add("> 정보 제공 목적이며 투자 자문이 아닙니다.")
    return "\n".join(L)


def render_json(snap: Snapshot) -> str:
    return json.dumps(snap.as_dict(), ensure_ascii=False, indent=2)
