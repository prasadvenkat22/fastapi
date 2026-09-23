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


def _as_role(role):
    """Stand in for get_current_user, so require_role sees an account with `role`."""
    import types
    from helpers.auth_deps import get_current_user
    from main import app
    app.dependency_overrides[get_current_user] = lambda: types.SimpleNamespace(
        id=1, email="u@example.com", role=types.SimpleNamespace(role=role) if role else None)
    return app, get_current_user


def test_anonymous_visitors_get_no_chat(client):
    assert client.post("/api/chat/ask", json={"query": "latest news on MU"}).status_code == 401
    assert client.post("/api/news/ask", json={"query": "latest news on MU"}).status_code in (404, 405)


def test_chat_needs_a_role_and_never_takes_long_input(client, monkeypatch):
    app, dep = _as_role(None)
    try:
        assert client.post("/api/chat/ask", json={"query": "hi"}).status_code == 403
        _as_role("user")
        assert client.post("/api/chat/ask", json={"query": "x" * 2001}).status_code == 422
    finally:
        app.dependency_overrides.pop(dep, None)


def test_signed_in_user_gets_news_from_feeds_and_chat_from_the_model(client, monkeypatch):
    import GENAI.chat_router as cr
    na._CACHE.clear()
    called = []
    monkeypatch.setattr(na, "gather", lambda s: called.append(s) or [])
    monkeypatch.setattr(na, "latest_verdict", lambda s: None)
    prompts = []

    async def fake_generate(prompt, system=None, **kw):
        prompts.append(system)
        return "general answer"
    monkeypatch.setattr(cr, "agenerate", fake_generate)
    app, dep = _as_role("user")
    try:
        r = client.post("/api/chat/ask", json={"query": "what is the latest news on MU"}).json()
        assert r["kind"] == "news" and r["symbol"] == "MU" and called == ["MU"]
        r = client.post("/api/chat/ask", json={"query": "what services do you offer"}).json()
        assert r == {"kind": "chat", "answer": "general answer", "symbol": None, "headlines": []}
        assert called == ["MU"]                   # no feed read for a non-news question
        assert "NO access to any trading account" in prompts[-1]
    finally:
        app.dependency_overrides.pop(dep, None)


def test_site_user_still_refused_the_trading_chat(client):
    app, dep = _as_role("user")
    try:
        assert client.post("/api/genai/agent/ask", json={"query": "my positions"}).status_code == 403
    finally:
        app.dependency_overrides.pop(dep, None)


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


def test_trader_gets_the_trading_chat_but_not_the_admin_genai_routes(client, monkeypatch):
    import GENAI.router as gr

    async def fake_supervisor(query, use_db):
        return {"final_answer": "book answer", "db_answer": "rows", "db_sql": "SELECT 1"}
    monkeypatch.setattr(gr, "run_supervisor", fake_supervisor)
    app, dep = _as_role("trader")
    try:
        r = client.post("/api/genai/agent/ask", json={"query": "realised P&L this week"})
        assert r.status_code == 200 and r.json()["final_answer"] == "book answer"
        assert client.post("/api/genai/llm", json={"prompt": "hi"}).status_code == 403
    finally:
        app.dependency_overrides.pop(dep, None)
