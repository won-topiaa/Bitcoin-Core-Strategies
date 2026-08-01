#!/usr/bin/env python3
"""비트코인 ↔ 광역 자산군 스크리닝 — 한 번에 훑고 같은 잣대로 거른다.

    python3 tools/macro_sweep.py                    # 전체 스윕
    python3 tools/macro_sweep.py --only 부동산 식품   # 자산군만 골라서

## 왜 별도 도구인가 (macro.csv 에 열로 안 넣는 이유)

`data/macro.csv` 는 **매일 자동 갱신**되고 사이트·점수가 읽는 파일이다. 여기 검정용
열을 무한정 늘리면 갱신이 느려지고 파일이 무거워진다. 반면 이 스윕의 25종은 **전부
기각**돼 점수·사이트가 한 줄도 읽지 않는다(docs/28). 그래서 데이터를 영구 보관하는
대신 **필요할 때 받아서 다시 재는 도구**로 남긴다 — 재현성은 그대로다(명령 한 줄).

정식 후보로 승격되는 열(gold·dxy·kospi·hy_spread 처럼 서사가 뚜렷해 반복 검증할
값어치가 있는 것)만 `fetch_macro.py` 의 CORE 로 올려 macro.csv 에 넣는다.

## 잣대 (금·엔캐리·코스피와 동일)

레벨이 아니라 **변화율**(가격류는 로그수익률, 음수가 가능한 금리·지수는 차분 Δ),
후보 빈도에 맞춘 주간/월간 격자, 국면 분할, 그리고 결정적 검정인
**ΔVIX·나스닥 통제 부분상관**. 통과 조건:

    |ρ| ≥ 0.25  AND  부호가 가설과 일치  AND  두 통제 후에도 |ρ| ≥ 0.2

## 다중검정을 숨기지 않는다

후보를 25개 훑으면 α=0.05 에서 **우연히 통과하는 것이 ~1개 나오는 게 정상**이다.
그래서 스윕은 언제나 **몇 개를 훑었는지 함께 출력**하고, 통과 후보가 나오면
순열·표본외 검정을 따로 붙이라고 알린다. 훑은 개수를 감추면 통과 하나는 발견이
아니라 자동으로 만들어지는 착시다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import macro_correlation as mc            # noqa: E402
from fetch_macro import PolicyBlocked, fetch_series   # noqa: E402


# (자산군, 라벨, FRED ID, 로그변환?, 기대부호, 인용되는 서사)
# 로그 = 가격·지수류(항상 양수) / 차분 = 금리·스프레드·지수(음수 가능)
SWEEP: list[tuple[str, str, str, bool, str, str]] = [
    # --- 부동산: "부동산 경기(침체·활황)와 엮인다" ---
    ("부동산", "케이스-실러 주택가격", "CSUSHPINSA", True, "+", "부동산 활황 = 자산가격 동조"),
    ("부동산", "주택착공", "HOUST", True, "+", "건설경기 = 경기확장"),
    ("부동산", "30년 모기지금리", "MORTGAGE30US", False, "-", "금리↑ = 부동산·위험자산 위축"),
    # --- 식품·농산물: "식품 인플레 = 화폐가치 훼손" ---
    ("식품", "글로벌 식품가격지수", "PFOODINDEXM", True, "+", "식품 인플레 = 화폐가치 훼손"),
    ("식품", "밀", "PWHEAMTUSDM", True, "+", "곡물 = 인플레·지정학"),
    ("식품", "옥수수", "PMAIZMTUSDM", True, "+", "곡물 = 인플레"),
    ("식품", "대두", "PSOYBUSDM", True, "+", "곡물 = 중국 수요"),
    # --- 주식: 개별 종목 대신 '집중도가 다른 두 지수'로 종목 질문에 답한다 ---
    ("주식", "나스닥100(메가캡 집중)", "NASDAQ100", True, "+", "메가캡 테크 = BTC 와 같은 고베타"),
    ("주식", "다우존스(구경제)", "DJIA", True, "+", "전통 대형주"),
    # --- 금융여건·불확실성 ---
    ("금융여건", "시카고연준 금융여건", "NFCI", False, "-", "여건 긴축 = 위험자산 하락"),
    ("금융여건", "세인트루이스 금융스트레스", "STLFSI4", False, "-", "스트레스↑ = 투매"),
    ("금융여건", "경제정책 불확실성", "USEPUINDXD", True, "-", "불확실성↑ = 위험회피"),
    ("금융여건", "은행 대출태도(긴축%)", "DRTSCILM", False, "-", "대출 조이면 유동성 위축"),
    # --- 실물경기·고용·심리 ---
    ("실물경기", "신규 실업수당청구", "ICSA", True, "-", "고용 악화 = 침체"),
    ("실물경기", "소비자심리", "UMCSENT", True, "+", "심리 개선 = 위험선호"),
    ("실물경기", "산업생산", "INDPRO", True, "+", "경기확장"),
    ("실물경기", "소매판매", "RSAFS", True, "+", "소비 = 유동성 체감"),
    # --- 원자재·에너지 ---
    ("원자재", "전체 원자재지수", "PALLFNFINDEXM", True, "+", "원자재 상승 = 인플레·성장"),
    ("원자재", "천연가스", "DHHNGSP", True, "+", "에너지 = 채굴원가·인플레"),
    ("원자재", "전기요금(채굴원가)", "APU000072610", True, "+", "채굴 원가가 가격 하단을 만든다"),
    # --- 통화·유동성 (중국 M2 외에 남은 것들) ---
    ("통화", "본원통화", "BOGMBASE", True, "+", "돈을 찍으면 위험자산으로"),
    ("통화", "M2 유통속도", "M2V", True, "-", "속도↓ = 돈이 자산으로 고임"),
    ("통화", "은행 총대출", "TOTBKCR", True, "+", "신용창출 = 유동성"),
    # --- 금리 구조 ---
    ("금리", "기간 프리미엄", "THREEFYTP10", False, "-", "기간 프리미엄↑ = 할인율↑"),
    ("금리", "선진국 대비 달러", "DTWEXAFEGS", True, "-", "달러 강세 = 유동성 긴축"),
]

SIZE_BAR = 0.25          # 주요 프레임 |ρ| 최소
PARTIAL_BAR = 0.20       # 통제 후에도 남아야 하는 |ρ|


def screen(btc, vix, nasdaq, specs) -> list[dict]:
    out = []
    for cls, label, fid, use_log, want, narrative in specs:
        try:
            s = fetch_series(fid)
        except PolicyBlocked:
            print(f"  ! {fid}: 정책 차단(403) — 건너뜀", file=sys.stderr)
            continue
        if not s:
            print(f"  ! {fid}: 받지 못함 — 건너뜀", file=sys.stderr)
            continue
        r = mc.candidate_analysis(btc, s, vix, nasdaq, use_log=use_log)
        prim = r["full_mo"] if r["monthly_only"] else r["full_wk"]
        pv, pn = r.get("partial_vix"), r.get("partial_ndq")
        size = prim is not None and abs(prim) >= SIZE_BAR
        sign = prim is not None and ((prim > 0) == (want == "+"))
        part = (pv is not None and abs(pv) >= PARTIAL_BAR
                and pn is not None and abs(pn) >= PARTIAL_BAR)
        out.append(dict(cls=cls, label=label, fid=fid, narrative=narrative, want=want,
                        prim=prim, n=r.get("n_primary", 0), freq=r["primary"],
                        pv=pv, pn=pn, size=size, sign=sign, part=part,
                        passed=size and sign and part,
                        best_lag=r.get("best_lag"), best_corr=r.get("best_corr")))
    return out


def _f(v) -> str:
    return f"{v:+.3f}" if isinstance(v, float) else "—"


def report(rows: list[dict], n_specs: int) -> str:
    L: list[str] = []
    add = L.append
    add("=" * 92)
    add("  비트코인 ↔ 광역 자산군 스크리닝")
    add(f"  통과 기준: |ρ| ≥ {SIZE_BAR} AND 부호 일치 AND ΔVIX·나스닥 통제 후 |ρ| ≥ {PARTIAL_BAR}")
    add("=" * 92)
    add(f"  {'자산군':10}{'지표':24}{'빈도':6}{'주요ρ':>9}{'n':>6}{'ΔVIX':>8}{'나스닥':>8}  판정")
    add("  " + "-" * 88)
    cur = None
    for r in sorted(rows, key=lambda x: (x["cls"], -abs(x["prim"] or 0))):
        head = r["cls"] if r["cls"] != cur else ""
        cur = r["cls"]
        why = "" if r["passed"] else (f"  (크기{'O' if r['size'] else 'X'}"
                                      f" 부호{'O' if r['sign'] else 'X'}"
                                      f" 통제{'O' if r['part'] else 'X'})")
        add(f"  {head:10}{r['label']:24}{r['freq']:6}{_f(r['prim']):>9}{r['n']:>6}"
            f"{_f(r['pv']):>8}{_f(r['pn']):>8}  {'통과 ✓' if r['passed'] else '기각 ✗'}{why}")

    npass = sum(1 for r in rows if r["passed"])
    add("  " + "-" * 88)
    add(f"  통과 {npass} / {len(rows)}종  (스윕 정의 {n_specs}종)")
    add("")
    # 기대 우연통과 수는 훑은 개수에 비례한다 — 개수와 무관하게 "~1개"라고 쓰면
    # 5종만 훑었을 때 과장이고, 50종을 훑었을 때는 심각한 과소평가가 된다.
    add(f"  ※ 다중검정: 후보 {len(rows)}개를 α=0.05 로 훑으면 우연 통과 기댓값이"
        f" 약 {0.05 * len(rows):.1f}개다.")
    add("     통과가 나와도 그 자체로는 발견이 아니다 — 순열검정·표본외 구간·국면별")
    add("     부호 일관성을 따로 확인해야 한다.")

    near = [r for r in rows if r["size"] and not r["passed"]]
    if near:
        add("")
        add("  [근접] 크기는 넘었으나 부호/통제에서 탈락:")
        for r in near:
            add(f"    {r['label']}  ρ={_f(r['prim'])}  ΔVIX={_f(r['pv'])}  나스닥={_f(r['pn'])}"
                f"   (기대부호 {r['want']})")
    ind = [r for r in rows if r["part"]]
    add("")
    if ind:
        add(f"  [독립성 생존] 통제 후에도 |ρ| ≥ {PARTIAL_BAR} 인 것:")
        for r in ind:
            add(f"    {r['label']}  주요={_f(r['prim'])}  ΔVIX={_f(r['pv'])}  나스닥={_f(r['pn'])}")
    else:
        add(f"  [독립성 생존] 없음 — 통제 후 |ρ| ≥ {PARTIAL_BAR} 인 후보가 하나도 없다.")
        # 여기서 '전부 나스닥 베타'로 뭉뚱그리면 사실이 아니다. 나스닥과 거의 직교한
        # 후보(실질금리·순유동성·수익률곡선 등)는 베타의 위장이 아니라 그냥 약해서
        # 떨어진 것이다. 두 사유를 구분해 적는다 (docs/28 의 정정).
        add("     단, 사유는 둘로 갈린다 — (a) 크기는 되는데 통제하면 사라지는 '위험선호")
        add("     대리변수', (b) 나스닥과 거의 직교하지만 애초에 너무 약한 것. 후자를")
        add("     '나스닥 베타'라 부르면 틀린다. 크기를 넘으면서 위험선호와 독립인 후보를")
        add("     아직 못 찾았을 뿐이다.")
    add("=" * 92)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="비트코인 ↔ 광역 자산군 스크리닝")
    ap.add_argument("--csv", default="data/market.csv", help="비트코인 가격 CSV")
    ap.add_argument("--macro", default="data/macro.csv", help="통제변수(vix·nasdaq) CSV")
    ap.add_argument("--only", nargs="*", default=None, help="자산군만 골라서 (예: 부동산 식품)")
    ap.add_argument("--out", default=None, help="리포트 저장 경로")
    args = ap.parse_args(argv)

    btc = mc.load_btc(args.csv)
    macro = mc.load_macro(args.macro)
    vix, nasdaq = macro.get("vix"), macro.get("nasdaq")
    if not vix or not nasdaq:
        raise SystemExit("통제변수가 없습니다 — macro.csv 에 vix·nasdaq 이 필요합니다.\n"
                         "  python3 tools/fetch_macro.py --github --global")

    specs = SWEEP
    if args.only:
        specs = [s for s in SWEEP if s[0] in set(args.only)]
        if not specs:
            raise SystemExit(f"해당 자산군이 없습니다: {args.only}\n"
                             f"  가능: {sorted({s[0] for s in SWEEP})}")

    print(f"FRED 에서 {len(specs)}종 받는 중…", file=sys.stderr)
    rows = screen(btc, vix, nasdaq, specs)
    if not rows:
        raise SystemExit("받은 시리즈가 없습니다 (네트워크·정책 확인).")
    text = report(rows, len(specs))
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
