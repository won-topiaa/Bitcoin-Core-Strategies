#!/usr/bin/env python3
"""시각화 페이지를 만든다 — 데이터를 페이지 안에 굽는다.

    python3 tools/build_viz.py --csv data/market.csv --out viz/bcs-gauge.html

페이지는 **자기완결이어야 한다.** 외부 요청이 하나라도 있으면 그 요청이 막힌
환경에서 빈 화면이 나오고, 무료 데이터로 돌아가는 시스템에서 그건 곧 "안 보인다"
와 같다. 그래서 CSS·JS·데이터를 전부 한 파일에 넣는다.

    viz/template.html   구조 + 스타일 + 차트 엔진 (데이터 자리만 비워 둠)
    tools/export_viz.py 하루씩 계산해 JSON 을 만든다
    이 파일              둘을 합쳐 하나의 HTML 로 굽는다

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

# 페이지는 둘이고 데이터는 하나다. 같은 payload 를 두 틀에 굽는다 —
# 규칙 페이지가 오래된 숫자를 보여주는 일이 없게.
PAGES = (
    ("viz/template.html", "index.html"),
    ("viz/rules.template.html", "rules.html"),
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
            "ind": c.get("ind", {}),
        }

    def tp(t: dict) -> dict:
        return {
            "phase": t["phase"], "d": t["d"], "price": _n(t["price"], 0),
            "bcs": _n(t["bcs"], 1), "low": _n(t["low"], 1), "high": _n(t["high"], 1),
            "stable": t["stable"], "band": t["band"],
        }

    return {
        "base": payload["span"][0], "span": payload["span"], "nDays": payload["n_days"],
        "bands": payload["bands"], "families": payload["families"],
        "bandStances": payload["band_stances"],
        "indMeta": payload["indicator_meta"],
        "ladders": payload["ladders"], "dca": payload["dca"],
        "halvings": payload["halvings"],
        "tps": [tp(t) for t in payload["turning_points"]],
        "stats": payload["interval_stats"],
        "current": point(payload["current"]), "latest": point(payload["latest"]),
        "deriveNotes": payload["derive_notes"],
        "S": rows,
    }


def _bake(tpl_path: Path, data: str, out: Path, links: dict[str, str]) -> Path:
    if not tpl_path.exists():
        raise SystemExit(f"틀을 찾을 수 없습니다: {tpl_path}")
    tpl = tpl_path.read_text(encoding="utf-8")
    a = tpl.find(MARKER)
    b = tpl.find(MARKER, a + len(MARKER))
    if a < 0 or b < 0:
        raise SystemExit(f"{tpl_path}: {MARKER} 자리표시자가 두 개 있어야 합니다.")
    page = tpl[:a] + MARKER + data + tpl[b:]
    # 두 페이지는 서로를 링크한다. 로컬에서는 파일 이름이면 되지만, 각각 다른
    # 주소로 올릴 때는 절대 주소여야 해서 빌드 시점에 갈아 끼운다.
    for k, v in links.items():
        page = page.replace(k, v)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def build(csv: str, outdir: Path, *, config: Optional[str] = None,
          derive: bool = True, index_url: str = "index.html",
          rules_url: str = "rules.html") -> list[Path]:
    cfg = load_config(config) if config else load_config()
    payload = export_viz.build(cfg, csv, derive=derive)
    data = json.dumps(compact(payload), ensure_ascii=False, separators=(",", ":"))
    links = {"__INDEX_URL__": index_url, "__RULES_URL__": rules_url}
    return [_bake(ROOT / tpl, data, outdir / name, links) for tpl, name in PAGES]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="자기완결 시각화 페이지 생성")
    ap.add_argument("--csv", default="data/market.csv")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out-dir", default="viz/site", help="페이지를 내보낼 디렉터리")
    ap.add_argument("--no-derive", action="store_true",
                    help="빠진 유통량·시총을 반감기 스케줄로 채우지 않는다")
    ap.add_argument("--index-url", default="index.html",
                    help="규칙 페이지에서 첫 페이지로 거는 링크 (호스팅 시 절대 주소)")
    ap.add_argument("--rules-url", default="rules.html",
                    help="첫 페이지에서 규칙 페이지로 거는 링크")
    args = ap.parse_args(argv)

    paths = build(args.csv, Path(args.out_dir), config=args.config,
                  derive=not args.no_derive,
                  index_url=args.index_url, rules_url=args.rules_url)
    for p in paths:
        kb = p.stat().st_size / 1024
        print(f"생성: {p}  ({kb:.0f} KB)")
        if kb > 400:
            print("  ! 400KB 를 넘었습니다 — export_viz.py 의 추리기 간격을 넓히세요")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
