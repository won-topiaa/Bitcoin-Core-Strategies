#!/usr/bin/env python3
"""중국 M2(m2_cn) 수집 — 공식 원본을 차례로 묻고, 검증을 통과한 것만 쓴다.

## 왜 바꿨나

예전 원본 chinadata.live 는 2026-05 에서 멈췄고(10월까지 다섯 달), 그동안 화면은
그 값을 '지금 임펄스'라 불렀다. 대안을 비교 검증했다(2026-10-12):

    원본                      최신     조건                            채택
    ─────────────────────────────────────────────────────────────────────────
    인민은행(PBoC) 통계표      2026-08  공식 통계, 발표 기관 그 자체     1순위
    국가통계국(NBS) 데이터 API  2026-08  출처 표기 시 전재 허용(이용약관)   2순위
    OECD 통화총량(SDMX)        2026-07  CC BY 4.0, 한 달 늦음             3순위(추가만)
    东方财富(Eastmoney) API     2026-08  법률성명 §八: 무단 복제·전파 금지  ✗
    TradingView(CNM2)          2026-08  이용약관 §3: 기계적 이용 금지      ✗

다섯 곳 모두 같은 숫자다 — 2024-01~2026-08 은 PBoC 표와 원 단위까지 같고, 저장된
chinadata 2015-01~2026-05 와는 최대 0.0011% 차이(chinadata 가 2026 년 값을 100亿
단위로 반올림해 둔 것)다. 그래서 고르는 기준은 숫자가 아니라 **쓸 수 있는가**다.
값이 같은 TradingView·Eastmoney 를 빼는 것은 그 약관 때문이다 — 이 저장소는 공개
저장소이고, 받은 값을 커밋하고 사이트에 싣는다.

## 원본별

  PBoC  https://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html
        왼쪽 메뉴 'YYYY年统计数据' → '货币统计概览' → '货币供应量' 행의 htm 표.
        파일 이름이 발표마다 바뀌는 타임스탬프라 매번 따라간다. 표 하나가 그 해
        1~12월이라 올해와 작년 표를 본다. 인코딩은 표마다 다르다(gb2312·utf-8 —
        2017·2018 표는 utf-8) — 선언된 charset 을 따른다.
  NBS   POST https://data.stats.gov.cn/dg/website/publicrelease/web/external/stream/esData
        한 번에 전 구간. 미래 달은 value "" 로 온다. 연결이 자주 끊겨 몇 번 다시 묻는다.
        일부 달은 보도자료처럼 100亿 단위로 반올림돼 있다(2026-02).
  OECD  https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_MONAGG,4.0/CHN.M.MABM.XDC._Z.N._Z._Z.N
        백만 위안(UNIT_MULT=6) → /100 으로 亿元. 한 달 늦고, 2017 년은 인민은행이
        2018 년에 재작성하기 **전** 값이다(0.4~0.8% 낮다). 그래서 **저장된 마지막 달
        뒤의 새 달만** 보탠다 — 기존 값을 덮지 않는다.

## 지키는 것 (2026-10 적대적 검토가 요구한 것)

  · 단위: 2015 년 이후 값은 100만~800만 亿元 안이어야 한다(M1·M0·위안·万亿 혼동 차단)
  · 월간 변화 |MoM| < 5%, 2015-01 이후 달 빠짐 없음(전 구간 원본만)
  · 개정 가드: 저장된 값과 겹치는 달 중 **13개월보다 오래된 달**이 0.05% 넘게 바뀌면
    그 원본 전체를 거부하고 다음 원본으로 넘어간다(잘못된 응답 하나가 24개월 보정에
    쓰이는 과거를 통째로 다시 쓰지 못하게)
  · 후퇴 금지: 새 마지막 달이 저장된 마지막 달보다 이르면 거부
  · 값은 소수 둘째 자리 — 인민은행 표와 같게
  · 매시간 도는 갱신이 원본을 두드리지 않도록, 저장된 값이 이미 '나왔을 수 있는
    가장 새 달'이면 묻지 않는다(인민은행은 다음 달 10~15일께 발표)

    python3 tools/china_m2.py                  # 지금 각 원본에 물어 결과만 보인다(쓰지 않는다)
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
# `import http.client` 는 안 된다 — 이 모듈의 http() 함수가 그 이름을 가린다.
from http.client import HTTPException
from pathlib import Path
from typing import Callable, Optional

START = date(2015, 1, 1)            # 저장 이력의 시작 — 24개월 임펄스 보정 구간을 바꾸지 않는다
UA = "curl/8.0"
TIMEOUT = 40

PBOC_ROOT = "https://www.pbc.gov.cn"
PBOC_INDEX = PBOC_ROOT + "/diaochatongjisi/116219/116319/index.html"
NBS_URL = ("https://data.stats.gov.cn/dg/website/publicrelease/web/external/stream/esData")
NBS_M2_ID = "f3c0ae453a54424489af41de315ec592"      # 货币和准货币(M2)供应量_期末值(亿元)
OECD_URL = ("https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_MONAGG,4.0/"
            "CHN.M.MABM.XDC._Z.N._Z._Z.N?startPeriod={start}&format=csv")

LEVEL_MIN, LEVEL_MAX = 1.0e6, 8.0e6   # 亿元. 2015-01 ≈ 124万, 2026-08 ≈ 357만
MAX_MOM = 0.05
REVISION_WINDOW_MONTHS = 13
MAX_OLD_REVISION = 0.0005             # 0.05%
RELEASE_DAY = 9                       # 이 날부터 지난달 값이 나왔을 수 있다고 본다

Series = dict[date, float]
Getter = Callable[..., bytes]


class SourceError(Exception):
    """원본이 응답했지만 쓸 수 없는 모양이다(구조 변경·차단 페이지 등)."""


# --------------------------------------------------------------------------
# 네트워크
# --------------------------------------------------------------------------
def http(url: str, *, data: Optional[bytes] = None, headers: Optional[dict] = None,
         timeout: int = TIMEOUT) -> bytes:
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _month_add(d: date, n: int) -> date:
    m = d.year * 12 + (d.month - 1) + n
    return date(m // 12, m % 12 + 1, 1)


def expected_latest(today: date) -> date:
    """오늘 나와 있을 수 있는 가장 새 달. 9일부터는 지난달, 그 전엔 지지난달."""
    return _month_add(date(today.year, today.month, 1), -1 if today.day >= RELEASE_DAY else -2)


# --------------------------------------------------------------------------
# PBoC
# --------------------------------------------------------------------------
def _decode(raw: bytes) -> str:
    m = re.search(rb'charset\s*=\s*["\']?([\w-]+)', raw[:4096], re.I)
    enc = m.group(1).decode("ascii", "replace").lower() if m else "utf-8"
    if enc in ("gb2312", "gbk", "gb18030"):
        enc = "gb18030"
    try:
        return raw.decode(enc)
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _check_not_challenge(text: str, where: str) -> None:
    low = text.lower()
    if "wzwschallenge" in low or "wzwsrel" in low or "urlacl" in low:
        raise SourceError(f"{where}: 봇 확인(WZWS) 페이지가 왔습니다")


def pboc_year_pages(index_html: str) -> dict[int, str]:
    """통계 목록에서 'YYYY年统计数据' 링크들."""
    out = {}
    for href, year in re.findall(r"href=['\"]([^'\"]+)['\"][^>]*>\s*(\d{4})年统计数据", index_html):
        out[int(year)] = href
    return out


def pboc_overview_link(year_html: str) -> Optional[str]:
    m = re.search(r"href=['\"]([^'\"]+)['\"][^>]*>\s*货币统计概览", year_html)
    return m.group(1) if m else None


def pboc_supply_table_link(overview_html: str) -> Optional[str]:
    """'货币供应量' 행의 htm 링크. 같은 페이지의 다른 표(汇率报表 등)를 집지 않는다."""
    i = overview_html.find("货币供应量")
    if i < 0:
        return None
    # 그 행이 든 표 안에서만 찾는다 — 그 행에 htm 이 없으면 다음 표(汇率报表)의 것을
    # 집게 된다.
    end = overview_html.find("</table>", i)
    m = re.search(r"href=['\"]([^'\"]+\.htm)['\"]", overview_html[i:end if end > 0 else i + 1500])
    return m.group(1) if m else None


def _cells(row_html: str) -> list[str]:
    out = []
    for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I):
        c = html.unescape(re.sub(r"<[^>]+>", " ", c)).replace("\u3000", " ")
        out.append(re.sub(r"\s+", " ", c).strip())
    return out


def parse_pboc_table(text: str) -> Series:
    """货币供应量 표 → {달: M2(亿元)}.

    **행과 칸 위치로** 짝짓는다. 머리행(YYYY.MM 이 있는 행)의 j 번째 칸의 달에 M2 행의 j
    번째 칸 값을 붙인다. 예전처럼 빈 칸을 버리고 순서로 짝지으면, 중간 달 하나가 비었을
    때 뒤의 값이 전부 한 달씩 밀리고 개정 가드(최근 13개월은 안 본다)도 못 잡는다.
    """
    rows = [_cells(r) for r in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I)]
    header = next((r for r in rows if sum(bool(re.fullmatch(r"\d{4}\.\d{1,2}", c)) for c in r) >= 2), None)
    m2 = next((r for r in rows if r and r[0].startswith("货币和准货币")), None)
    if m2 is None:
        raise SourceError("PBoC: '货币和准货币（M2）' 행이 없습니다")
    if header is None:
        raise SourceError("PBoC: 월 머리행(YYYY.MM)이 없습니다")
    out: Series = {}
    for j, h in enumerate(header):
        mm = re.fullmatch(r"(\d{4})\.(\d{1,2})", h)
        if not mm or j >= len(m2):
            continue
        v = m2[j].replace(",", "")
        if re.fullmatch(r"-?\d+(\.\d+)?", v):
            out[date(int(mm.group(1)), int(mm.group(2)), 1)] = round(float(v), 2)
    if not out:
        raise SourceError("PBoC: M2 행에 값이 없습니다")
    return out


def fetch_pboc(today: date, get: Getter = http) -> Series:
    """올해·작년 표. 1월 초처럼 올해 표가 아직 비어 있어도 작년 표가 12월까지 준다."""
    index = _decode(get(PBOC_INDEX))
    _check_not_challenge(index, "PBoC 목록")
    years = pboc_year_pages(index)
    want = [y for y in (today.year, today.year - 1) if y in years]
    if not want:
        raise SourceError(f"PBoC: {today.year}·{today.year - 1} 년 통계 메뉴가 없습니다 ({sorted(years)[-3:]})")
    out: Series = {}
    errors = []
    for y in want:
        # 해마다 따로 받는다 — 한 해의 페이지 오류가 다른 해의 멀쩡한 표를 버리게 하지 않는다.
        try:
            year_html = _decode(get(PBOC_ROOT + years[y]))
            _check_not_challenge(year_html, f"PBoC {y} 년")
            ov = pboc_overview_link(year_html)
            if not ov:
                raise SourceError(f"{y} 년 페이지에 货币统计概览 링크가 없습니다")
            ov_html = _decode(get(PBOC_ROOT + ov))
            _check_not_challenge(ov_html, f"PBoC {y} 货币统计概览")
            link = pboc_supply_table_link(ov_html)
            if not link:
                raise SourceError(f"{y} 年 货币统计概览 에 货币供应量 htm 이 없습니다")
            table = _decode(get(PBOC_ROOT + link))
            _check_not_challenge(table, f"PBoC {y} 货币供应量")
            out.update(parse_pboc_table(table))
        except Exception as exc:  # noqa: BLE001 — 그 해만 버리고 다른 해는 지킨다
            errors.append(f"{y}: {type(exc).__name__}: {exc}")
    if not out:
        raise SourceError("PBoC: 货币供应量 표를 하나도 못 읽었습니다 — " + " / ".join(errors))
    return out


# --------------------------------------------------------------------------
# NBS
# --------------------------------------------------------------------------
def nbs_body(today: date) -> bytes:
    return json.dumps({
        "cid": "82130c6621a745cda3d64b090e733383",
        "indicatorIds": [NBS_M2_ID],
        "daCatalogId": "",
        "das": [{"text": "全国", "value": "000000000000"}],
        "showType": "1",
        "rootId": "fc982599aa684be7969d7b90b1bd0e84",
        "dts": [f"{START:%Y%m}MM-{today:%Y%m}MM"],
    }, ensure_ascii=False).encode("utf-8")


def parse_nbs(obj) -> Series:
    if not isinstance(obj, dict):
        raise SourceError(f"NBS: 응답이 객체가 아닙니다({type(obj).__name__})")
    if not obj.get("success") or not isinstance(obj.get("data"), list):
        raise SourceError(f"NBS: 실패 응답 ({obj.get('state')}, {obj.get('message')})")
    out: Series = {}
    for row in obj["data"]:
        if not isinstance(row, dict):
            raise SourceError("NBS: data 의 행이 객체가 아닙니다")
        m = re.fullmatch(r"(\d{4})(\d{2})MM", str(row.get("code", "")))
        if not m:
            continue
        for v in row.get("values") or []:
            if not isinstance(v, dict):
                raise SourceError("NBS: values 의 항목이 객체가 아닙니다")
            if v.get("_id") != NBS_M2_ID:
                continue
            if v.get("du_name") not in (None, "亿元"):
                raise SourceError(f"NBS: 단위가 亿元 이 아닙니다({v.get('du_name')})")
            raw = str(v.get("value") or "").replace(",", "")
            if raw:
                out[date(int(m.group(1)), int(m.group(2)), 1)] = round(float(raw), 2)
    return out


def fetch_nbs(today: date, get: Getter = http, tries: int = 3,
              sleep: Callable[[float], None] = time.sleep) -> Series:
    last: Exception = SourceError("NBS: 묻지 못했습니다")
    for i in range(tries):
        if i:
            sleep(5 * i)
        try:
            raw = get(NBS_URL, data=nbs_body(today), headers={"Content-Type": "application/json"})
            text = raw.decode("utf-8", errors="replace")
            _check_not_challenge(text, "NBS")
            return parse_nbs(json.loads(text))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError,
                HTTPException) as exc:
            # HTTPException: 본문이 중간에 끊기면(IncompleteRead) urllib 은 이것을 그대로 던진다.
            last = exc
    raise last


# --------------------------------------------------------------------------
# OECD
# --------------------------------------------------------------------------
def parse_oecd(text: str) -> Series:
    out: Series = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("REF_AREA") != "CHN" or row.get("MEASURE") != "MABM":
            continue
        try:
            y, mth = (int(x) for x in str(row["TIME_PERIOD"]).split("-")[:2])
            mult = int(row.get("UNIT_MULT") or 6)
            v = float(row["OBS_VALUE"]) * (10 ** mult) / 1e8      # → 亿元
        except (KeyError, ValueError):
            continue
        out[date(y, mth, 1)] = round(v, 2)
    if not out:
        raise SourceError("OECD: 중국 광의통화(MABM) 행이 없습니다")
    return out


def fetch_oecd(get: Getter = http, start: date = START) -> Series:
    return parse_oecd(get(OECD_URL.format(start=f"{start:%Y-%m}")).decode("utf-8", errors="replace"))


# --------------------------------------------------------------------------
# 검증
# --------------------------------------------------------------------------
def problems(new: Series, stored: Series, *, full_history: bool) -> list[str]:
    """이 원본의 값을 쓰면 안 되는 이유들(없으면 빈 목록)."""
    new = {d: v for d, v in new.items() if d >= START}
    if not new:
        return ["값이 하나도 없습니다"]
    out = []
    bad = [d for d, v in new.items() if not (LEVEL_MIN <= v <= LEVEL_MAX)]
    if bad:
        out.append(f"단위가 이상합니다 — {bad[0]:%Y-%m} = {new[bad[0]]:,.0f} (허용 {LEVEL_MIN:,.0f}~{LEVEL_MAX:,.0f} 亿元)")
    ds = sorted(new)
    # 월간 변화는 **저장값과의 이음매까지** 본다. 새 값끼리만 보면 M1(≈M2 의 1/3)을 통째로
    # 잘못 받아도 그 안에서는 매끄러워 단위 범위에도 걸리지 않는다.
    for b in ds:
        a = _month_add(b, -1)
        prev = new.get(a, stored.get(a))
        if prev and abs(new[b] / prev - 1) > MAX_MOM:
            out.append(f"월간 변화가 큽니다 — {a:%Y-%m}→{b:%Y-%m} {new[b] / prev - 1:+.1%}")
            break
    if full_history:
        gaps = [d for d in _months(START, ds[-1]) if d not in new]
        if gaps:
            out.append(f"달이 빠졌습니다 — {gaps[0]:%Y-%m} 등 {len(gaps)}개")
    if stored:
        if ds[-1] < max(stored):
            out.append(f"후퇴 — 새 마지막 달 {ds[-1]:%Y-%m} < 저장된 {max(stored):%Y-%m}")
        cutoff = _month_add(ds[-1], -REVISION_WINDOW_MONTHS)
        for d in ds:
            if d < cutoff and d in stored and stored[d]:
                rel = abs(new[d] / stored[d] - 1)
                if rel > MAX_OLD_REVISION:
                    out.append(f"과거 값이 바뀌었습니다 — {d:%Y-%m} 저장 {stored[d]:,.2f} → {new[d]:,.2f} ({rel:.2%})")
                    break
    return out


def _months(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        out.append(d)
        d = _month_add(d, 1)
    return out


# --------------------------------------------------------------------------
# 고르기
# --------------------------------------------------------------------------
def continuity(new: Series, stored: Series) -> list[str]:
    """일부 구간만 주는 원본(인민은행 2년치·OECD 새 달)이 저장값과 **이어지는가**.

    이어지지 않으면 달은 최신인데 24개월 임펄스는 몇 해 동안 None 이 된다 — 화면은
    '최신'이라 하면서 읽기 줄만 조용히 빈다. 그런 값은 들이지 않고 전 구간 원본을 묻는다.
    """
    if not stored:
        return ["저장된 이력이 없어 일부 구간 원본으로는 채울 수 없습니다 — 전 구간 원본(국가통계국)이 필요합니다"]
    nxt = _month_add(max(stored), 1)
    if min(new) > nxt:
        return [f"저장값과 이어지지 않습니다 — {nxt:%Y-%m} ~ {_month_add(min(new), -1):%Y-%m} 이 빕니다"]
    holes = [d for d in _months(START, max(stored)) if d not in stored and d not in new]
    if holes:
        return [f"저장 이력에 빈 달이 있습니다 — {holes[0]:%Y-%m} 등 {len(holes)}개"]
    return []


ALL_FAILED = "::warning::중국 M2 를 어느 원본(인민은행·국가통계국·OECD)에서도 받지 못했습니다 — 저장된 값을 그대로 둡니다."


def collect(stored: Series, today: date, *, get: Getter = http, log=print,
            sleep: Callable[[float], None] = time.sleep, force: bool = False) -> tuple[Series, str]:
    """(m2_cn 에 병합할 값, 원본 이름). 쓸 것이 없으면 ({}, 이유).

    병합은 fetch_macro.merge_to_csv 가 한다 — 이번에 준 날짜만 덮고 나머지는 저장값을
    지킨다. 그래서 여기서 돌려주는 값이 곧 '덮어쓸 범위'다.

    어떤 원본이 검증은 통과했지만 **새 달이 없으면** 성공으로 치지 않고 다음 원본도
    묻는다. 인민은행 표가 낡은 파일을 계속 가리키면 그것을 '성공'으로 받아 국가통계국은
    영영 묻지 않게 되는데, 그게 chinadata 가 다섯 달 멈춘 것과 같은 꼴이다.
    """
    this_month = date(today.year, today.month, 1)
    future = sorted(d for d in stored if d > this_month)
    if future:
        # 미래 날짜 한 칸이 '이미 최신'으로 읽혀 영영 묻지 않게 되는 것을 막는다.
        log(f"::warning::중국 M2 저장값에 미래 달이 있습니다({future[0]:%Y-%m}) — 무시하고 판정합니다")
    stored = {d: v for d, v in stored.items() if v is not None and d <= this_month}
    want = expected_latest(today)
    base = max(stored) if stored else None
    if base and base >= want and not force:
        return {}, f"이미 최신({base:%Y-%m}) — 다음 발표 전이라 묻지 않습니다"

    candidate: Optional[tuple[Series, str]] = None   # 통과했지만 새 달이 없는 결과(개정분은 쓴다)
    attempts = (
        ("PBoC", lambda: fetch_pboc(today, get), False),
        ("NBS", lambda: fetch_nbs(today, get, sleep=sleep), True),
    )
    for name, fetch, full in attempts:
        try:
            got = fetch()
        except Exception as exc:  # noqa: BLE001 — 한 원본의 어떤 실패도 다음 원본을 막지 않는다
            log(f"  ! 중국 M2 {name}: {type(exc).__name__}: {exc}")
            continue
        got = {d: v for d, v in got.items() if d >= START}
        why = problems(got, stored, full_history=full)
        if not why and not full:
            why = continuity(got, stored)
        if why:
            log(f"  ! 중국 M2 {name}: 거부 — " + "; ".join(why))
            continue
        if base and max(got) <= base and not force:
            log(f"  중국 M2 {name}: {max(got):%Y-%m} 까지뿐 — 새 달이 없어 다음 원본도 묻습니다")
            candidate = candidate or (got, name)
            continue
        log(f"  중국 M2 ← {name}: {len(got)}개월 ({min(got):%Y-%m} ~ {max(got):%Y-%m})")
        return got, name

    # 마지막: OECD — 한 달 늦고 2017 년이 재작성 전 값이라 **저장값 뒤의 새 달만** 보탠다.
    # 저장 이력이 없으면 쓰지 않는다(재작성 전 2017 년으로 과거를 채우게 된다).
    try:
        oecd = fetch_oecd(get)
    except Exception as exc:  # noqa: BLE001
        log(f"  ! 중국 M2 OECD: {type(exc).__name__}: {exc}")
        oecd = None
    if oecd is not None and base:
        newer = {d: v for d, v in oecd.items() if d > base}
        if newer:
            why = problems(newer, stored, full_history=False) or continuity(newer, stored)
            if why:
                log("  ! 중국 M2 OECD: 거부 — " + "; ".join(why))
            else:
                log(f"  중국 M2 ← OECD(새 달만): {len(newer)}개월 ({min(newer):%Y-%m} ~ {max(newer):%Y-%m})")
                return newer, "OECD"
    if candidate:
        got, name = candidate
        if today.day >= 20:
            # 인민은행은 보통 10~15일에 낸다. 20일이 넘도록 어느 원본도 새 달이 없으면 알린다.
            log(f"::warning::중국 M2 가 {base:%Y-%m} 에서 나아가지 않습니다 — {want:%Y-%m} 이 나왔어야 할 때입니다")
        return got, f"{name}(새 달 없음)"
    log(ALL_FAILED)
    return {}, "모든 원본 실패"


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="중국 M2 원본을 지금 물어 결과만 보인다(쓰지 않는다)")
    ap.add_argument("--macro", default="data/macro.csv")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fetch_macro import _read_existing
    stored = _read_existing(Path(a.macro)).get("m2_cn", {})
    got, src = collect(stored, date.today(), force=True)
    if not got:
        print(f"쓸 값 없음: {src}")
        return 1
    for d in sorted(got)[-6:]:
        old = stored.get(d)
        print(f"  {d:%Y-%m}  {got[d]:>14,.2f}" + (f"   (저장 {old:,.2f})" if old else "   (새 달)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
