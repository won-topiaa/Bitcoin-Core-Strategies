#!/usr/bin/env python3
"""비트코인 핵심 뉴스 수집 → data/news.json — 뉴스 페이지(06)의 데이터원.

    python3 tools/fetch_news.py                    # 피드 수집 → 선별 → 병합 저장
    python3 tools/fetch_news.py --out data/news.json

## 무엇을 하나

네 개 RSS/Atom 피드(CoinDesk·Cointelegraph·Bitcoin Magazine·Decrypt)에서 기사를
받고, 전부 죽었을 때만 CryptoCompare JSON API 로 폴백한다. "모든 기사"가 아니라
**핵심 기사**만 남긴다 — 키워드 가중 점수 × 최신성 감쇠 × 소스 다양성 감점으로
줄 세우고, 제목이 거의 같은 기사(자카드 0.6↑)는 하나만 남긴 뒤 상위 20건을 뽑는다.

## 위생 처리 — 이 텍스트는 페이지에 구워진다

제목·요약·URL 은 **남이 쓴 텍스트**다(tests/test_newspage.py 의 전제와 같다).
그래서 수집 단계에서 먼저 벗긴다: HTML 태그 제거와 엔티티 해제를 고정점까지
반복하고(한 번만 하면 "&lt;script&gt;" 가 해제 후 태그로 되살아난다), 제어문자를
지우고, 요약은 280자에서 자른다. URL 은 http/https 만 — javascript: 같은 스킴이면
항목째 버린다. 최종 방어(</  이스케이프)는 inject_news.news_js 가 한 번 더 한다.

## 병합 철학 — refresh_data 와 같다: 잘못 잇지 않는 것

뉴스는 하루 몇 번씩 갱신되고 피드는 최근 수십 건만 준다. 이번 run 결과로 통째로
덮어쓰면 피드 창을 벗어난 어제 기사가 사라진다. 그래서 기존 파일의 7일 이내
항목을 살려 **매 run 전체를 다시 점수 매기고**(오래된 기사는 감쇠로 자연히
가라앉는다) 상위 40건으로 자른다. 쓰기는 임시 파일 → Path.replace 로 원자적.
전 피드 실패면 기존 파일을 건드리지 않고 종료코드 1 — 마지막 성공본이 남는다.

## 결정론

같은 입력(피드 응답·기존 파일·now)이면 같은 바이트가 나온다. 정렬 키는
score 내림차순 → published 내림차순 → url. 점수는 매 run 원본 텍스트에서
다시 계산하므로(저장된 score 는 표시용) 두 번 돌려도 파일이 흔들리지 않는다.

## 이 환경의 함정

이 에이전트 환경의 프록시는 뉴스 호스트를 전부 403 으로 막는다 — 실행 검증은
픽스처 주입(tests/test_news.py)으로만 한다. GitHub Actions 러너는 egress 가 열려
있어 거기서 실제로 돈다. UA 를 curl/8.0 으로 보내는 이유는 fetch_macro 와 같다
(낯선 UA 는 WAF 가 본문 전송을 멈춰 세운다). 정책 403 도 fetch_macro 처럼 두
모습(직접 403 HTTPError, CONNECT 터널 거절 URLError)을 구분해 보고한다.
"""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

UA = "curl/8.0"             # fetch_macro 와 같은 이유 — 낯선 UA 는 WAF 가 세운다
TIMEOUT = 30                # 피드 하나가 느려도 전체 run 이 5분을 넘지 않게

RSS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/feed"),
    ("Decrypt", "https://decrypt.co/feed"),
]
# 폴백 — RSS 가 전부 죽었을 때만 부른다(평시 호출·의존을 줄인다). JSON 응답이고
# 기사마다 원 매체 이름(source_info.name)이 있어 소스 다양성 감점이 그대로 먹는다.
FALLBACK = ("CryptoCompare", "https://min-api.cryptocompare.com/data/v2/news/?lang=EN")

TOP_NEW = 20                # 이번 run 에서 새로 올리는 최대 건수
CAP = 40                    # 병합 후 파일 전체 상한 (페이지가 무한히 길어지지 않게)
KEEP_DAYS = 7               # 기존 항목 유지 기한 — 이보다 오래되면 병합에서 제외
HALF_LIFE_HOURS = 48.0      # 최신성 반감기 — 이틀 지난 기사는 가중치가 절반
SOURCE_DECAY = 0.8          # 같은 소스 k번째 항목은 ×0.8^k — 한 매체 쏠림 완화
JACCARD_DUP = 0.6           # 제목 토큰 자카드가 이 이상이면 같은 기사로 본다
SUMMARY_LIMIT = 280         # 요약 절단 길이 — 카드 한 장 분량

# ── 핵심 기사 선별: 키워드 가중치 ────────────────────────────────────────────
# 튜닝 근거: '핵심 기사'의 정답 레이블이 없어 데이터로 적합(fit)하지 않았다.
# 대신 이 저장소가 사이클 위치를 읽을 때 실제로 쓰는 축과 맞춰 세 단계로만
# 나눴다 — 구조 변화(2.0) > 정책·기관(1.5) > 시장 서사(1.0 이하). 소수점을 더
# 정밀하게 조정하는 것은 근거 없는 과적합 흉내라 하지 않는다.
#   - bitcoin/btc 는 '필수급': 본문에 없으면 총점 ×0.1 (NON_BTC_MULT). 비트코인
#     사이클 도구의 뉴스 페이지에 알트코인 단독 기사가 올라오는 것을 막되,
#     하드 필터는 아니라서 달리 올릴 게 없으면 그래도 올라온다.
#   - \b 경계 필수: sec ⊂ "second", fed ⊂ "federal" 오탐을 막는다.
#   - strategy 는 사명 변경(MicroStrategy→Strategy) 때문에 넣지만 일반명사
#     오탐("trading strategy")이 잦아 가장 낮은 가중으로 둔다.
KEYWORDS: list[tuple[str, float]] = [
    (r"\betfs?\b", 2.0),                       # 현물 ETF — 2024~ 사이클 최대 구조 변화
    (r"\bhalving\b|\bhalvening\b", 2.0),       # 반감기 — 이 도구의 사이클 축 그 자체
    (r"all[- ]time high|\bath\b", 1.8),        # 신고가 — 사이클 후반 신호
    (r"\bcrash\b|\bplunge[sd]?\b|\bcapitulat", 1.8),   # 폭락·항복 — 사이클 바닥 신호
    (r"\bfed\b|\bfomc\b|federal reserve|rate cut|rate hike", 1.5),  # 연준 — 유동성 국면
    (r"\bsec\b", 1.5),                         # SEC — 승인·소송이 구조 변화를 만든다
    (r"\btreasur(?:y|ies)\b", 1.5),            # 기업·국가 트레저리 매집 — 신규 수요 주체
    (r"\bregulat(?:ion|ory|or|e[sd]?)\b|\blegislat", 1.2),  # 규제 일반
    (r"\bmicrostrategy\b|\bsaylor\b", 1.2),    # 최대 기업 보유자 — 트레저리 서사 대표
    (r"\bstrategy\b", 0.6),                    # 사명 'Strategy' — 일반명사 오탐 잦음
    (r"\bminers?\b|\bmining\b|\bhashrate\b", 1.0),      # 채굴 — 공급 측
    (r"\bhack(?:ed|ing)?\b|\bexploit\b", 1.0),          # 해킹 — 단기 충격 이벤트
    (r"\bwhales?\b|\bon-?chain\b", 0.8),       # 온체인·고래 — 참고 신호
    (r"\brally\b|\bsurge[sd]?\b|\bsoar", 0.8),          # 급등 서사 — 흔해서 낮게
]
NON_BTC_MULT = 0.1
_KEYWORD_RE = [(re.compile(p), w) for p, w in KEYWORDS]
_BTC_RE = re.compile(r"\bbitcoin\b|\bbtc\b")

Fetch = Callable[[str, int], str]


# --------------------------------------------------------------------------
# 받기
# --------------------------------------------------------------------------
def fetch_url(url: str, timeout: int = TIMEOUT) -> str:
    """URL 하나를 문자열로. 실패는 urllib 예외 그대로 — 분류는 _describe 가 한다."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _describe(exc: BaseException) -> str:
    """실패 사유 한 줄. 정책 차단(403/407)은 따로 표시한다 — fetch_macro 와 같은
    구분: 프록시가 직접 403(HTTPError.code), 또는 CONNECT 터널을 403 으로 거절
    (URLError, reason 안에 "403"). 이 표시가 sources_failed 로 페이지까지 간다."""
    code = getattr(exc, "code", None)
    if code in (403, 407) or "403" in str(getattr(exc, "reason", "")):
        return "정책 차단(403/407)"
    return str(exc) or type(exc).__name__


def _local(tag: object) -> str:
    """네임스페이스를 벗긴 로컬 태그명. '{ns}item' → 'item'. 주석 노드 등
    태그가 문자열이 아니면 빈 문자열(매칭 안 됨)."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_when(raw: Optional[str]) -> Optional[datetime]:
    """RFC 2822(pubDate) 먼저, 안 되면 ISO 8601(published/updated). 못 읽으면 None.
    시간대 없는 값은 UTC 로 본다 — 감쇠·만료 계산에 시간대가 꼭 필요하다."""
    raw = (raw or "").strip()
    if not raw:
        return None
    dt: Optional[datetime] = None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_feed_xml(text: str, source: str) -> list[dict]:
    """RSS 2.0 과 Atom 을 한 파서로. 네임스페이스는 로컬명 매칭으로 흡수한다.

    RSS: <item><title/><link>텍스트</link><description/><pubDate/>
    Atom: <entry><title/><link href rel="alternate"/><summary|content/><published|updated/>
    파싱 불능(ET.ParseError)은 호출부가 잡아 그 피드만 실패 처리한다."""
    root = ET.fromstring(text)
    out: list[dict] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = link = summary = stamp = None
        for child in node:
            name = _local(child.tag)
            if name == "title":
                title = child.text or ""
            elif name == "link":
                href = (child.get("href") or "").strip()
                if href:                              # Atom — rel="alternate" 우선
                    rel = child.get("rel") or "alternate"
                    if rel == "alternate" or link is None:
                        link = href
                elif child.text and child.text.strip():   # RSS — 텍스트가 URL
                    link = child.text.strip()
            elif name in ("description", "summary"):
                summary = child.text or ""
            elif name == "content" and summary is None:   # Atom — summary 없을 때만
                summary = child.text or ""
            elif name in ("pubDate", "published", "date"):
                stamp = child.text
            elif name == "updated" and stamp is None:     # published 가 있으면 그쪽 우선
                stamp = child.text
        out.append({"title": title or "", "url": link or "",
                    "summary": summary or "", "published": parse_when(stamp),
                    "source": source})
    return out


def parse_cryptocompare(text: str) -> list[dict]:
    """폴백 JSON API. Data[] 의 title/url/body/published_on(unix)/source_info.name.
    모양이 다른 항목은 조용히 건너뛴다 — 폴백까지 온 상황에서 더 까다롭게 굴지 않는다."""
    data = json.loads(text)
    rows = data.get("Data") if isinstance(data, dict) else None
    out: list[dict] = []
    for e in rows or []:
        if not isinstance(e, dict):
            continue
        ts = e.get("published_on")
        if not isinstance(ts, (int, float)):
            continue
        si = e.get("source_info")
        source = (si.get("name") if isinstance(si, dict) else None) \
            or e.get("source") or "CryptoCompare"
        out.append({"title": e.get("title") or "", "url": e.get("url") or "",
                    "summary": e.get("body") or "",
                    "published": datetime.fromtimestamp(ts, tz=timezone.utc),
                    "source": str(source)})
    return out


def collect(fetch: Fetch) -> tuple[list[dict], list[str], list[str]]:
    """모든 피드에서 원시 항목을 모은다 → (항목들, sources_ok, sources_failed).

    피드 하나의 실패가 전체를 죽이지 않는다 — 실패는 sources_failed 에 사유와
    함께 남고(정책 403 은 구분 표시) 나머지 피드로 계속 간다."""
    raw: list[dict] = []
    ok: list[str] = []
    failed: list[str] = []
    for name, url in RSS_FEEDS:
        try:
            text = fetch(url, TIMEOUT)
        except (OSError, ValueError) as exc:    # urllib 예외는 전부 OSError 계열
            failed.append(f"{name}: {_describe(exc)}")
            continue
        try:
            items = parse_feed_xml(text, name)
        except ET.ParseError as exc:
            failed.append(f"{name}: XML 파싱 실패 — {exc}")
            continue
        ok.append(name)
        raw.extend(items)
        print(f"  {name:<16} {len(items)}건")
    if not raw:                                 # RSS 전멸 — 그때만 폴백
        name, url = FALLBACK
        try:
            items = parse_cryptocompare(fetch(url, TIMEOUT))
        except (OSError, ValueError) as exc:    # JSONDecodeError ⊂ ValueError
            failed.append(f"{name}: {_describe(exc)}")
        else:
            ok.append(name)
            raw.extend(items)
            print(f"  {name:<16} {len(items)}건 (폴백)")
    return raw, ok, failed


# --------------------------------------------------------------------------
# 위생 처리 — 이 텍스트가 페이지에 구워진다
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^<>]*>")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # \t\n\r 는 아래서 공백 정리
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def clean_text(raw: str) -> str:
    """HTML 태그 제거 + 엔티티 해제를 고정점까지 반복 → 제어문자 제거 → 공백 정리.

    반복하는 이유: 한 번만 벗기면 "&lt;script&gt;" 가 unescape 후 태그로
    되살아난다. 두 겹 인코딩까지 감안해 고정점(더 안 바뀔 때)까지 돈다."""
    text = raw or ""
    for _ in range(5):                          # 5겹 인코딩은 현실에 없다 — 안전 상한
        step = html.unescape(_TAG_RE.sub(" ", text))
        if step == text:
            break
        text = step
    return " ".join(_CTRL_RE.sub("", text).split())


def _truncate(text: str, limit: int = SUMMARY_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def valid_url(raw: str) -> Optional[str]:
    """http/https 이고 호스트가 있는 URL 만. 그 외(javascript:·data:·상대경로)는
    None — 호출부가 항목째 버린다. 링크는 위생 처리로 '고칠' 수 없다."""
    # \t\n\r 까지 지운다 — urlsplit 은 스킴 검사에서 이 셋을 무시하므로, 남겨
    # 두면 '검사한 문자열'과 '저장한 문자열'이 달라진다(서버는 통과·클라이언트는
    # 거부하는 불일치 — 적대적 검증의 지적).
    url = re.sub(r"[\x00-\x1f\x7f]", "", (raw or "").strip())
    try:
        parts = urllib.parse.urlsplit(url)      # scheme 은 소문자로 정규화된다
    except ValueError:
        return None
    if parts.scheme in ("http", "https") and parts.netloc:
        return url
    return None


def refine(raw: list[dict]) -> list[dict]:
    """원시 항목 → 위생 처리된 후보. 제목·URL·날짜 중 하나라도 없으면 버린다
    (날짜 없는 항목은 감쇠·만료를 계산할 수 없다). 같은 URL 은 첫 피드가 이긴다
    — 피드 순서가 고정이라 결정론이 유지된다."""
    out: list[dict] = []
    seen: set[str] = set()
    for r in raw:
        title = clean_text(r.get("title") or "")
        url = valid_url(r.get("url") or "")
        when = r.get("published")
        if not title or not url or when is None or url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url,
                    "source": clean_text(r.get("source") or "") or "?",
                    "summary": _truncate(clean_text(r.get("summary") or "")),
                    "when": when})
    return out


# --------------------------------------------------------------------------
# 선별 — 점수 · 소스 다양성 · 중복 제거
# --------------------------------------------------------------------------
def base_score(text: str, when: datetime, now: datetime) -> float:
    """키워드 가중 점수 × 최신성 감쇠. 키워드는 각 항목당 한 번만 센다."""
    t = text.lower()
    kw = 1.0 + sum(w for rx, w in _KEYWORD_RE if rx.search(t))
    if _BTC_RE.search(t) is None:
        kw *= NON_BTC_MULT                      # '필수급' — 없으면 사실상 뒷줄
    age_h = max(0.0, (now - when).total_seconds() / 3600.0)   # 미래 날짜는 0 으로 클램프
    return kw * 0.5 ** (age_h / HALF_LIFE_HOURS)


def tokens(title: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(title.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0                              # 둘 다 빈 제목 — 같은 쓰레기로 본다
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _order(pair: tuple[float, dict]) -> tuple[float, float, str]:
    """정렬 키: score 내림차순 → published 내림차순 → url (완전 결정론)."""
    s, c = pair
    return (-s, -c["when"].timestamp(), c["url"])


def rank(cands: list[dict], now: datetime) -> list[dict]:
    """점수 → 소스 다양성 감점 → 제목 자카드 중복 제거, 정렬된 목록으로.

    소스 다양성: 점수순으로 세었을 때 같은 소스의 k번째 항목에 ×SOURCE_DECAY^k.
    하드 쿼터가 아니라 부드러운 감점이다 — 한 매체가 정말 중요한 기사를 몰아
    냈다면 그래도 올라온다. 중복 제거는 감점 후 순서로 돌아, 살아남는 쪽이
    항상 최종 점수가 높은 쪽이다(자카드 0.6↑ = 통신사발 리라이트 수준의 겹침)."""
    prelim = [(base_score(f"{c['title']} {c['summary']}", c["when"], now), c)
              for c in cands]
    prelim.sort(key=_order)
    per_source: dict[str, int] = {}
    adjusted: list[tuple[float, dict]] = []
    for s, c in prelim:
        k = per_source.get(c["source"], 0)
        per_source[c["source"]] = k + 1
        adjusted.append((s * SOURCE_DECAY ** k, c))
    adjusted.sort(key=_order)
    kept: list[tuple[float, dict]] = []
    kept_tokens: list[frozenset[str]] = []
    for s, c in adjusted:
        t = tokens(c["title"])
        if any(jaccard(t, kt) >= JACCARD_DUP for kt in kept_tokens):
            continue
        kept.append((s, c))
        kept_tokens.append(t)
    return [{**c, "score": round(s, 4)} for s, c in kept]


# --------------------------------------------------------------------------
# 병합 · 저장
# --------------------------------------------------------------------------
def load_old(path: Path, now: datetime, skip_urls: set[str]) -> list[dict]:
    """기존 news.json 에서 7일 이내 항목을 되살린다. 깨진 파일·이상한 항목은
    조용히 건너뛴다(방어적으로 — 파일 하나 때문에 수집이 죽지 않게). 텍스트는
    다시 위생 처리한다 — 손으로 고친 파일이 끼어들어도 규칙이 지켜지도록."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    out: list[dict] = []
    seen: set[str] = set(skip_urls)
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = clean_text(str(it.get("title") or ""))
        url = valid_url(str(it.get("url") or ""))
        when = parse_when(str(it.get("published") or ""))
        if not title or not url or when is None or url in seen:
            continue
        if now - when > timedelta(days=KEEP_DAYS):
            continue
        seen.add(url)
        out.append({"title": title, "url": url,
                    "source": clean_text(str(it.get("source") or "")) or "?",
                    "summary": _truncate(clean_text(str(it.get("summary") or ""))),
                    "when": when})
    return out


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def write_atomic(path: Path, payload: dict) -> None:
    """같은 폴더의 임시 파일에 쓰고 Path.replace 로 바꿔치기 — 쓰다 죽어도
    기존 파일이 반쯤 망가지지 않는다(fetch_macro.merge_to_csv 와 같은 방식)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def main(argv: Optional[list[str]] = None, fetch: Optional[Fetch] = None,
         now: Optional[datetime] = None) -> int:
    """수집 → 선별 → 병합 → 저장. fetch·now 는 테스트가 주입한다(오프라인 검증).

    종료 코드: 0 정상 · 1 전 피드 실패(기존 파일 불변)."""
    ap = argparse.ArgumentParser(description="비트코인 핵심 뉴스 수집 → data/news.json")
    ap.add_argument("--out", default="data/news.json")
    args = ap.parse_args(argv)
    fetch = fetch or fetch_url
    now = now or datetime.now(timezone.utc)
    out_path = Path(args.out)

    raw, ok, failed = collect(fetch)
    for line in failed:
        print(f"  ! {line}", file=sys.stderr)
    if not ok:
        # 폴백까지 전부 실패 — 아무것도 쓰지 않는다. 마지막 성공본이 페이지에 남는
        # 것이 빈 페이지보다 낫고, 원인(정책 403 여부)은 위 stderr 로 이미 알렸다.
        print("전 피드 실패 — 기존 파일을 건드리지 않습니다.", file=sys.stderr)
        return 1

    fresh = rank(refine(raw), now)[:TOP_NEW]
    old = load_old(out_path, now, skip_urls={i["url"] for i in fresh})
    # 병합분 전체를 다시 점수 매긴다 — 저장된 score 를 믿지 않고 텍스트·시각에서
    # 재계산해야 오래된 기사가 감쇠로 자연히 가라앉고, 두 번 돌려도 같은 파일이 나온다.
    merged = rank(fresh + old, now)[:CAP]

    payload = {
        "fetched": _iso(now),
        "sources_ok": ok,
        "sources_failed": failed,
        "items": [{"title": i["title"], "url": i["url"], "source": i["source"],
                   "published": _iso(i["when"]), "summary": i["summary"],
                   "score": i["score"]} for i in merged],
    }
    write_atomic(out_path, payload)
    fresh_urls = {i["url"] for i in fresh}
    n_fresh = sum(1 for i in merged if i["url"] in fresh_urls)
    print(f"저장: {args.out}  (신규 {n_fresh} · 유지 {len(merged) - n_fresh} · "
          f"총 {len(merged)}건)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
