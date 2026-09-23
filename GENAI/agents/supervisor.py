from typing import List, Optional, Union

from GENAI.gemini_llm import GeminiChat
from langgraph.graph import END, StateGraph

from .csv_agent import run_csv_agent
from .news_agent import BOOK_WORDS, is_news_question, run_news_agent
from .pdf_agent import run_pdf_agent
from .trading_db_agent import run_trading_db_agent
from .state import SupervisorState
from .utils import extract_text


def _supervisor(state: SupervisorState) -> dict:
    """No-op passthrough — the routing decision is made by `_route` below."""
    return {}


def _route(state: SupervisorState) -> Union[str, List[str]]:
    """Uniformly inspects what was uploaded and routes to the matching specialist(s)."""
    branches = []
    if state.get("csv_text"):
        branches.append("csv_agent")
    if state.get("pdf_text"):
        branches.append("pdf_agent")
    # "Latest news on MU" is a news question, not a query of the book: it goes
    # to the news agent, and to the trading-database agent as well only when
    # it also asks about trades or positions.
    news = not branches and is_news_question(state.get("query", ""))
    if news:
        branches.append("news_agent")
    # A question with nothing uploaded is a question about the trading book
    # (section 200); `use_db` asks for it beside an upload.
    if (state.get("use_db") or not branches) and (not news or BOOK_WORDS.search(state["query"])):
        branches.append("trading_db_agent")
    return branches


async def _synthesize(state: SupervisorState) -> dict:
    answers = [(label, state.get(key)) for label, key in (
        ("CSV-analysis agent", "csv_answer"), ("PDF-analysis agent", "pdf_answer"),
        ("trading-database agent", "db_answer"), ("news agent", "news_answer")) if state.get(key)]
    if len(answers) > 1:
        llm = GeminiChat(max_tokens=1024)
        prompt = f"A user asked: {state['query']}\n\n" + "".join(
            f"A {label} answered:\n{ans}\n\n" for label, ans in answers
        ) + "Combine these into one coherent answer for the user. Keep any SQL line at the end."
        response = await llm.ainvoke(prompt)
        return {"final_answer": extract_text(response.content)}
    if answers:
        return {"final_answer": answers[0][1]}
    return {"final_answer": "Nothing to analyze: no upload, no news question and no question the trading database could answer."}


def build_supervisor_graph():
    graph = StateGraph(SupervisorState)
    graph.add_node("supervisor", _supervisor)
    graph.add_node("csv_agent", run_csv_agent)
    graph.add_node("pdf_agent", run_pdf_agent)
    graph.add_node("trading_db_agent", run_trading_db_agent)
    graph.add_node("news_agent", run_news_agent)
    graph.add_node("synthesize", _synthesize)

    graph.set_entry_point("supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route,
        {"csv_agent": "csv_agent", "pdf_agent": "pdf_agent",
         "trading_db_agent": "trading_db_agent", "news_agent": "news_agent",
         "synthesize": "synthesize"},
    )
    graph.add_edge("csv_agent", "synthesize")
    graph.add_edge("pdf_agent", "synthesize")
    graph.add_edge("trading_db_agent", "synthesize")
    graph.add_edge("news_agent", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


async def run_supervisor(query: str, csv_text: Optional[str] = None, pdf_text: Optional[str] = None,
                         use_db: bool = False) -> SupervisorState:
    app = build_supervisor_graph()
    initial_state: SupervisorState = {"query": query, "csv_text": csv_text, "pdf_text": pdf_text,
                                      "use_db": use_db}
    return await app.ainvoke(initial_state)
