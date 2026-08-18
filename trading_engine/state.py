import operator
from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage


class TradingState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
    macd_signal: str          # 'BULLISH', 'BEARISH', or 'NEUTRAL'
    sma_trend: str             # 'ABOVE_SMA' or 'BELOW_SMA'
    bollinger_zone: str
    bollinger_cross: str       # 'CROSS_UP', 'CROSS_DOWN', or 'NONE' — 20-period midline cross        # 'UPPER_BAND', 'LOWER_BAND', or 'NORMAL'
    rsi_zone: str              # 'OVERBOUGHT' (>=70), 'OVERSOLD' (<=30), or 'NEUTRAL'
    market_sentiment: str      # 'GOOD' or 'BAD'
    macro_halt: bool           # VIX at/above its ceiling — no entries in either direction
    macro_confidence: float    # model's confidence in the macro verdict
    macro_risk_factor: str     # model's one-line reason, logged for review
    execution_status: str      # 'BUY_CALL', 'BUY_MORE', 'SELL_ALL', 'HOLD'
    playbook: str              # Named time-window strategy that opened the position ('' if none opened)
    exit_reason: str           # Why a position closed: 'FORCE_CLOSE', 'TAKE_PROFIT', 'STOP_LOSS', 'RISK_OFF' ('' if nothing closed)
    buy_more_count: int        # Tracking safety scale-ins
