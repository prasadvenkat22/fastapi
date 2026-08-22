import operator
from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage


class TradingState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
    qqq_close: float           # QQQ close on the indicator bar — recorded every cycle
    macd_signal: str          # 'BULLISH', 'BEARISH', or 'NEUTRAL'
    sma_trend: str
    ema9_side: str             # 'ABOVE_EMA9'/'BELOW_EMA9' — trailing-exit reference
    ema50_reject: bool         # high pierced the 50 EMA, close fell back below it
    ema_cross: str             # 'EMA9_ABOVE_SMA20'/'EMA9_BELOW_SMA20' — velocity
    vwap_side: str             # 'ABOVE_VWAP'/'BELOW_VWAP'/'UNKNOWN'
    rsi_band: str              # 'BULL_BAND'/'BEAR_BAND'/'NONE' — trend strength             # 'ABOVE_EMA9' or 'BELOW_EMA9' — trailing-exit reference             # 'ABOVE_SMA' or 'BELOW_SMA'
    oil_level: float           # WTI front-month (CL=F) — watched, not gating
    oil_change_pct: float      # crude move since the 09:30 open
    tnx_level: float           # 10-year yield, percent
    tnx_change_bps: float      # yield move since the open, basis points
    yields_direction: str      # 'RISING'/'FALLING'/'FLAT' — both directions recorded
    shadow_condor: dict        # Iron Condor marked but NOT traded — forward evidence only
    adx: float                 # Wilder ADX(14) — trend STRENGTH, not direction
    adx_zone: str              # 'TRENDING' (>=22), 'CHOPPY' (<22), or 'UNKNOWN'
    bollinger_zone: str
    bollinger_sd: float        # 20-period stdev — sizes the credit-spread strike distance
    bollinger_cross: str       # 'CROSS_UP', 'CROSS_DOWN', or 'NONE' — 20-period midline cross        # 'UPPER_BAND', 'LOWER_BAND', or 'NORMAL'
    rsi_zone: str              # 'OVERBOUGHT' (>=70), 'OVERSOLD' (<=30), or 'NEUTRAL'
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
    macro_halt: bool           # VIX at/above its ceiling — no entries in either direction
    macro_confidence: float    # model's confidence in the macro verdict
    macro_risk_factor: str     # model's one-line reason, logged for review
    execution_status: str      # 'BUY_CALL', 'BUY_MORE', 'SELL_ALL', 'HOLD'
    playbook: str              # Named time-window strategy that opened the position ('' if none opened)
    exit_reason: str           # Why a position closed: 'FORCE_CLOSE', 'TAKE_PROFIT', 'STOP_LOSS', 'RISK_OFF' ('' if nothing closed)
    buy_more_count: int        # Tracking safety scale-ins
