import operator
from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage


class TradingState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
    macd_signal: str          # 'BULLISH', 'BEARISH', or 'NEUTRAL'
    sma_trend: str             # 'ABOVE_SMA' or 'BELOW_SMA'
    bollinger_zone: str        # 'UPPER_BAND', 'LOWER_BAND', or 'NORMAL'
    rsi_zone: str              # 'OVERBOUGHT' (>=70), 'OVERSOLD' (<=30), or 'NEUTRAL'
    market_sentiment: str      # 'GOOD' or 'BAD'
    execution_status: str      # 'BUY_CALL', 'BUY_MORE', 'SELL_ALL', 'HOLD'
    exit_reason: str           # Why a position closed: 'FORCE_CLOSE', 'TAKE_PROFIT', 'STOP_LOSS', 'RISK_OFF' ('' if nothing closed)
    buy_more_count: int        # Tracking safety scale-ins
