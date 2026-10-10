"""거시 데이터 열별 신선도(tools/macro_freshness.py).

fetch_macro 는 열 단위 병합이라 소스가 영영 멈춰도 파일은 멀쩡해 보인다. 2026-10
감사에서 m2_cn(5월)·vix(9-22)·usdjpy(7-24)가 그렇게 멈춰 있었는데 아무도 몰랐다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import macro_freshness as mf  # noqa: E402


def write(path, rows, cols):
    lines = ["date," + ",".join(cols)]
    for d, vals in rows:
        lines.append(d + "," + ",".join("" if v is None else str(v) for v in vals))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_three_columns_the_audit_found_stalled_are_flagged(tmp_path, capsys):
    p = tmp_path / "m.csv"
    write(p, [("2026-05-01", [100.0, None, None, None]),
              ("2026-07-24", [None, None, 163.7, None]),
              ("2026-09-22", [None, 14.21, None, None]),
              ("2026-10-09", [None, None, None, 22000.0])],
          ["m2_cn", "vix", "usdjpy", "nasdaq"])
    assert mf.main([str(p), "--today", "2026-10-10"]) == 0, "경고만 하고 빌드는 멈추지 않는다"
    out = capsys.readouterr().out
    for col in ("m2_cn", "vix", "usdjpy"):
        assert f"::warning::거시 데이터가 멈췄습니다 — {col}:" in out, (col, out)
    assert "— nasdaq:" not in out
    assert mf.main([str(p), "--today", "2026-10-10", "--strict"]) == 1


def test_a_normal_publication_lag_is_not_flagged(tmp_path):
    """월간은 다음 달 중순에 나온다 — 10월 초에 8월 값이 마지막인 것은 정상이다."""
    p = tmp_path / "m.csv"
    write(p, [("2026-08-01", [100.0, 21000.0]), ("2026-10-08", [None, None])], ["m2_cn", "m2_us"])
    stale, _ = mf.judge(mf.last_dates(p), date(2026, 10, 10))
    assert not [s for s in stale if s.startswith(("m2_cn", "m2_us"))]


def test_every_column_in_the_real_file_is_classified():
    """새 열을 말없이 감시 밖에 두지 않는다 — WATCH 나 ENDED 중 하나에 넣어야 한다."""
    real = ROOT / "data" / "macro.csv"
    last = mf.last_dates(real)
    unknown = [c for c in last if c not in mf.WATCH and c not in mf.ENDED]
    assert not unknown, f"감시 목록에 없는 열: {unknown}"
    assert not (set(mf.WATCH) & mf.ENDED)


def test_every_column_fetch_macro_can_produce_is_classified():
    import fetch_macro as fm
    produced = set(fm.CORE.values()) | {f"m2_{cc}" for cc in fm.GLOBAL} | {"m2_global", "net_liq"}
    produced |= {col for _u, _d, cols in fm.GITHUB_SOURCES for col in cols.values()}
    produced |= {col for *_x, col in fm.CSV_API_SOURCES}
    produced = {c for c in produced if not c.startswith("_")}
    missing = sorted(c for c in produced if c not in mf.WATCH and c not in mf.ENDED)
    assert not missing, f"fetch_macro 가 만드는데 신선도 감시에 없는 열: {missing}"


def test_the_refresh_runs_the_check():
    import yaml
    doc = yaml.safe_load((ROOT / ".github/workflows/refresh-data.yml").read_text(encoding="utf-8"))
    steps = [s for j in doc["jobs"].values() for s in j["steps"]]
    assert any("tools/macro_freshness.py" in str(s.get("run", "")) for s in steps)
    i_macro = next(i for i, s in enumerate(steps) if "fetch_macro.py" in str(s.get("run", "")))
    i_check = next(i for i, s in enumerate(steps) if "macro_freshness.py" in str(s.get("run", "")))
    assert i_macro < i_check, "받기 전에 재면 지난 실행의 파일을 잽니다"
