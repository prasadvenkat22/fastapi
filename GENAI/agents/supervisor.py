from typing import List, Optional, Union

from langchain_anthropic import ChatAnthropic
from langgraph.graph import END, StateGraph

from .csv_agent import run_csv_agent
from .pdf_agent import run_pdf_agent
from .state import SupervisorState


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
    return branches or "synthesize"


def _synthesize(state: SupervisorState) -> dict:
    csv_answer = state.get("csv_answer")
    pdf_answer = state.get("pdf_answer")

    if csv_answer and pdf_answer:
        llm = ChatAnthropic(model="claude-opus-5", max_tokens=1024)
        prompt = (
            f"A user asked: {state['query']}\n\n"
            f"A CSV-analysis agent answered:\n{csv_answer}\n\n"
            f"A PDF-analysis agent answered:\n{pdf_answer}\n\n"
            "Combine these into one coherent answer for the user."
        )
        response = llm.invoke(prompt)
        return {"final_answer": response.content}

    return {"final_answer": csv_answer or pdf_answer or "No CSV or PDF content was provided to analyze."}


def build_supervisor_graph():
    graph = StateGraph(SupervisorState)
    graph.add_node("supervisor", _supervisor)
    graph.add_node("csv_agent", run_csv_agent)
    graph.add_node("pdf_agent", run_pdf_agent)
    graph.add_node("synthesize", _synthesize)

    graph.set_entry_point("supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route,
        {"csv_agent": "csv_agent", "pdf_agent": "pdf_agent", "synthesize": "synthesize"},
    )
    graph.add_edge("csv_agent", "synthesize")
    graph.add_edge("pdf_agent", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


def run_supervisor(query: str, csv_text: Optional[str] = None, pdf_text: Optional[str] = None) -> SupervisorState:
    app = build_supervisor_graph()
    initial_state: SupervisorState = {"query": query, "csv_text": csv_text, "pdf_text": pdf_text}
    return app.invoke(initial_state)
