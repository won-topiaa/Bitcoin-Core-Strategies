"""뉴스 수집기(tools/fetch_news.py) — 네트워크 없이 픽스처로 전 경로를 검증한다.

이 환경에서 실제 피드는 프록시가 403 으로 막는다(실제 수집은 CI 러너가 한다).
그래서 fetch 함수를 주입해 파싱·위생·선별·병합·실패 경로를 오프라인·결정론으로
검증한다. 픽스처 XML/JSON 은 규약대로 이 파일 안 문자열로만 둔다.

지키려는 것 세 가지:
  1. 이 텍스트는 페이지에 구워진다 — 태그·엉뚱한 스킴이 살아 나가면 안 된다.
  2. 병합은 refresh_data 철학 — 이번 run 실패가 기존 파일을 망가뜨리면 안 된다.
  3. 같은 입력이면 같은 바이트 — 두 번 돌려 파일이 흔들리면 diff 가 소음이 된다.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fetch_news as fn   # conftest 가 tools/ 를 경로에 넣는다

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)

RSS_URL = dict(fn.RSS_FEEDS)["CoinDesk"]
RSS_URL2 = dict(fn.RSS_FEEDS)["Cointelegraph"]
RSS_URL3 = dict(fn.RSS_FEEDS)["Bitcoin Magazine"]
RSS_URL4 = dict(fn.RSS_FEEDS)["Decrypt"]
CC_URL = fn.FALLBACK[1]

EMPTY_RSS = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<rss version="2.0"><channel><title>t</title></channel></rss>')

ATOM_FIX = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>t</title>
  <entry>
    <title>Bitcoin halving countdown begins</title>
    <link rel="self" href="https://example.org/self-not-article"/>
    <link rel="alternate" href="https://example.org/halving"/>
    <summary>Miners brace for the halving.</summary>
    <published>2026-08-02T08:30:00Z</published>
  </entry>
</feed>
"""

CC_JSON = json.dumps({"Data": [
    {"title": "Bitcoin steadies after selloff", "url": "https://cc.example/1",
     "body": "Markets calm down.", "published_on": int(NOW.timestamp()) - 3600,
     "source_info": {"name": "SomeWire"}},
    {"title": "no date item is dropped", "url": "https://cc.example/2",
     "body": "x", "source_info": {"name": "SomeWire"}},
]})


def rss(items_xml: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel><title>t</title>'
            + items_xml + "</channel></rss>")


def rss_item(title: str, link: str, desc: str = "",
             pub: str = "Sun, 02 Aug 2026 09:00:00 GMT") -> str:
    return (f"<item><title><![CDATA[{title}]]></title><link>{link}</link>"
            f"<description><![CDATA[{desc}]]></description>"
            f"<pubDate>{pub}</pubDate></item>")


def make_fetch(mapping: dict):
    """URL → 응답 문자열(또는 예외 인스턴스). 목록에 없는 URL 이면 KeyError 로
    시끄럽게 죽는다 — 수집기가 엉뚱한 URL 을 부르면 테스트가 그걸 잡아야 한다."""
    def fetch(url: str, timeout: int = 0) -> str:
        v = mapping[url]
        if isinstance(v, Exception):
            raise v
        return v
    return fetch


def full_mapping(coindesk, extra: dict = None) -> dict:
    """네 피드 + 폴백을 모두 채운 기본 매핑. 지정 안 한 피드는 빈 RSS."""
    m = {RSS_URL: coindesk, RSS_URL2: EMPTY_RSS, RSS_URL3: EMPTY_RSS,
         RSS_URL4: EMPTY_RSS, CC_URL: CC_JSON}
    m.update(extra or {})
    return m


def run(tmp_path: Path, mapping: dict, out: Path = None) -> tuple[int, dict]:
    out = out or tmp_path / "news.json"
    rc = fn.main(["--out", str(out)], fetch=make_fetch(mapping), now=NOW)
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return rc, data


# --------------------------------------------------------------------------
# 파싱 — RSS 2.0 · Atom · 폴백 JSON
# --------------------------------------------------------------------------
def test_rss_fixture_parses_title_link_date():
    items = fn.parse_feed_xml(
        rss(rss_item("Bitcoin up", "https://e.example/a", "desc here")), "CoinDesk")
    assert len(items) == 1
    it = items[0]
    assert it["url"] == "https://e.example/a" and it["source"] == "CoinDesk"
    assert it["published"] == datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)


def test_atom_fixture_picks_alternate_link_and_iso_date():
    """Atom 은 링크가 여러 개(self·alternate) — 기사 본문(alternate)을 골라야 하고,
    'Z' 접미 ISO 시각도 읽어야 한다(fromisoformat 은 3.11 전엔 Z 를 못 읽는다)."""
    items = fn.parse_feed_xml(ATOM_FIX, "Decrypt")
    assert len(items) == 1
    it = items[0]
    assert it["url"] == "https://example.org/halving"
    assert it["published"] == datetime(2026, 8, 2, 8, 30, tzinfo=timezone.utc)


def test_cryptocompare_fallback_parses_and_drops_dateless():
    items = fn.parse_cryptocompare(CC_JSON)
    assert len(items) == 1                      # published_on 없는 항목은 버려진다
    assert items[0]["source"] == "SomeWire"
    assert items[0]["published"].tzinfo is not None


# --------------------------------------------------------------------------
# 위생 처리 — 이 텍스트가 페이지에 구워진다
# --------------------------------------------------------------------------
def test_script_tag_in_title_becomes_plain_text():
    assert fn.clean_text("<script>alert(1)</script>Bitcoin &amp; <b>ETF</b>") \
        == "alert(1) Bitcoin & ETF"


def test_entity_encoded_tag_does_not_survive_unescape():
    """한 번만 벗기면 "&lt;script&gt;" 가 unescape 후 태그로 되살아난다 —
    고정점까지 반복해야 한다."""
    out = fn.clean_text("&lt;img src=x onerror=alert(1)&gt; BTC")
    assert "<" not in out and out == "BTC"


def test_control_chars_removed_and_whitespace_collapsed():
    assert fn.clean_text("Bitcoin\x00\x07  up\r\n now") == "Bitcoin up now"


def test_summary_truncated_to_280_chars(tmp_path):
    long_desc = "bitcoin news " * 40            # 520자
    rc, data = run(tmp_path, full_mapping(
        rss(rss_item("Bitcoin long story", "https://e.example/long", long_desc))))
    assert rc == 0
    summary = data["items"][0]["summary"]
    assert len(summary) <= 280 and summary.endswith("…")


def test_bad_url_schemes_drop_the_whole_item(tmp_path):
    feed = rss(
        rss_item("Bitcoin fine", "https://e.example/ok")
        + rss_item("Bitcoin evil", "javascript:alert(1)")
        + rss_item("Bitcoin sneaky", "JaVaScRiPt:alert(2)")
        + rss_item("Bitcoin data", "data:text/plain,evil")
        + rss_item("Bitcoin relative", "/no-scheme"))
    rc, data = run(tmp_path, full_mapping(feed))
    assert rc == 0
    assert [i["url"] for i in data["items"]] == ["https://e.example/ok"]


def test_baked_title_has_no_angle_brackets(tmp_path):
    rc, data = run(tmp_path, full_mapping(
        rss(rss_item("<script>alert(1)</script>Bitcoin rallies",
                     "https://e.example/x"))))
    assert rc == 0
    assert "<" not in data["items"][0]["title"]


# --------------------------------------------------------------------------
# 선별 — 점수 · 감쇠 · 소스 다양성 · 중복 제거
# --------------------------------------------------------------------------
def test_keyword_weights_rank_etf_above_generic():
    hi = fn.base_score("bitcoin spot etf approved", NOW, NOW)
    lo = fn.base_score("bitcoin conference opens", NOW, NOW)
    assert hi > lo


def test_missing_bitcoin_keyword_is_a_heavy_penalty():
    btc = fn.base_score("bitcoin rally continues", NOW, NOW)
    alt = fn.base_score("solana rally continues", NOW, NOW)
    assert alt == pytest.approx(btc * fn.NON_BTC_MULT)


def test_word_boundaries_avoid_sec_and_fed_false_positives():
    """'security' 안의 sec, 'federal'(연준 아님) 안의 fed 가 점수를 받으면 안 된다."""
    plain = fn.base_score("bitcoin quiet update", NOW, NOW)
    assert fn.base_score("bitcoin security update", NOW, NOW) == pytest.approx(plain)


def test_recency_decay_halves_at_48_hours():
    fresh = fn.base_score("bitcoin steady", NOW, NOW)
    stale = fn.base_score("bitcoin steady", NOW - timedelta(hours=48), NOW)
    assert stale == pytest.approx(fresh * 0.5)
    # 미래 날짜(피드 시계 오류)는 부스트가 아니라 0 시간으로 클램프
    ahead = fn.base_score("bitcoin steady", NOW + timedelta(hours=24), NOW)
    assert ahead == pytest.approx(fresh)


def _cand(title, url, source, when=NOW):
    return {"title": title, "url": url, "source": source, "summary": "", "when": when}


def test_source_diversity_penalizes_second_item_from_same_source():
    ranked = fn.rank([
        _cand("Bitcoin rises in quiet trading", "https://x.example/a", "X"),
        _cand("Bitcoin gains amid calm markets", "https://x.example/b", "X"),
        _cand("Bitcoin holds steady overnight", "https://y.example/c", "Y"),
    ], NOW)
    by_url = {i["url"]: i["score"] for i in ranked}
    # 같은 소스 두 번째는 ×SOURCE_DECAY, 다른 소스 첫 항목은 감점 없음
    assert by_url["https://x.example/b"] == pytest.approx(
        by_url["https://x.example/a"] * fn.SOURCE_DECAY, abs=1e-4)
    assert by_url["https://y.example/c"] == pytest.approx(
        by_url["https://x.example/a"], abs=1e-4)
    # 감점 반영 순서: X 1위·Y 1위(동점, url 순) 다음에 X 2위
    assert [i["url"] for i in ranked] == [
        "https://x.example/a", "https://y.example/c", "https://x.example/b"]


def test_jaccard_dedupe_keeps_higher_scored_rewrite():
    """토큰이 대부분 겹치는 두 제목(통신사발 리라이트)은 하나만 남는다."""
    ranked = fn.rank([
        _cand("Bitcoin hits all-time high above 150k mark", "https://a.example/1", "A"),
        _cand("Bitcoin hits all-time high above 150k today", "https://b.example/2", "B",
              NOW - timedelta(hours=1)),                    # 1시간 늦음 → 점수 낮음
        _cand("Bitcoin miners expand into new region", "https://c.example/3", "C"),
    ], NOW)
    urls = [i["url"] for i in ranked]
    assert "https://b.example/2" not in urls               # 진 쪽이 빠진다
    assert "https://a.example/1" in urls and "https://c.example/3" in urls


def test_jaccard_threshold_does_not_merge_different_stories():
    t1 = fn.tokens("Bitcoin ETF inflows surge on Monday")
    t2 = fn.tokens("Bitcoin miners face rising energy costs")
    assert fn.jaccard(t1, t2) < fn.JACCARD_DUP


# --------------------------------------------------------------------------
# 병합 — 상위 20 신규 · 7일 만료 · 상위 40 캡
# --------------------------------------------------------------------------
def _old_item(i: int, when: datetime) -> dict:
    return {"title": f"Bitcoin archive {i} old{i} keep{i}",
            "url": f"https://old.example/{i}", "source": "OldSrc",
            "published": when.isoformat(), "summary": "kept", "score": 1.0}


def test_merge_top20_new_seven_day_expiry_and_cap40(tmp_path):
    out = tmp_path / "news.json"
    # 기존 파일: 2일 지난 25건(살아야 함) + 10일 지난 5건(만료돼야 함)
    old_items = [_old_item(i, NOW - timedelta(days=2)) for i in range(25)] \
        + [_old_item(100 + i, NOW - timedelta(days=10)) for i in range(5)]
    out.write_text(json.dumps({"fetched": "x", "sources_ok": [], "sources_failed": [],
                               "items": old_items}, ensure_ascii=False),
                   encoding="utf-8")
    # 신규 피드: 25건 → 상위 20건만 새로 올라간다
    feed = rss("".join(
        rss_item(f"Bitcoin update {i} tag{i} extra{i}", f"https://new.example/{i}",
                 pub=(NOW - timedelta(hours=3)).strftime("%a, %d %b %Y %H:%M:%S GMT"))
        for i in range(25)))
    rc, data = run(tmp_path, full_mapping(feed), out=out)
    assert rc == 0
    urls = [i["url"] for i in data["items"]]
    assert len(urls) == 40                                    # 캡
    assert sum(1 for u in urls if u.startswith("https://new.")) == 20   # 신규 상한
    expired = {f"https://old.example/{100 + i}" for i in range(5)}
    assert not expired & set(urls), "10일 지난 항목이 살아남았습니다"
    # 정렬 계약: score 내림차순 (동점 세부 순서는 published↓ → url 이 결정론으로 가른다)
    scores = [i["score"] for i in data["items"]]
    assert scores == sorted(scores, reverse=True)


def test_new_fetch_of_same_url_wins_over_stored_copy(tmp_path):
    """같은 URL 을 다시 받으면 새 제목·요약이 이긴다(재수집이 더 권위)."""
    out = tmp_path / "news.json"
    out.write_text(json.dumps({"items": [
        {"title": "Bitcoin stale headline words", "url": "https://e.example/same",
         "source": "OldSrc", "published": (NOW - timedelta(days=1)).isoformat(),
         "summary": "old", "score": 1.0}]}, ensure_ascii=False), encoding="utf-8")
    rc, data = run(tmp_path, full_mapping(
        rss(rss_item("Bitcoin corrected headline", "https://e.example/same"))), out=out)
    assert rc == 0
    same = [i for i in data["items"] if i["url"] == "https://e.example/same"]
    assert len(same) == 1 and same[0]["title"] == "Bitcoin corrected headline"


# --------------------------------------------------------------------------
# 실패 경로 — 부분 실패 · 정책 403 구분 · 전 피드 실패 · 폴백
# --------------------------------------------------------------------------
def test_policy_403_is_reported_distinctly_but_run_continues(tmp_path):
    mapping = full_mapping(
        urllib.error.HTTPError(RSS_URL, 403, "Forbidden", None, None),
        {RSS_URL2: rss(rss_item("Bitcoin survives", "https://e.example/s")),
         RSS_URL3: urllib.error.URLError("Tunnel connection failed: 403 Forbidden")})
    rc, data = run(tmp_path, mapping)
    assert rc == 0                                        # 피드 하나가 전체를 못 죽인다
    assert any("CoinDesk" in f and "정책 차단" in f for f in data["sources_failed"])
    assert any("Bitcoin Magazine" in f and "정책 차단" in f
               for f in data["sources_failed"])           # CONNECT 거절형도 같은 분류
    assert "Cointelegraph" in data["sources_ok"]
    assert [i["url"] for i in data["items"]] == ["https://e.example/s"]


def test_all_feeds_fail_leaves_file_untouched_and_exits_1(tmp_path, capsys):
    out = tmp_path / "news.json"
    out.write_text('{"items": [], "sentinel": true}', encoding="utf-8")
    before = out.read_bytes()
    err = urllib.error.HTTPError("u", 403, "Forbidden", None, None)
    mapping = {RSS_URL: err, RSS_URL2: err, RSS_URL3: err, RSS_URL4: err, CC_URL: err}
    rc = fn.main(["--out", str(out)], fetch=make_fetch(mapping), now=NOW)
    assert rc == 1
    assert out.read_bytes() == before, "실패한 run 이 기존 파일을 건드렸습니다"
    assert "정책 차단" in capsys.readouterr().err


def test_fallback_api_fills_in_when_all_rss_die(tmp_path):
    err = urllib.error.URLError("connection refused")
    mapping = {RSS_URL: err, RSS_URL2: err, RSS_URL3: err, RSS_URL4: err,
               CC_URL: CC_JSON}
    rc, data = run(tmp_path, mapping)
    assert rc == 0
    assert data["sources_ok"] == ["CryptoCompare"]
    assert len(data["sources_failed"]) == 4
    assert [i["url"] for i in data["items"]] == ["https://cc.example/1"]


# --------------------------------------------------------------------------
# 결정론 — 같은 입력이면 같은 바이트
# --------------------------------------------------------------------------
def test_same_input_twice_produces_identical_bytes(tmp_path):
    feed = rss(rss_item("Bitcoin ETF record inflows", "https://e.example/1")
               + rss_item("Bitcoin miners sell into halving", "https://e.example/2",
                          pub="Sat, 01 Aug 2026 10:00:00 GMT"))
    out = tmp_path / "news.json"
    mapping = full_mapping(feed)
    assert fn.main(["--out", str(out)], fetch=make_fetch(mapping), now=NOW) == 0
    first = out.read_bytes()
    # 같은 경로에 다시 — 병합(자기 자신과)까지 포함해 바이트 동일해야 한다
    assert fn.main(["--out", str(out)], fetch=make_fetch(mapping), now=NOW) == 0
    assert out.read_bytes() == first
    # 다른 경로에 처음부터 — 기존 파일 유무와 무관하게 같은 결과
    out2 = tmp_path / "news2.json"
    assert fn.main(["--out", str(out2)], fetch=make_fetch(mapping), now=NOW) == 0
    assert out2.read_bytes() == first
