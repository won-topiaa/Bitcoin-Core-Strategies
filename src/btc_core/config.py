"""strategy.yaml 로딩과 검증.

임계값은 코드가 아니라 설정에 산다. 여기서 하는 일은 읽고, 형태를 검사하고,
가중치 합처럼 스스로 어긋날 수 있는 값들을 미리 잡아내는 것뿐이다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML 이 필요합니다.  pip install -r requirements.txt"
    ) from exc


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "strategy.yaml"


class ConfigError(ValueError):
    """설정 파일이 스스로 모순될 때."""


@dataclass(frozen=True)
class StrategyConfig:
    raw: Mapping[str, Any]
    path: Path

    # -- 편의 접근자 --------------------------------------------------------
    @property
    def meta(self) -> Mapping[str, Any]:
        return self.raw.get("meta", {})

    @property
    def indicators(self) -> Mapping[str, Any]:
        return self.raw["indicators"]

    @property
    def bcs_families(self) -> Mapping[str, Any]:
        return self.raw["bcs"]["families"]

    @property
    def coverage(self) -> Mapping[str, Any]:
        return self.raw["bcs"].get("coverage", {})

    @property
    def lrs_components(self) -> Mapping[str, Any]:
        return self.raw["lrs"]["components"]

    @property
    def lrs_bands(self) -> list[Mapping[str, Any]]:
        return list(self.raw["lrs"]["bands"])

    @property
    def consensus(self) -> Mapping[str, Any]:
        return self.raw["consensus"]

    @property
    def bands(self) -> list[Mapping[str, Any]]:
        return list(self.raw["bands"])

    @property
    def ladders(self) -> Mapping[str, Any]:
        return self.raw["ladders"]

    @property
    def hysteresis(self) -> Mapping[str, Any]:
        return self.raw.get("hysteresis", {})

    @property
    def lrs_modifier(self) -> Mapping[str, Any]:
        return self.raw.get("lrs_modifier", {"enabled": False})

    @property
    def floors(self) -> Mapping[str, Any]:
        return self.raw.get("floors", {"lines": [], "buy_zones": []})

    @property
    def bottom_profile(self) -> Mapping[str, Any]:
        return self.raw.get("bottom_profile", {"enabled": False})

    @property
    def invalidation(self) -> list[Mapping[str, Any]]:
        return list(self.raw.get("invalidation", []))

    def indicator(self, key: str) -> Mapping[str, Any]:
        try:
            return self.indicators[key]
        except KeyError as exc:
            raise ConfigError(f"지표 {key!r} 가 config 에 없습니다.") from exc

    def band_for(self, bcs: float) -> Mapping[str, Any]:
        return _select_band(self.bands, bcs)

    def lrs_band_for(self, lrs: float) -> Mapping[str, Any]:
        return _select_band(self.lrs_bands, lrs)


def load_config(path: str | os.PathLike[str] | None = None) -> StrategyConfig:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"설정 파일 형식이 잘못됐습니다: {p}")
    cfg = StrategyConfig(raw=data, path=p)
    validate(cfg)
    return cfg


def validate(cfg: StrategyConfig) -> None:
    """설정이 스스로 모순되지 않는지 확인한다."""
    for section in ("indicators", "bcs", "lrs", "consensus", "bands", "ladders"):
        if section not in cfg.raw:
            raise ConfigError(f"필수 섹션 누락: {section}")

    # 1. 계열 가중치 합 = 100
    total = sum(float(f["weight"]) for f in cfg.bcs_families.values())
    if abs(total - 100.0) > 1e-6:
        raise ConfigError(f"BCS 계열 가중치 합이 100이 아닙니다: {total}")

    lrs_total = sum(float(c["weight"]) for c in cfg.lrs_components.values())
    if abs(lrs_total - 100.0) > 1e-6:
        raise ConfigError(f"LRS 구성요소 가중치 합이 100이 아닙니다: {lrs_total}")

    # 2. 계열 멤버가 실제로 존재하고, 지표의 family 선언과 일치하는가
    seen: set[str] = set()
    for fam_key, fam in cfg.bcs_families.items():
        members = list(fam.get("members", []))
        if not members:
            raise ConfigError(f"계열 {fam_key!r} 에 members 가 없습니다.")
        for m in members:
            if m not in cfg.indicators:
                raise ConfigError(f"계열 {fam_key!r} 의 멤버 {m!r} 가 indicators 에 없습니다.")
            declared = cfg.indicators[m].get("family")
            if declared != fam_key:
                raise ConfigError(
                    f"지표 {m!r} 의 family({declared!r})가 소속 계열({fam_key!r})과 다릅니다."
                )
            if m in seen:
                raise ConfigError(f"지표 {m!r} 가 두 계열에 중복 소속돼 있습니다.")
            seen.add(m)
        if fam.get("aggregate", "mean") not in {"mean", "max_abs", "min", "max"}:
            raise ConfigError(f"계열 {fam_key!r} 의 aggregate 값이 잘못됐습니다.")

    orphan = set(cfg.indicators) - seen
    if orphan:
        raise ConfigError(f"어떤 계열에도 속하지 않은 지표: {sorted(orphan)}")

    # 3. 밴드가 -100~100 을 빈틈없이 덮는가
    _check_band_cover(cfg.bands, "bands")
    _check_band_cover(cfg.lrs_bands, "lrs.bands")

    # 4. 매도 사다리 누적이 (100 - core_hold_pct) 를 넘지 않는가
    dist = cfg.ladders.get("distribute", {})
    core = float(dist.get("core_hold_pct", 0))
    cum = sum(float(s["pct_of_holdings"]) for s in dist.get("steps", []))
    if cum > 100.0 - core + 1e-9:
        raise ConfigError(
            f"매도 사다리 누적 {cum}% 가 매도 가능분 {100 - core}% 를 넘습니다."
        )

    # 5. 매수 사다리 누적이 예비현금 100% 를 넘지 않는가
    acc_cum = sum(float(s["pct_of_reserve"]) for s in cfg.ladders.get("accumulate", {}).get("steps", []))
    if acc_cum > 100.0 + 1e-9:
        raise ConfigError(f"매수 사다리 누적 {acc_cum}% 가 100% 를 넘습니다.")

    # 6. 사다리 트리거가 단조인가 (매도는 증가, 매수는 감소)
    d_trig = [float(s["trigger_bcs"]) for s in dist.get("steps", [])]
    if any(b <= a for a, b in zip(d_trig, d_trig[1:])):
        raise ConfigError(f"매도 사다리 트리거가 증가 순서가 아닙니다: {d_trig}")
    a_trig = [float(s["trigger_bcs"]) for s in cfg.ladders.get("accumulate", {}).get("steps", [])]
    if any(b >= a for a, b in zip(a_trig, a_trig[1:])):
        raise ConfigError(f"매수 사다리 트리거가 감소 순서가 아닙니다: {a_trig}")

    # 7. DCA 배수가 모든 밴드에 정의됐는가
    dca = cfg.ladders.get("dca_multiplier", {})
    for band in cfg.bands:
        if band["key"] not in dca:
            raise ConfigError(f"밴드 {band['key']!r} 에 dca_multiplier 가 없습니다.")

    # 8. 바닥선 가중치 합 = 1
    lines = cfg.floors.get("lines", [])
    if lines:
        fw = sum(float(l["weight"]) for l in lines)
        if abs(fw - 1.0) > 1e-6:
            raise ConfigError(f"바닥선 가중치 합이 1.0 이 아닙니다: {fw}")

    # 9. 앵커 유효성은 normalize 계층에서 검사하되, 여기서 한 번 훑는다
    from .normalize import _validate_anchors  # 지역 import: 순환 방지

    for key, spec in cfg.indicators.items():
        if spec.get("input_mode") == "categorical":
            if not spec.get("states"):
                raise ConfigError(f"지표 {key!r}: categorical 인데 states 가 없습니다.")
        else:
            _validate_anchors(spec.get("anchors", []), key)
    for key, spec in cfg.lrs_components.items():
        if spec.get("input_mode") == "categorical":
            if not spec.get("states"):
                raise ConfigError(f"LRS {key!r}: categorical 인데 states 가 없습니다.")
        else:
            _validate_anchors(spec.get("anchors", []), key)


def _select_band(bands: list[Mapping[str, Any]], value: float) -> Mapping[str, Any]:
    """점수가 속하는 밴드. 경계값은 항상 0에서 더 먼 쪽 밴드에 속한다.

    점수가 0을 중심으로 대칭이므로 밴드 배정도 대칭이어야 한다.
    +20 에서 상단 중립으로 들어간다면 −20 에서도 저평가로 들어가야 한다.
    구간을 단순히 [하한, 상한) 으로 잡으면 음수 쪽에서만 경계가 한 칸
    안쪽으로 밀려서, 임계값에 정확히 걸린 날 매수 신호가 사라진다.
    """
    ordered = sorted(bands, key=lambda b: float(b["min"]))
    if value <= float(ordered[0]["min"]):
        return ordered[0]
    if value >= float(ordered[-1]["max"]):
        return ordered[-1]

    for band in ordered:
        lo, hi = float(band["min"]), float(band["max"])
        if value >= 0:
            if lo <= value < hi:
                return band
        elif lo < value <= hi:
            return band
    # 밴드 커버리지는 validate 가 보장하므로 여기 오는 값은 NaN 뿐이다.
    # 조용히 첫 밴드를 돌려주면 그건 '항복'(전량 매수) 밴드다 — 최악의 오답.
    raise ConfigError(f"밴드를 정할 수 없는 점수입니다: {value!r}")


def _check_band_cover(bands: list[Mapping[str, Any]], name: str) -> None:
    if not bands:
        raise ConfigError(f"{name} 이 비어 있습니다.")
    ordered = sorted(bands, key=lambda b: float(b["min"]))
    if abs(float(ordered[0]["min"]) + 100.0) > 1e-9:
        raise ConfigError(f"{name}: 최하단 밴드가 -100 에서 시작해야 합니다.")
    if abs(float(ordered[-1]["max"]) - 100.0) > 1e-9:
        raise ConfigError(f"{name}: 최상단 밴드가 100 에서 끝나야 합니다.")
    for a, b in zip(ordered, ordered[1:]):
        if abs(float(a["max"]) - float(b["min"])) > 1e-9:
            raise ConfigError(
                f"{name}: 밴드 사이에 틈/겹침이 있습니다 ({a['key']} max={a['max']}, {b['key']} min={b['min']})"
            )
