import operator
from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage


class TradingState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
    # Section 261: the 5-minute indicator agents (MACD, SMA/VWAP/EMA/ADX, zones,
    # Bollinger, RSI) were retired with the old entry tiers. The engine's only
    # signal is the 1-minute band touch, read inside execution_risk_agent.
    oil_level: float           # WTI front-month (CL=F) — watched, not gating
    oil_change_pct: float      # crude move since the 09:30 open
    tnx_level: float           # 10-year yield, percent
    tnx_change_bps: float      # yield move since the open, basis points
    yields_direction: str      # 'RISING'/'FALLING'/'FLAT' — both directions recorded
    # Self-computed Nasdaq-100 breadth. Recorded because it GATES -- a level
    # check and a collapse check both read it -- and until now it left no
    # number behind: the value drove the decision and survived only inside the
    # model's prose reason, which cannot be bucketed, correlated or swept.
    #
    # Scale matters when reading these against outside commentary. The real
    # $ADDQ spans roughly 3,000 Nasdaq issues, so the -600 and -1000 prints
    # quoted as institutional distribution are about -20% and -33% of that
    # universe. This basket is ~100 names, so the same distribution reads
    # about -20 and -33 here.
    breadth_addq: float        # advancers minus decliners, ~100-name basket
    breadth_advancers: int
    breadth_decliners: int
    breadth_net_ratio: float   # addq / basket size
    breadth_drawdown: float    # net ratio, down from the recent window's peak
    breadth_collapsing: bool   # drawdown past the collapse threshold
    market_sentiment: str      # 'GOOD' or 'BAD'
    macro_block_reason: str    # which AND-term refused: breadth/vix_level/vix_spike/yields/llm
    macro_halt: bool           # VIX at/above its ceiling — no entries in either direction
    macro_confidence: float    # model's confidence in the macro verdict
    macro_risk_factor: str     # model's one-line reason, logged for review
    execution_status: str      # 'BUY_CALL', 'BUY_MORE', 'SELL_ALL', 'HOLD'
    playbook: str              # Named time-window strategy that opened the position ('' if none opened)
    exit_reason: str           # Why a position closed: 'FORCE_CLOSE', 'TAKE_PROFIT', 'STOP_LOSS', 'RISK_OFF' ('' if nothing closed)
    buy_more_count: int        # Tracking safety scale-ins
