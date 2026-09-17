"""검색 1회 = 증거 집합 하나 — 모델 tool_result · 되돌림 digest · 🔎 줄이 같은 것을 쓴다.

2026-09-17 US Cowork S3 실측(1.0.69~1.0.75 전 구간):
- 모델이 max_results 15 를 요구하면 설정 상한 5(WEB_SEARCH_MAX_RESULTS_DEFAULT)는 "기본값"이라
  무시됐다 → 15×1500자 > 12k 캡 → **검색마다** JSON 이 레코드 중간에서 절단(tool_result 12,069자,
  파싱 불가, 15건 중 8건만 보임, 그중 같은 기사의 미러 3건).
- 되돌림 digest 는 절단 JSON 을 못 읽어 원본에서 5건을 다시 만들었고(시작점도 다름), 🔎 줄은 15 를
  셌다. 셋이 서로 다르니 모델은 "내가 본 결과에 없다" 며 실제 근거를 되물렸다.

계약:
- 상한은 **최대**: keep = min(모델 요청, 상한). 커넥터엔 중복 제거 여유로 2×상한까지 요청.
- URL 중복 + 미러(본문 8-단어 지문 포함률 ≥ 0.5)를 제거하고 처음 keep 건만 남긴다.
- 본문은 단어 구간부터 시작, 결과당 길이는 min(항목 상한, (캡 − 오버헤드)/keep) — 캡 안에 들어가
  JSON 이 절단되지 않는다(_truncate_result 는 안전망).
- digest = 증거 그대로(같은 title/url/date/text). 🔎 줄의 n = 남긴 수.
- provider 텍스트가 JSON 이 아니면 종전 경로(전체 캡 + 원본 digest).
"""

from __future__ import annotations

import json

import app.services.web_search_loop as wsl

PREFIX = wsl._TRACE_PREFIX
ARTICLE = (
    "SK hynix began volume shipping of 16-layer HBM4 for Nvidia Rubin in the third "
    "quarter, according to a supply-chain tracker. Micron has been in high-volume "
    "production of 12-layer HBM4 since March and has shipped 16-layer samples. Samsung "
    "started producing HBM4 in February and its 12-layer stack is in the final "
    "qualification stage. TrendForce still estimates a supply gap near 20% this year. "
)


def _raw(items):
    return json.dumps({"results": items}, ensure_ascii=False)


class _Mcp:
    def __init__(self, items):
        self.items = items
        self.calls: list[tuple[str, int]] = []

    async def ensure_initialized(self):
        return "WebSearch"

    async def search(self, q, n):
        self.calls.append((q, n))

        class _R:
            raw_text = _raw(self.items[:n])
            results = None

        return _R()


def _mirrors():
    return [
        {
            "url": "https://www.benzinga.com/news/1",
            "title": "SK Hynix 16-Layer HBM4: Where Micron, Samsung Stand",
            "text": "705.310.55% 76537.602.1162% September 15, 2026 2:23 PM 3 min read "
            + ARTICLE * 3,
            "publishedDate": "2026-09-15",
        },
        {
            "url": "https://www.tradingview.com/news/benzinga:1",
            "title": "Where Do Micron And Samsung Stand? — TradingView News",
            "text": "2 min read 000660NVDAM " + ARTICLE * 3,
        },
        {
            "url": "https://longbridge.com/en/news/1",
            "title": "SK Hynix Is Shipping 16-Layer HBM4 for Nvidia Rubin",
            "text": "I'm LongbridgeAI. " + ARTICLE * 3,
        },
        {
            "url": "https://news.skhynix.com/hbm4",
            "title": "SK hynix Completes World's First HBM4 Development",
            "text": "SK hynix announced it completed HBM4 development and readied mass production. "
            * 20,
        },
        {
            "url": "https://tech-insider.org/hbm4",
            "title": "HBM4 Crunch: DRAM Inventory Falls Below 10 Days",
            "text": (
                "Samsung, SK Hynix, and Micron: who is winning the HBM4 race. "
                "Inventory fell below ten days. "
            )
            * 20,
        },
        {
            "url": "https://semiconductor.samsung.com/kr/hbm4",
            "title": "삼성전자, 세계 최초 HBM4 양산 출하",
            "text": (
                "삼성전자가 세계 최초로 업계 최고 성능의 HBM4를 양산 출하하며 시장 선점에 나섰다. "
            )
            * 30,
        },
        {
            "url": "https://finance.yahoo.com/skhy",
            "title": "SKHY upbeat",
            "text": "SK Hynix gave an upbeat projection. " * 30,
        },
    ]


def test_build_evidence_dedupes_mirrors_caps_count_and_fits_budget():
    items = _mirrors()
    built = wsl._build_evidence(_raw(items), keep=5, per_result_chars=1500, max_chars=12000)
    assert built is not None
    evidence, stats = built
    assert stats["fetched"] == 7 and stats["dup_mirror"] == 2 and stats["dup_url"] == 0
    assert [e["url"].split("/")[2] for e in evidence] == [
        "www.benzinga.com",
        "news.skhynix.com",
        "tech-insider.org",
        "semiconductor.samsung.com",
        "finance.yahoo.com",
    ], "미러 2건이 빠지고 서로 다른 출처 5건이 남는다"
    assert evidence[0]["text"].startswith("SK hynix began volume shipping"), (
        "시세표 잡음을 건너뛴다"
    )
    assert evidence[0]["publishedDate"] == "2026-09-15", "본문 외 키는 보존"
    assert all(len(e["text"]) <= 1502 for e in evidence)
    text = json.dumps({"results": evidence}, ensure_ascii=False, separators=(",", ":"))
    assert len(text) <= 12000, len(text)
    assert stats["kept"] == 5 and stats["per_result"] == 1500


def test_build_evidence_budget_shrinks_per_result_so_the_set_fits_the_cap():
    items = _mirrors()
    evidence, stats = wsl._build_evidence(
        _raw(items), keep=5, per_result_chars=1500, max_chars=3000
    )
    assert 300 <= stats["per_result"] < 1500 and len(evidence) == 5
    assert all(len(e["text"]) <= stats["per_result"] + 2 for e in evidence)
    text = json.dumps({"results": evidence}, ensure_ascii=False, separators=(",", ":"))
    assert len(text) <= 3000 and json.loads(text)["results"], "캡 안에 들어가 절단이 없다"
    # per-result cut disabled → bodies untouched (the old whole-JSON cap applies downstream)
    evidence, stats = wsl._build_evidence(_raw(items), keep=0, per_result_chars=0, max_chars=100)
    assert stats["kept"] == 5 and stats["per_result"] == 0
    assert any(len(e["text"]) > 400 for e in evidence)


def test_build_evidence_tolerates_non_json_and_odd_items():
    assert wsl._build_evidence("not json", keep=5, per_result_chars=100, max_chars=0) is None
    assert wsl._build_evidence('{"error":"x"}', keep=5, per_result_chars=100, max_chars=0) is None
    evidence, stats = wsl._build_evidence(
        '[{"t":"WEBTEXT"}, 7, {"url":"https://a.com","text":"a"}]',
        keep=5,
        per_result_chars=100,
        max_chars=0,
    )
    assert [e.get("t") for e in evidence] == ["WEBTEXT", None] and evidence[1]["text"] == "a"
    # URL duplicates (fragment / trailing slash) still drop
    evidence, stats = wsl._build_evidence(
        _raw(
            [
                {"url": "https://a.com/x", "text": "one"},
                {"url": "https://a.com/x/#frag", "text": "two"},
            ]
        ),
        keep=5,
        per_result_chars=100,
        max_chars=0,
    )
    assert stats["dup_url"] == 1 and len(evidence) == 1


async def test_do_search_enforces_the_cap_and_uses_one_evidence_set_everywhere():
    mcp = _Mcp(_mirrors())
    text, ok, trace, digest = await wsl._do_search(
        mcp, {"query": "q", "max_results": 15}, 5, 12000, 1500
    )
    assert ok and mcp.calls == [("q", 10)], "모델의 15 → 상한 5, 여유 2배로 10 을 커넥터에"
    items = json.loads(text)["results"]  # always valid JSON now
    assert len(items) == 5 and "truncated by gateway" not in text
    assert trace.startswith(
        f'{PREFIX} "q" — 5 results (benzinga.com, news.skhynix.com, tech-insider.org)'
    )
    assert [d["url"] for d in digest] == [i["url"] for i in items]
    assert [d["snippet"] for d in digest] == [i["text"] for i in items], "되돌림 = 모델이 본 것"
    assert digest[0]["page_age"] == "2026-09-15" and digest[0]["title"] == items[0]["title"]
    # a smaller request is honored as-is; no cap (0) → request passes through unchanged
    mcp = _Mcp(_mirrors())
    text, ok, trace, digest = await wsl._do_search(mcp, {"query": "q", "max_results": 3}, 5, 0, 300)
    assert mcp.calls == [("q", 6)] and len(json.loads(text)["results"]) == 3
    mcp = _Mcp(_mirrors())
    await wsl._do_search(mcp, {"query": "q", "max_results": 7}, 0, 0, 300)
    assert mcp.calls == [("q", 7)]


async def test_do_search_non_json_provider_falls_back_to_whole_text_cap():
    class _Raw:
        calls = []

        async def search(self, q, n):
            class _R:
                raw_text = "plain text result " * 50
                results = [{"url": "https://z.com", "title": "Z", "text": "Z body"}]

            return _R()

    text, ok, trace, digest = await wsl._do_search(_Raw(), {"query": "q"}, 5, 120, 300)
    assert ok and text.startswith("plain text result") and "truncated by gateway" in text
    assert trace.startswith(f'{PREFIX} "q" — 1 result (z.com)') and digest[0]["title"] == "Z"


def test_mirror_fingerprint_is_conservative():
    a = wsl._fingerprint(ARTICLE)
    b = wsl._fingerprint(
        "Totally different story about DRAM contract prices rising 13% in Q3. " * 5
    )
    assert not wsl._is_mirror(b, [a]) and wsl._is_mirror(wsl._fingerprint("x y z " + ARTICLE), [a])
    assert not wsl._is_mirror(wsl._fingerprint("short"), [a]), (
        "지문이 너무 짧으면 미러로 보지 않는다"
    )


def test_evidence_always_fits_the_cap_property():
    """불변식: keep×per×cap 어떤 조합이든 직렬화 길이 ≤ cap, _truncate_result 미발동, 결과 ≥ 1.
    제목 120자·URL 100자·따옴표/역슬래시/비ASCII 본문 3000자 — 오버헤드 추정치가 아니라 실측."""
    title = (
        "SK Hynix Is Shipping 16-Layer HBM4 for Nvidia Rubin: Where Do Micron And Samsung Stand? "
        * 2
    )

    def body(
        i,
    ):  # 12 distinct articles: shared style, different words (identical bodies are mirrors)
        return " ".join(
            f'Story{i}word{j} said "yes\\no" — 삼성전자·SK하이닉스 {i}{j}%.' for j in range(120)
        )

    items = [
        {
            "url": f"https://example-news-site.com/2026/09/15/very-long-article-path-{i:03d}/"
            f"sk-hynix-16-layer-hbm4-nvidia-rubin-micron-samsung-{i:03d}",
            "title": title[:120],
            "text": body(i),
            "publishedDate": "11:23AM, Tuesday, September 15 2026, PDT",
        }
        for i in range(12)
    ]
    raw = _raw(items)
    for keep in range(1, 11):
        for per in (300, 800, 1500):
            for cap in (3000, 6000, 12000):
                evidence, stats = wsl._build_evidence(
                    raw, keep=keep, per_result_chars=per, max_chars=cap
                )
                text = json.dumps({"results": evidence}, ensure_ascii=False, separators=(",", ":"))
                assert evidence, (keep, per, cap)
                assert len(text) <= cap, (keep, per, cap, len(text))
                assert wsl._truncate_result(text, cap)[1] is False
                assert json.loads(text)["results"] == evidence
                assert stats["kept"] + stats["dropped_budget"] == min(keep, 12)


def test_mirror_detection_keeps_different_articles_that_share_a_long_quote():
    quote = (
        "SK hynix said it completed development of HBM4 and readied mass production, "
        "with bandwidth doubled through a 2,048-bit interface and power efficiency up 40 percent. "
    ) * 6
    a = (
        "Seoul Economic Daily reports on the supplier race. " * 12
        + quote
        + "Analysts expect tight supply into 2027. " * 12
    )
    b = (
        "Column: what the newsroom statement leaves out. " * 12
        + quote
        + "Investors should watch capex guidance closely. " * 12
    )
    evidence, stats = wsl._build_evidence(
        _raw(
            [
                {"url": "https://a.com/x", "title": "A", "text": a},
                {"url": "https://b.com/y", "title": "B", "text": b},
            ]
        ),
        keep=5,
        per_result_chars=1500,
        max_chars=12000,
    )
    assert len(evidence) == 2 and stats["dup_mirror"] == 0, (
        "긴 인용문을 공유해도 다른 기사는 둘 다 남는다"
    )
    # a truncated copy of the same article (first 60%, nothing else) IS a mirror
    mirror = a[: int(len(a) * 0.6)]
    evidence, stats = wsl._build_evidence(
        _raw(
            [
                {"url": "https://a.com/x", "title": "A", "text": a},
                {"url": "https://c.com/z", "title": "C", "text": mirror},
            ]
        ),
        keep=5,
        per_result_chars=1500,
        max_chars=12000,
    )
    assert len(evidence) == 1 and stats["dup_mirror"] == 1
