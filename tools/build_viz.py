#!/usr/bin/env python3
"""시각화 페이지를 만든다 — 데이터를 페이지 안에 굽는다.

    python3 tools/build_viz.py --csv data/market.csv    # viz/site/ 에 다섯 장

페이지는 **자기완결이어야 한다.** 외부 요청이 하나라도 있으면 그 요청이 막힌
환경에서 빈 화면이 나오고, 무료 데이터로 돌아가는 시스템에서 그건 곧 "안 보인다"
와 같다. 그래서 CSS·JS·데이터를 전부 한 파일에 넣는다.

    viz/_head.html        스타일 (전 장 공용)
    viz/_script.html      차트 엔진 (전 장 공용, 없는 요소는 건너뛴다)
    viz/*.body.html       페이지별 본문
    tools/export_viz.py   하루씩 계산해 JSON 을 만든다
    이 파일                조각을 합쳐 자기완결 HTML 로 굽는다 (PAGES 가 목록)

데이터는 주 단위로 추리고(전환점 주변·최근 400일은 일 단위) 좌표를 배열로
눕혀 담는다. 그렇게 해야 16년치가 100KB 안쪽에 들어온다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import export_viz  # noqa: E402

from btc_core.config import load_config  # noqa: E402

# 페이지는 여럿이고 데이터는 하나다. 같은 payload 를 모든 조각에 굽는다 —
# 어느 한 장이 오래된 숫자를 보여주는 일이 없게.
#
# 머리(스타일)와 차트 엔진은 조각으로 빼서 전 장이 공유한다. 예전에는 틀마다
# 스타일 블록을 통째로 복제하고 있었고, 한쪽만 고치면 두 장의 생김새가
# 갈라졌다.
HEAD = "viz/_head.html"
SCRIPT = "viz/_script.html"
I18N = "viz/_i18n.html"
NAV = "viz/_nav.html"

PAGES = (
    # (본문 조각, 파일명, 페이지 키, <title>, 추가 스크립트)
    ("viz/index.body.html",  "index.html",  "index",
     "지금 어디쯤인가 — 비트코인 사이클 위치", None),
    ("viz/verify.body.html", "verify.html", "verify",
     "이 숫자를 믿어도 되나 — 비트코인 사이클 위치", None),
    ("viz/rules.body.html",  "rules.html",  "rules",
     "규칙과 한계 — 비트코인 사이클 위치", "viz/_rules_script.html"),
    ("viz/liquidity.body.html", "liquidity.html", "liquidity",
     "중국 유동성 — 비트코인 사이클 위치", None),
    ("viz/relationship.body.html", "relationship.html", "relationship",
     "무엇과 관련 있나 — 비트코인 사이클 위치", None),
)
MARKER = "/*__DATA__*/"


def _n(v, digits: int = 2):
    """소수 자리를 줄이고 정수는 정수로 — 파일 크기의 대부분이 여기서 결정된다."""
    if v is None:
        return None
    r = round(v, digits)
    return int(r) if r == int(r) else r


def compact(payload: dict) -> dict:
    """리포트용 JSON 을 페이지용으로 압축한다.

    시계열은 객체 대신 배열로 눕힌다. 키 이름이 1,648번 반복되면 그것만으로
    수십 KB 다. 배열 순서는 차트 엔진의 인덱스와 맞아야 한다:

        0 날짜오프셋  1 가격  2 BCS  3 하단  4 상단
        5 밸류에이션  6 가격·추세  7 공급·채굴  8 합의게이트  9 결측수
    """
    base = date.fromisoformat(payload["span"][0])
    rows = []
    for r in payload["series"]:
        f = r["fam"]
        rows.append([
            (date.fromisoformat(r["d"]) - base).days,
            _n(r["price"], 2), _n(r["bcs"], 1), _n(r["low"], 1), _n(r["high"], 1),
            _n(f.get("valuation"), 2), _n(f.get("price"), 2), _n(f.get("supply"), 2),
            1 if r["gate"] else 0, r["nmiss"],
        ])

    def point(c: dict) -> dict:
        return {
            "d": c["d"], "price": _n(c["price"], 0), "bcs": _n(c["bcs"], 1),
            "low": _n(c["low"], 1), "high": _n(c["high"], 1),
            "band": c["band"], "stable": c["stable"], "cov": c["cov"],
            "gate": c["gate"], "nmiss": c["nmiss"], "miss": c.get("miss", []),
            "fam": {k: _n(v, 2) for k, v in c["fam"].items()}, "dsh": c["dsh"],
            "rank": c.get("rank_pct"),
            "ind": c.get("ind", {}),
        }

    def tp(t: dict) -> dict:
        return {
            "phase": t["phase"], "d": t["d"], "price": _n(t["price"], 0),
            "bcs": _n(t["bcs"], 1), "low": _n(t["low"], 1), "high": _n(t["high"], 1),
            "stable": t["stable"], "band": t["band"],
            # 반감기 이후 경과일. 화면의 "과거 고점은 …일에 나왔습니다" 를 여기서
            # 계산한다 — 예전엔 그 숫자가 JS 에 박혀 있다가 전환점을 옮겼을 때
            # 어긋났다.
            "dsh": _n(t.get("dsh"), 0),
        }

    return {
        "base": payload["span"][0], "span": payload["span"], "nDays": payload["n_days"],
        "generated": payload["generated"],
        "bands": payload["bands"], "families": payload["families"],
        "bandStances": payload["band_stances"],
        "bandStancesEn": payload["band_stances_en"],
        "states": payload["states"], "statesEn": payload["states_en"],
        "indMeta": payload["indicator_meta"],
        "ladders": payload["ladders"], "dca": payload["dca"],
        "halvings": payload["halvings"],
        "tps": [tp(t) for t in payload["turning_points"]],
        "stats": payload["interval_stats"],
        "current": point(payload["current"]), "latest": point(payload["latest"]),
        "deltas": payload["deltas"], "lastSimilar": payload["last_similar"],
        "deriveNotes": payload["derive_notes"],
        "macro": payload.get("macro"),
        "rel": payload.get("relationship"),
        "S": rows,
    }


def _read(rel: str) -> str:
    p = ROOT / rel
    if not p.exists():
        raise SystemExit(f"조각을 찾을 수 없습니다: {p}")
    return p.read_text(encoding="utf-8")


PLACEHOLDERS = ("__INDEX_URL__", "__VERIFY_URL__", "__RULES_URL__",
                "__LIQUIDITY_URL__", "__RELATION_URL__",
                "__TITLE__", "__NAV__", "__PAGE__")


def _bake(body_rel: str, page_key: str, title: str, extra_script: Optional[str],
          data: str, out: Path, links: dict[str, str]) -> Path:
    # 머리띠는 전 장이 공유한다. 예전에는 본문마다 복제돼 있었고, 버튼을 하나
    # 붙이려면 모든 본문을 똑같이 고쳐야 했다.
    nav = _read(NAV).replace("__PAGE__", page_key)
    page = (
        _read(HEAD).replace("__TITLE__", title)
        + _read(body_rel).replace("__NAV__", nav)
        + f"\n<script>window.__BCS__ = {MARKER}{data}{MARKER};</script>\n"
        + _read(I18N)
        + _read(SCRIPT)
        + (_read(extra_script) if extra_script else "")
    )
    # 페이지들이 서로를 링크한다. 로컬에서는 파일 이름이면 되지만, 각각 다른
    # 주소로 올릴 때는 절대 주소여야 해서 빌드 시점에 갈아 끼운다.
    for k, v in links.items():
        page = page.replace(k, v)
    leftover = [w for w in PLACEHOLDERS if w in page]
    if leftover:
        raise SystemExit(f"{out.name}: 치환되지 않은 자리표시자 {leftover}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def build(csv: str, outdir: Path, *, config: Optional[str] = None,
          derive: bool = True, index_url: str = "index.html",
          verify_url: str = "verify.html", rules_url: str = "rules.html",
          liquidity_url: str = "liquidity.html",
          relation_url: str = "relationship.html",
          macro_csv: Optional[str] = None,
          info: Optional[dict] = None) -> list[Path]:
    """페이지를 굽고 경로를 돌려준다.

    ``info`` 를 주면 기준일·데이터 지연 같은 부수 정보를 그 딕셔너리에 담는다.
    반환형을 바꾸면 ``for p in build(...)`` 로 쓰는 호출부가 전부 깨지는데,
    커밋 메시지에 기준일을 넣으려고 그럴 이유는 없다.
    """
    cfg = load_config(config) if config else load_config()
    payload = export_viz.build(cfg, csv, derive=derive, macro_csv=macro_csv)
    data = json.dumps(compact(payload), ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)   # NaN/inf 는 무효 JSON → JSON.parse 백지. 여기서 막는다.
    links = {"__INDEX_URL__": index_url, "__VERIFY_URL__": verify_url,
             "__RULES_URL__": rules_url, "__LIQUIDITY_URL__": liquidity_url,
             "__RELATION_URL__": relation_url}
    paths = [_bake(body, key, title, extra, data, outdir / name, links)
             for body, name, key, title, extra in PAGES]
    if info is not None:
        info["as_of"] = payload["current"]["d"]
        info["latest"] = payload["latest"]["d"]
        info["age_days"] = payload["age_days"]
    return paths


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="자기완결 시각화 페이지 생성")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--macro", default="data/macro.csv",
                    help="거시 CSV (중국 M2 선행 차트·LRS). 없으면 그 섹션은 숨긴다")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out-dir", default="viz/site", help="페이지를 내보낼 디렉터리")
    ap.add_argument("--no-derive", action="store_true",
                    help="빠진 유통량·시총을 반감기 스케줄로 채우지 않는다")
    # 페이지들이 서로를 링크한다. 로컬 파일로 볼 때는 기본값(파일 이름)이면 되고,
    # 각각 다른 주소로 올릴 때만 절대 주소를 넘긴다.
    ap.add_argument("--index-url", default="index.html")
    ap.add_argument("--verify-url", default="verify.html")
    ap.add_argument("--rules-url", default="rules.html")
    ap.add_argument("--liquidity-url", default="liquidity.html")
    ap.add_argument("--relation-url", default="relationship.html")
    args = ap.parse_args(argv)

    info: dict = {}
    paths = build(args.csv, Path(args.out_dir), config=args.config,
                  derive=not args.no_derive, index_url=args.index_url,
                  verify_url=args.verify_url, rules_url=args.rules_url,
                  liquidity_url=args.liquidity_url,
                  relation_url=args.relation_url,
                  macro_csv=args.macro, info=info)
    for p in paths:
        kb = p.stat().st_size / 1024
        print(f"생성: {p}  ({kb:.0f} KB)")
        if kb > 400:
            print("  ! 400KB 를 넘었습니다 — export_viz.py 의 추리기 간격을 넓히세요")
    # 갱신 워크플로가 이 줄을 읽어 커밋 메시지에 쓴다. 형식을 바꾸면
    # .github/workflows/refresh-data.yml 의 sed 도 같이 고쳐야 한다.
    print(f"기준일: {info['as_of']}  (데이터 {info['age_days']}일 전)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
