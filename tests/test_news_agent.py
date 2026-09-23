"""The news agent: ticker/intent detection, routing, the public route's limits,
and the stored-headline query against Postgres when it is reachable."""

import asyncio

import pytest

from GENAI.agents import news_agent as na
from GENAI.agents.supervisor import _route


@pytest.mark.parametrize("q,sym", [
    ("what is the latest news on MU", "MU"),
    ("latest news on mu?", "MU"),
    ("any headlines about Micron today", "MU"),
    ("news on $pltr", "PLTR"),
    ("What's happening with NVDA", "NVDA"),
    ("why is Seagate down", "STX"),
    ("Western Digital news", "WDC"),
    ("latest news on AMD", "AMD"),            # not in ALIASES: capitals make it a ticker
])
def test_detects_symbol(q, sym):
    assert na.detect_symbol(q) == sym
    assert na.is_news_question(q)


@pytest.mark.parametrize("q", [
    "what services do you offer",
    "any news from the CEO",                  # CEO is not a ticker
    "what is the latest AI news",             # no company named
    "how did our best trade do this week",    # a book question, not news
    "tell me about pineapple news",           # "apple" inside a word
])
def test_not_a_news_question(q):
    assert not na.is_news_question(q)


def test_routing():
    assert _route({"query": "latest news on MU", "use_db": True}) == ["news_agent"]
    assert _route({"query": "latest news on MU and our MU trades", "use_db": True}) == \
        ["news_agent", "trading_db_agent"]
    assert _route({"query": "best trade this week", "use_db": True}) == ["trading_db_agent"]
    assert _route({"query": "latest news on MU", "csv_text": "a,b"}) == ["csv_agent"]


def test_headline_patterns_match_ticker_in_capitals_only():
    aliases, ticker = na.headline_patterns("MU")
    assert aliases == [r"\mmicron\M"]
    assert ticker == r"\mMU\M"
    assert na.headline_patterns("NVDA")[0] == [r"\mnvidia\M"]   # the ticker alias is not repeated


def test_bad_symbols_never_reach_sql_or_polygon():
    assert na.stored_headlines("MU; DROP TABLE x") == []
    assert na.polygon_headlines("../../etc") == []


def test_dedupe_keeps_newest_one_per_headline():
    from datetime import datetime, timezone
    t1 = datetime(2026, 9, 23, 13, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 23, 14, tzinfo=timezone.utc)
    rows = na._dedupe([{"title": "Micron beats!", "published": t1, "source": "rss"},
                       {"title": "micron beats", "published": t2, "source": "POLYGON:x"},
                       {"title": "Micron guides up", "published": t1, "source": "rss"}])
    assert [r["source"] for r in rows] == ["POLYGON:x", "rss"]


def test_public_route_refuses_long_input_and_skips_non_news(client, monkeypatch):
    called = []
    monkeypatch.setattr(na, "gather", lambda s: called.append(s) or [])
    assert client.post("/api/news/ask", json={"query": "x" * 301}).status_code == 422
    r = client.post("/api/news/ask", json={"query": "what services do you offer"})
    assert r.status_code == 200 and r.json()["is_news"] is False
    assert called == []                           # no feed read for a non-news question


def test_answer_falls_back_to_list_when_model_fails(monkeypatch):
    from datetime import datetime, timezone
    na._CACHE.clear()
    monkeypatch.setattr(na, "gather", lambda s: [{"title": "Micron beats", "source": "rss", "url": None,
                                                  "sentiment": None,
                                                  "published": datetime.now(timezone.utc)}])
    monkeypatch.setattr(na, "latest_verdict", lambda s: None)

    async def boom(*a, **k):
        raise RuntimeError("no key")
    monkeypatch.setattr(na, "agenerate", boom)
    res = asyncio.run(na.answer_news("latest news on MU"))
    assert res["symbol"] == "MU" and "Micron beats" in res["answer"]
    na._CACHE.clear()


def test_stored_headlines_query_runs(db_available):
    if not db_available:
        pytest.skip("Postgres unreachable")
    rows = na.stored_headlines("MU", hours=24 * 30)
    assert isinstance(rows, list)
    for r in rows:
        assert set(r) >= {"title", "source", "published", "url", "sentiment"}
