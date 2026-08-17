from langgraph.graph import END, START, StateGraph

from .broker import MockBrokerClient
from .nodes import bollinger_agent, execution_risk_agent, macd_agent, market_signals_agent, rsi_agent, sma_agent
from .state import TradingState


def build_trading_graph(broker: MockBrokerClient = None):
    graph = StateGraph(TradingState)
    graph.add_node("macd_agent", macd_agent)
    graph.add_node("sma_agent", sma_agent)
    graph.add_node("bollinger_agent", bollinger_agent)
    graph.add_node("rsi_agent", rsi_agent)
    graph.add_node("market_signals_agent", market_signals_agent)
    graph.add_node("execution_risk_agent", lambda state: execution_risk_agent(state, broker=broker))

    # START fans out to all 5 indicator nodes in parallel
    graph.add_edge(START, "macd_agent")
    graph.add_edge(START, "sma_agent")
    graph.add_edge(START, "bollinger_agent")
    graph.add_edge(START, "rsi_agent")
    graph.add_edge(START, "market_signals_agent")

    # All 5 fan in to the execution_risk_agent confluence point
    graph.add_edge("macd_agent", "execution_risk_agent")
    graph.add_edge("sma_agent", "execution_risk_agent")
    graph.add_edge("bollinger_agent", "execution_risk_agent")
    graph.add_edge("rsi_agent", "execution_risk_agent")
    graph.add_edge("market_signals_agent", "execution_risk_agent")

    graph.add_edge("execution_risk_agent", END)

    return graph.compile()


async def run_trading_cycle(broker: MockBrokerClient = None) -> TradingState:
    app = build_trading_graph(broker=broker)
    initial_state: TradingState = {
        "messages": [],
        "macd_signal": "",
        "sma_trend": "",
        "bollinger_zone": "",
        "rsi_zone": "",
        "market_sentiment": "",
        "execution_status": "",
        "exit_reason": "",
        "buy_more_count": 0,
    }
    return await app.ainvoke(initial_state)
