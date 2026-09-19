"""GENAI on Gemini: message flattening and the trading-DB SQL guard. Section 200.
No network, no database."""
import pytest

pytest.importorskip("langchain_core")
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from GENAI import gemini_llm  # noqa: E402
from GENAI.agents import trading_db_agent as tda  # noqa: E402


def test_messages_flatten_with_system_and_alternation():
    contents, system = gemini_llm.messages_to_contents([
        SystemMessage(content="be terse"), HumanMessage(content="a"), HumanMessage(content="b"),
        AIMessage(content="c"), HumanMessage(content="d")])
    assert system == "be terse"
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert [p["text"] for p in contents[0]["parts"]] == ["a", "b"]


def test_text_extraction_handles_blocked():
    assert gemini_llm._text({"candidates": [{"content": {"parts": [{"text": "x"}, {"text": "y"}]}}]}) == "xy"
    assert "no text" in gemini_llm._text({"promptFeedback": {"blockReason": "SAFETY"}})


def test_guard_accepts_select_and_adds_limit():
    sql, why = tda.guard("select underlying, sum(realized_pnl_dollars) from trading_history group by 1 order by 2 desc")
    assert why is None and sql.endswith(f"LIMIT {tda.ROW_LIMIT}")


def test_guard_keeps_existing_limit_and_cte():
    sql, why = tda.guard("WITH w AS (SELECT * FROM weekly_shadow) SELECT count(*) FROM w LIMIT 5")
    assert why is None and sql.count("LIMIT") == 1


@pytest.mark.parametrize("bad,frag", [
    ("delete from trading_history", "only SELECT"),
    ("select 1; drop table users", "one statement"),
    ("select * from users", "not allowed"),
    ("select * from trading_history where 1=1 union select password_hash from users", "not allowed"),
    ("select pg_sleep(1) into x from trading_history", "write or admin"),
    ("", "empty"),
])
def test_guard_refuses(bad, frag):
    sql, why = tda.guard(bad)
    assert sql is None and frag in why


def test_guard_ignores_verbs_inside_string_literals():
    sql, why = tda.guard("select * from news_seen where title ilike '%set to join%' and title not ilike '%delete%'")
    assert why is None
