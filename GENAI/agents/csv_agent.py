import io

import pandas as pd
from langchain_anthropic import ChatAnthropic
from langchain_experimental.agents import create_pandas_dataframe_agent

from .state import SupervisorState


def run_csv_agent(state: SupervisorState) -> dict:
    """Answer the query against the uploaded CSV using a pandas dataframe agent."""
    df = pd.read_csv(io.StringIO(state["csv_text"]))
    llm = ChatAnthropic(model="claude-opus-5", max_tokens=1024)
    agent = create_pandas_dataframe_agent(
        llm,
        df,
        verbose=False,
        allow_dangerous_code=True,
    )
    result = agent.invoke({"input": state["query"]})
    answer = result.get("output", str(result)) if isinstance(result, dict) else str(result)
    return {"csv_answer": answer}
