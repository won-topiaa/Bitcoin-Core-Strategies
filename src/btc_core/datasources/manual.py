"""수동 입력 어댑터.

RHODL Ratio, Reserve Risk, LTH 실현가격, CVDD, 그리고 거시 지표 전부는
무료 API가 없다. BM Pro 차트를 눈으로 읽어 YAML 한 파일에 적는다.

이 지표들은 월 1~2회면 충분한 느린 지표라, 자동화하지 못하는 것이
실질적인 손해가 되지 않는다. 오히려 매달 한 번 직접 차트를 보게 되는
쪽이 원문 6.4가 말하는 '판단 기록'과 잘 맞는다.

값이 오래되면 경고를 띄운다. 3개월 지난 Reserve Risk 로 오늘을 판단하는
일이 조용히 벌어지지 않게 하기 위한 장치다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML 이 필요합니다.  pip install -r requirements.txt") from exc


STALE_AFTER_DAYS = 45      # 이보다 오래된 수동 입력은 경고
EXPIRE_AFTER_DAYS = 120    # 이보다 오래되면 아예 결측 처리


@dataclass(frozen=True)
class ManualInput:
    """수동으로 적어 넣은 값들."""

    as_of: Optional[date]
    indicators: Mapping[str, Any] = field(default_factory=dict)
    macro: Mapping[str, Any] = field(default_factory=dict)
    floors: Mapping[str, Any] = field(default_factory=dict)
    dated: Mapping[str, date] = field(default_factory=dict)   # 항목별 개별 관측일
    warnings: tuple[str, ...] = ()

    def age_days(self, key: str, reference: date) -> Optional[int]:
        observed = self.dated.get(key) or self.as_of
        return (reference - observed).days if observed else None


def load_manual(path: str | Path | None, *, reference: Optional[date] = None) -> ManualInput:
    """수동 입력 YAML 을 읽는다. 경로가 없으면 빈 입력."""
    if path is None:
        return ManualInput(as_of=None)
    p = Path(path)
    if not p.exists():
        return ManualInput(as_of=None, warnings=(f"수동 입력 파일 없음: {p}",))

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return ManualInput(as_of=None, warnings=(f"{p}: 최상위가 매핑이 아닙니다.",))

    as_of = _as_date(data.get("as_of"))
    ref = reference or date.today()
    warns: list[str] = []

    indicators = dict(data.get("indicators") or {})
    macro = dict(data.get("macro") or {})
    floors = dict(data.get("floors") or {})
    dated: dict[str, date] = {}

    # 항목별로 {value:, observed_on:} 형태도 허용한다
    for bucket in (indicators, macro, floors):
        for key, val in list(bucket.items()):
            if isinstance(val, dict) and "value" in val:
                observed = _as_date(val.get("observed_on"))
                if observed:
                    dated[key] = observed
                bucket[key] = val["value"]

    if as_of is None and not dated:
        warns.append(f"{p}: as_of 가 없습니다. 값의 신선도를 검증할 수 없습니다.")

    # 오래된 값 처리
    for bucket_name, bucket in (("indicators", indicators), ("macro", macro), ("floors", floors)):
        for key in list(bucket):
            observed = dated.get(key) or as_of
            if observed is None:
                continue
            age = (ref - observed).days
            if age >= EXPIRE_AFTER_DAYS:
                warns.append(f"{key}: 관측 {age}일 경과 — 만료로 간주해 결측 처리했습니다.")
                bucket[key] = None
            elif age >= STALE_AFTER_DAYS:
                warns.append(f"{key}: 관측 {age}일 경과 — 갱신을 권합니다.")

    return ManualInput(
        as_of=as_of,
        indicators=indicators,
        macro=macro,
        floors=floors,
        dated=dated,
        warnings=tuple(warns),
    )


def _as_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def write_template(path: str | Path) -> Path:
    """빈 수동 입력 파일을 만들어 준다."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TEMPLATE, encoding="utf-8")
    return p


TEMPLATE = """\
# 수동 입력 — BM Pro 차트를 보고 채운다. 월 1회면 충분하다.
# 값을 비워두면(또는 줄을 지우면) 해당 지표는 결측으로 처리되고
# 계열 가중치가 자동으로 재분배된다. 억지로 채우지 말 것.

as_of: 2026-07-28        # 차트를 확인한 날짜

indicators:
  # RHODL Ratio — /charts/rhodl-ratio/
  # 밴드 위치로 읽는다. 0 = 하단 초록 밴드 바닥, 1 = 상단 빨간 밴드 상단.
  # 로그 스케일 차트에서 눈대중으로 "위아래 밴드 사이 어디쯤인가"를 0~1로.
  rhodl: 0.45

  # Reserve Risk — /charts/reserve-risk/
  # 차트 y축 값을 그대로 (예: 0.0031)
  reserve_risk: 0.0031

  # 가격 ÷ LTH 실현가격 — /charts/long-term-holder-realized-price/
  # 현재가와 LTH 실현가격을 읽어 나눈 값 (예: 118000 / 74000 = 1.59)
  lth_mvrv: 1.59

macro:
  # 글로벌 M2 전년비 증가율(%) — /bitcoin-macro/m2-global-vs-btc/
  m2_impulse: 5.2

  # DXY 13주 변화율(%) — /bitcoin-portfolio/btc-vs-dxy/
  # 하락이면 음수. 부호 반전은 엔진이 알아서 한다.
  dxy_trend: -2.1

  # 연준 스탠스: qt_hiking | qt_hold | neutral | pause_easing | cutting_qe
  fed_stance: pause_easing

  # 현물 ETF 30일 누적 순유입 (십억 달러). 순유출이면 음수.
  etf_flow: 2.4

floors:
  # 바닥선 가격 (달러). ma200w 는 자동 계산되므로 비워둬도 된다.
  lth_rp: 74000        # LTH 실현가격
  cvdd: 41000          # /charts/cvdd/

# 항목별로 관측일을 따로 적고 싶으면 이렇게 쓸 수도 있다:
#   cvdd:
#     value: 41000
#     observed_on: 2026-07-01
"""
