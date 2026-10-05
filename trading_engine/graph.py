from langgraph.graph import END, START, StateGraph

from .broker import MockBrokerClient
from .nodes import execution_risk_agent, market_signals_agent
from .state import TradingState


def build_trading_graph(broker: MockBrokerClient = None):
    """Macro read, then the rule engine. Section 261 retired the four
    5-minute indicator agents along with the entry tiers that read them."""
    graph = StateGraph(TradingState)
    graph.add_node("market_signals_agent", market_signals_agent)
    graph.add_node("execution_risk_agent", lambda state: execution_risk_agent(state, broker=broker))
    graph.add_edge(START, "market_signals_agent")
    graph.add_edge("market_signals_agent", "execution_risk_agent")
    graph.add_edge("execution_risk_agent", END)
    return graph.compile()


async def run_trading_cycle(broker: MockBrokerClient = None) -> TradingState:
    app = build_trading_graph(broker=broker)
    initial_state: TradingState = {
        "messages": [],
        "market_sentiment": "",
        "macro_halt": False,
        "macro_confidence": 0.0,
        "macro_risk_factor": "",
        "execution_status": "",
        "exit_reason": "",
        "playbook": "",
        "buy_more_count": 0,
    }
    return await app.ainvoke(initial_state)
