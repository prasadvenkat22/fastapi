"""Replay past sessions through the real engine, sweeping one parameter.

The comments in playbook.py cite sweeps -- widths, entry times, stop
distances -- that no committed tool could reproduce. This is that tool.

It does NOT reimplement the strategy. It swaps out the clock, the data feed
and the database, then runs the engine's own indicator agents and its own
execution_risk_agent bar by bar, so a result here is what the deployed code
would have done rather than what a second implementation of it thinks.

    python scripts/sweep.py morning     # widths x long-leg depth
    python scripts/sweep.py credit      # credit window widths
    python scripts/sweep.py condor      # the structure we log but never trade
    python scripts/sweep.py macro       # do yields, crude and VIX predict our day?
    python scripts/sweep.py placement   # morning width x depth, judged per DAY
    python scripts/sweep.py condorvs    # both sides vs the one side we already sell
    python scripts/sweep.py trenddepth  # uncap the spread when the trend is strong?
    python scripts/sweep.py daytrend    # refuse longs once the session is down
    python scripts/sweep.py daytype     # what the engine earns by market regime
    python scripts/sweep.py squeeze     # does a volatility squeeze make a morning safe to sell?
    python scripts/sweep.py bearside    # may the morning trade the short side too?
    python scripts/sweep.py ratchetstyle # dollar-offset ratchet vs share-of-peak
    python scripts/sweep.py cooldown    # how long to stand down after a loss
    python scripts/sweep.py size        # fewer, larger trades vs more, smaller ones
    python scripts/sweep.py daily       # does trading MORE per day earn more per day?
    python scripts/sweep.py creditexit  # giveback and window length for the credit trade
    python scripts/sweep.py creditgate  # does the credit window need a directional tier?
    python scripts/sweep.py rideratchet # profit protection for a riding position
    python scripts/sweep.py retries     # morning window length: does a second entry pay?
    python scripts/sweep.py breach      # model-free: how often a strike distance holds
    python scripts/sweep.py windows     # every window solo: which earns, which does not
    python scripts/sweep.py orb         # single-leg weekly calls on an opening-range break

What it cannot replay, and what that costs:

  * Macro sentiment and the VIX halt came from an LLM and a live feed. Every
    session here is assumed GOOD and un-halted, so bullish entries the macro
    gate would have refused are included. That flatters every long result
    equally, which is fine for RANKING variants against each other and wrong
    for predicting what a window will earn.
  * Premiums come from broker.py's model, not from a chain. The chain-vs-
    model divergence logging added on 2026-08-20 exists to put a number on
    that gap; until it has, treat absolute dollars as indicative and
    differences between variants as the real output.
  * yfinance serves 60 days of 5-minute bars, so the sample is what it is.
"""

import sys
from dataclasses import replace
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import trading_engine.broker as broker_mod
import trading_engine.nodes as N
import trading_engine.playbook as PB
from trading_engine.broker import (CALL_CREDIT_SPREAD, PUT_CREDIT_SPREAD, MockBrokerClient,
                                   estimate_credit_value, estimate_spread_value, fill_price,
                                   is_credit, round_to_strike)
from trading_engine.equity import EquityState

NY = ZoneInfo("America/New_York")
EQUITY = 10000.0

# Round-trip cost of crossing the spread, in premium terms, per contract.
#
# Measured on real QQQ quotes on 2026-08-21: a two-leg vertical's natural
# bid/ask gap was 0.15 on the morning debit spread (3.88/4.03) and 0.02 on
# the afternoon credit spread (0.59/0.61) -- call it 0.10 a contract as a
# round trip, or $10.
#
# Charging it matters for more than realism. Every variant that trades more
# often pays it more often, and a sweep that ignores it systematically
# flatters the busiest configuration -- which is precisely the axis several
# of these sweeps are deciding.
SLIPPAGE_ROUNDTRIP = 0.10


class _Clock:
    """The simulated 'now', shared by every patched time function."""
    now = datetime(2026, 1, 1, 9, 45, tzinfo=NY)


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _Clock.now


class _Cooldown:
    """The post-loss cooldown and the loss streak, simulated.

    Both were stubbed to "never blocking" in the first version of this
    harness, which quietly made two of the engine's risk rules invisible to
    every sweep run through it -- including any sweep of the cooldown itself.
    """
    minutes = 30.0
    last_loss_at = None
    last_loss_dir = None
    streak_today = 0

    BULLISH = ("BULL_CALL_SPREAD", "PUT_CREDIT_SPREAD")

    @classmethod
    def open_session(cls):
        cls.last_loss_at = None
        cls.last_loss_dir = None
        cls.streak_today = 0

    @classmethod
    def book(cls, strategy: str, pnl: float, ts):
        if pnl < 0:
            cls.last_loss_at = ts
            cls.last_loss_dir = "bullish" if strategy in cls.BULLISH else "bearish"
            cls.streak_today += 1
        else:
            cls.streak_today = 0

    @classmethod
    def blocked(cls):
        if cls.last_loss_at is None or cls.minutes <= 0:
            return None
        age = (_Clock.now - cls.last_loss_at).total_seconds() / 60.0
        return cls.last_loss_dir if age < cls.minutes else None

    @classmethod
    def streak(cls):
        return cls.streak_today


class _Account:
    """Equity that actually moves, so sizing and the circuit breaker are real.

    The first version of this harness handed the engine a fixed $10,000 with
    halted=False on every cycle of every session. That silently removed two
    things the live engine has: the daily loss cap, which stops entries once
    the session is down its limit, and compounding, which makes every
    position size a function of what the account has already made. Neither
    matters while sizing is held constant -- and both are the entire question
    the moment sizing is what is being swept.
    """
    equity = EQUITY
    realized_today = 0.0
    cap_pct = 0.06

    @classmethod
    def open_session(cls):
        cls.realized_today = 0.0

    @classmethod
    def book(cls, pnl: float):
        cls.equity += pnl
        cls.realized_today += pnl

    @classmethod
    def state(cls) -> EquityState:
        session_start = cls.equity - cls.realized_today
        limit = max(session_start, 0.0) * cls.cap_pct
        return EquityState(EQUITY, cls.equity - EQUITY, cls.equity, cls.realized_today,
                           limit, limit > 0 and cls.realized_today <= -limit)


def _patch_engine():
    """Point the engine at simulated time and account state."""
    N.datetime = _FakeDatetime
    PB.datetime = _FakeDatetime
    broker_mod.datetime = _FakeDatetime
    N.blocked_direction = _Cooldown.blocked
    N.consecutive_losses_today = _Cooldown.streak
    N.current_equity = lambda *_: _Account.state()
    # Observational only, and it would fire a live HTTP request per bar.
    N.log_price_divergence = lambda *a, **k: None
    # No chain, deliberately. Entries price from the chain since 2026-08-21,
    # but there are no HISTORICAL chains to replay -- and reaching for today's
    # would price a June entry at August's quotes. Left in, the first run of
    # this sweep filled debit spreads at an expired chain's penny asks and
    # reported +43,559 a trade. The model is the only pricing a replay can
    # use, which is precisely why a premium-selling window cannot be validated
    # by one.
    N.fetch_option_chain = lambda *a, **k: {}
    # The shadow condor reaches for the chain through data_feed directly, so
    # patching the nodes reference alone left a live HTTP call in every cycle
    # of every sweep -- one per 30-second cache window, plus a warning line
    # per session on any day the market is shut.
    import trading_engine.data_feed as DF
    DF.fetch_option_chain = lambda *a, **k: {}


def _session_vwap(bars_slice: pd.DataFrame) -> float:
    typical = (bars_slice["High"] + bars_slice["Low"] + bars_slice["Close"]) / 3.0
    return float(typical.mean())


def _session_state(bars_slice: pd.DataFrame) -> dict:
    """Run the engine's own indicator agents over the bars seen so far."""
    N.fetch_qqq_bars = lambda *a, **k: bars_slice
    # sma_agent calls Tradier for VWAP; compute it from the session's own bars
    # instead, which is what the live VWAP is meant to approximate anyway.
    N.fetch_qqq_session_vwap = lambda: _session_vwap(bars_slice)
    state = {}
    state.update(N.macd_agent({}))
    state.update(N.sma_agent({}))
    state.update(N.bollinger_agent({}))
    state.update(N.rsi_agent({}))
    state["market_sentiment"] = "GOOD"     # see the caveat in the docstring
    state["macro_halt"] = False
    state["buy_more_count"] = 0
    return state


# The full 5-minute series, and how much of it each cycle may see.
#
# The live engine calls fetch_qqq_bars(period="5d"), so a 20-period band or a
# 50 EMA at 10:15 is computed over the previous days as well as this one. The
# first version of this harness handed each session only its OWN bars, which
# left every indicator reading off nine bars at the morning entry and none of
# them warmed up at all -- a squeeze test written against it returned no rows,
# which is how it was noticed.
_HISTORY = None
_LOOKBACK_BARS = 390          # 5 sessions x 78 five-minute bars, as live


def _load_sessions(period: str = "60d") -> dict:
    global _HISTORY
    bars = yf.Ticker("QQQ").history(period=period, interval="5m")
    if bars.empty:
        raise SystemExit("yfinance returned no bars")
    bars = bars.tz_convert(NY)
    _HISTORY = bars
    return {d: g for d, g in bars.groupby(bars.index.date) if len(g) > 40}


def _seen_at(ts) -> pd.DataFrame:
    """Everything the engine would have had at `ts`, warmed up like the live feed."""
    return _HISTORY.loc[:ts].tail(_LOOKBACK_BARS)


def _mark(position, spot: float) -> float:
    if is_credit(position.strategy):
        return fill_price(estimate_credit_value(position.strategy, position.short_strike,
                                                position.long_strike, spot), "buy")
    return fill_price(estimate_spread_value(position.strategy, position.long_strike,
                                            position.short_strike, spot), "sell")


def replay_session(day_bars: pd.DataFrame, start: dtime, end: dtime) -> list:
    """One session through the engine. Returns the trades it closed."""
    _Account.open_session()
    _Cooldown.open_session()
    broker = MockBrokerClient(position=None, available_cash=_Account.equity)
    trades, entry = [], None

    for i in range(len(day_bars)):
        ts = day_bars.index[i]
        if not (start <= ts.time() <= end):
            continue
        _Clock.now = ts.to_pydatetime()
        seen = _seen_at(ts)
        spot = float(seen["Close"].iloc[-1])
        N.fetch_qqq_spot = lambda s=spot: s

        position = broker.get_open_position()
        if position is not None:
            # service.py marks the position and ratchets its peak before the
            # rules run; without both, every exit that reads the peak is blind.
            position.current_net_value = _mark(position, spot)
            position.peak_return_pct = max(position.peak_return_pct, position.return_pct)

        try:
            state = _session_state(seen)
        except Exception:
            continue

        before = broker.get_open_position()
        out = N.execution_risk_agent(state, broker=broker)
        after = broker.get_open_position()

        if before is None and after is not None:
            entry = {"ts": ts, "strategy": after.strategy, "qty": after.quantity,
                     "debit": after.entry_net_debit, "playbook": after.playbook}
        elif before is not None and after is None and entry is not None:
            closed = _close(entry, before, spot, ts, out.get("exit_reason", ""))
            _Account.book(closed["pnl"])
            _Cooldown.book(closed["strategy"], closed["pnl"], ts)
            trades.append(closed)
            entry = None

    position = broker.get_open_position()
    if position is not None and entry is not None:
        spot = float(day_bars["Close"].iloc[-1])
        closed = _close(entry, position, spot, day_bars.index[-1], "EOD")
        _Account.book(closed["pnl"])
        trades.append(closed)
    return trades



def _run_arm(sessions: dict, start: dtime, end: dtime = dtime(15, 45)):
    """One configuration across every session, from a clean account.

    Every sweep arm goes through here. The alternative -- each sweep looping
    over sessions itself -- is how several arms ended up inheriting the
    previous arm's compounded equity, which made a parameter look responsible
    for P&L that was really just a larger starting balance. The giveback
    sweep reported +32.50 to +58.58 a trade across four arms whose exit mixes
    were identical to the trade, which is what gave it away.
    """
    _Account.equity = EQUITY
    trades, per_day = [], []
    for day, bars in sessions.items():
        day_trades = replay_session(bars, start, end)
        trades += day_trades
        per_day.append({"day": day, "pnl": sum(t["pnl"] for t in day_trades),
                        "trades": len(day_trades)})
    return trades, per_day

def _close(entry: dict, position, spot: float, ts, reason: str) -> dict:
    exit_value = _mark(position, spot)
    per = ((entry["debit"] - exit_value) if is_credit(entry["strategy"])
           else (exit_value - entry["debit"]))
    per -= SLIPPAGE_ROUNDTRIP
    return {**entry, "exit_ts": ts, "exit_value": exit_value,
            "pnl": round(per * entry["qty"] * 100, 2),
            "pct": round(per / entry["debit"] * 100, 2) if entry["debit"] else 0.0,
            "reason": reason}


def _report(label: str, trades: list, sessions: int):
    if not trades:
        print(f"  {label:32s} no trades in {sessions} sessions")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    total = sum(t["pnl"] for t in trades)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    # Split halves, because an average that reverses between them is a result
    # about two samples rather than about the parameter.
    half = len(trades) // 2
    h1 = sum(t["pnl"] for t in trades[:half]) / max(half, 1)
    h2 = sum(t["pnl"] for t in trades[half:]) / max(len(trades) - half, 1)
    print(f"  {label:32s} {len(trades):3d} tr  {len(wins) / len(trades) * 100:3.0f}% win  "
          f"{total / len(trades):+8.2f}/tr  {total:+9.2f} tot  "
          f"worst {min(t['pnl'] for t in trades):+8.2f}  halves {h1:+7.2f}/{h2:+7.2f}  {reasons}")


def sweep_morning(sessions: dict):
    print("\nMORNING_DRIFT -- width x long-leg depth, entries 10:15-11:30\n")
    base = PB.WINDOWS
    for width in (3.0, 4.0, 5.0, 6.0):
        for depth in (None, 2.0):
            PB.WINDOWS = tuple(
                replace(w, width=width, long_depth=depth) if w.name == "MORNING_DRIFT" else w
                for w in base
            )
            PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
            trades, _ = _run_arm(sessions, dtime(10, 15))
            depth_label = "deep (long = width)" if depth is None else f"long ${depth:.0f} ITM"
            _report(f"${width:.0f} wide, {depth_label}", trades, len(sessions))
    PB.WINDOWS = base


def sweep_windows(sessions: dict):
    """Each window traded alone, so no window can hide inside another's P&L.

    Read the credit row with the caveat in mind: it sells far-out-of-the-money
    premium, which is exactly where broker.py's model was measured wrong on
    2026-08-21 -- 0.30 modelled against 0.02 quoted. There are no historical
    chains to replay, so a backtest of a premium-selling window cannot be
    trusted in absolute terms. Its forward record, priced from the chain since
    2026-08-21, is the evidence that counts.
    """
    print("")
    print("EVERY WINDOW SOLO -- 60 sessions")
    print("")
    base = PB.WINDOWS
    for w in base:
        PB.ENABLED_WINDOWS = frozenset({w.name})
        trades, _ = _run_arm(sessions, w.start)
        flag = "  [MODEL-PRICED, see docstring]" if w.placement == "CREDIT" else ""
        _report(f"{w.name} ${w.width:.0f} {w.placement}{flag}", trades, len(sessions))
    PB.WINDOWS = base


def sweep_retries(sessions: dict):
    """Does letting the morning window stay open pay for a second entry?

    Today's move went 709.40 at 10:15 to 715 by 11:50 and the engine caught
    it at 11:01, when the fourth confirmation finally arrived. It then rode
    to the 13:25 handoff. Nothing could re-enter, because MORNING_DRIFT
    closes at 11:30 -- so a setup that re-fires at noon has no window to fire
    into. This sweeps that end time.
    """
    print("")
    print("MORNING WINDOW LENGTH -- how late may a debit entry still open?")
    print("")
    base = PB.WINDOWS
    for end in (dtime(11, 30), dtime(12, 30), dtime(13, 0), dtime(13, 25)):
        PB.WINDOWS = tuple(
            replace(w, end=end) if w.name == "MORNING_DRIFT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
        trades, _ = _run_arm(sessions, dtime(10, 15))
        _report(f"entries until {end.strftime('%H:%M')}", trades, len(sessions))
    PB.WINDOWS = base


def _report_daily(label: str, per_day: list):
    """Per SESSION, not per trade -- the only frame in which "more trades"
    can be judged, since a rule that adds trades usually adds worse ones."""
    if not per_day:
        print(f"  {label:34s} nothing traded")
        return
    days = len(per_day)
    total = sum(d["pnl"] for d in per_day)
    trades = sum(d["trades"] for d in per_day)
    green = len([d for d in per_day if d["pnl"] > 0])
    half = days // 2
    h1 = sum(d["pnl"] for d in per_day[:half]) / max(half, 1)
    h2 = sum(d["pnl"] for d in per_day[half:]) / max(days - half, 1)
    print(f"  {label:34s} {days:2d} days  {trades / days:4.2f} tr/day  "
          f"{total / days:+8.2f}/day  {total:+9.2f} tot  {green / days * 100:3.0f}% green days  "
          f"worst {min(d['pnl'] for d in per_day):+8.2f}  halves {h1:+7.2f}/{h2:+7.2f}")


def _run_one_side(bars, i: int, offset: float, wing: float, calls: bool = True):
    """One credit vertical, priced and exited exactly like _run_condor's."""
    spot = float(bars["Close"].iloc[i])
    atm = round_to_strike(spot)
    if calls:
        short, long_ = atm + offset, atm + offset + wing
        strat = CALL_CREDIT_SPREAD
    else:
        short, long_ = atm - offset, atm - offset - wing
        strat = PUT_CREDIT_SPREAD
    mins = broker_mod.minutes_to_expiry(bars.index[i].to_pydatetime())
    credit = fill_price(estimate_credit_value(strat, short, long_, spot, mins), "sell")
    if credit <= 0.02:
        return None
    pnl, reason = None, None
    for j in range(i + 1, len(bars)):
        ts = bars.index[j]
        if ts.time() > dtime(15, 45):
            break
        sp = float(bars["Close"].iloc[j])
        m = broker_mod.minutes_to_expiry(ts.to_pydatetime())
        cost = fill_price(estimate_credit_value(strat, short, long_, sp, m), "buy")
        pnl, reason = (credit - cost) * 100, "FORCE_CLOSE"
        if cost <= credit * 0.10:
            reason = "TARGET"
            break
        if cost >= credit * 2.0:
            reason = "STOP"
            break
    if pnl is None:
        return None
    return {"pnl": round(pnl, 2), "reason": reason, "pct": round((pnl / 100) / credit * 100, 2)}


def sweep_macro(sessions: dict):
    """Do yields, crude and the VIX predict the engine's day?

    The engine fetches all three every cycle and logs them without gating on
    any of them -- deliberately, since nothing had measured whether they
    carry information. This buckets the engine's own daily P&L by each, which
    is the only question that matters: not whether crude moves QQQ, but
    whether knowing crude's move would have changed what we should trade.
    """
    print("")
    print("MACRO INPUTS -- engine P&L per day, bucketed by each input's terciles")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    _, per_day = _run_arm(sessions, dtime(9, 45))
    pnl_by_day = {d["day"]: d["pnl"] for d in per_day}

    # Intraday first, because the daily version is lookahead. Bucketing a
    # session by its FULL-DAY yield change scores the engine on information
    # that did not exist when it entered at 10:15, and the effect it shows --
    # falling yields +171.57 a day against rising yields -22.78 -- is mostly
    # a restatement of "QQQ went up today", which is not tradeable in
    # advance. What is observable at the decision is the move so far.
    print("  observable at the decision (session open to the entry bar):")
    for sym, label in (("^TNX", "10Y yield"), ("CL=F", "crude"), ("^VIX", "VIX")):
        try:
            intra = yf.Ticker(sym).history(period="60d", interval="5m")
            if intra.empty:
                print(f"    {label}: no intraday bars")
                continue
            intra = intra.tz_convert(NY)
        except Exception as e:
            print(f"    {label}: intraday fetch failed ({e})")
            continue
        for cutoff, when in ((dtime(10, 15), "by 10:15"), (dtime(13, 30), "by 13:30")):
            rows = []
            for day, pnl in pnl_by_day.items():
                bars = intra[intra.index.map(lambda t: t.date() == day)]
                bars = bars[bars.index.map(lambda t: t.time() <= cutoff)]
                if len(bars) < 3:
                    continue
                first, last = float(bars["Close"].iloc[0]), float(bars["Close"].iloc[-1])
                if first == 0:
                    continue
                rows.append({"chg": (last - first) / first * 100.0, "pnl": pnl})
            if len(rows) < 9:
                continue
            rows.sort(key=lambda r: r["chg"])
            third = len(rows) // 3
            out = []
            for name, g in (("down", rows[:third]), ("flat", rows[third:2 * third]),
                            ("up  ", rows[2 * third:])):
                avg = sum(r["pnl"] for r in g) / len(g)
                green = len([r for r in g if r["pnl"] > 0]) / len(g) * 100
                out.append(f"{name} [{g[0]['chg']:+.2f}..{g[-1]['chg']:+.2f}] {avg:+7.2f}/day {green:3.0f}%")
            print(f"    {label:10s} {when:9s} n={len(rows):2d}  " + "   ".join(out))
    print("")
    print("  full-day change, for contrast -- NOT usable, it contains the future:")

    series = {}
    for sym, label in (("^TNX", "10Y yield"), ("CL=F", "crude"), ("^VIX", "VIX")):
        try:
            hist = yf.Ticker(sym).history(period="6mo", interval="1d")
            if hist.empty:
                continue
            series[label] = hist
        except Exception as e:
            print(f"  {label}: fetch failed ({e})")

    qqq_daily = yf.Ticker("QQQ").history(period="6mo", interval="1d")

    for label, hist in series.items():
        rows = []
        closes = hist["Close"]
        for day, pnl in pnl_by_day.items():
            same = [i for i, ts in enumerate(closes.index) if ts.date() == day]
            if not same or same[0] == 0:
                continue
            i = same[0]
            prev, now = float(closes.iloc[i - 1]), float(closes.iloc[i])
            if prev == 0:
                continue
            rows.append({"day": day, "chg": (now - prev) / prev * 100.0,
                         "level": now, "pnl": pnl})
        if len(rows) < 9:
            print(f"  {label}: only {len(rows)} sessions matched, skipping")
            continue
        for key, kind in (("chg", "change vs prior close"), ("level", "level")):
            rows.sort(key=lambda r: r[key])
            third = len(rows) // 3
            groups = (("low ", rows[:third]), ("mid ", rows[third:2 * third]),
                      ("high", rows[2 * third:]))
            out = []
            for name, g in groups:
                avg = sum(r["pnl"] for r in g) / len(g)
                green = len([r for r in g if r["pnl"] > 0]) / len(g) * 100
                span = f"{g[0][key]:.2f}..{g[-1][key]:.2f}"
                out.append(f"{name} [{span}] {avg:+7.2f}/day {green:3.0f}% green")
            print(f"  {label:10s} {kind:22s} " + "   ".join(out))
    print("")
    print("  For reference, QQQ's own next-day move is the thing all three are")
    print("  supposed to anticipate; the engine's P&L is what it actually needs.")


def sweep_placement(sessions: dict):
    """Morning width and depth, judged on daily P&L with the credit window on.

    The per-trade sweep leaves this genuinely ambiguous -- deep placements
    win more often and lose less on their worst trade, shallow ones average
    more with steadier halves, and the sample is 24 trades. Per-trade is also
    the wrong frame for the decision: the morning window hands its slot to
    the credit window at 13:25, so a placement that stops out early frees the
    slot and one that rides does not.
    """
    print("")
    print("MORNING PLACEMENT -- per day, credit window live")
    print("")
    base = PB.WINDOWS
    for width, depth, label in (
        (5.0, None, "$5 deep (live)"),
        (5.0, 2.0, "$5 long $2 ITM"),
        (6.0, 2.0, "$6 long $2 ITM"),
        (4.0, None, "$4 deep"),
        (6.0, None, "$6 deep"),
    ):
        PB.WINDOWS = tuple(
            replace(w, width=width, long_depth=depth) if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)
    PB.WINDOWS = base


def sweep_condorvs(sessions: dict):
    """Both sides against the one side the engine already sells.

    Same entry bar, same wings, same exits, same model -- so the model's
    overstatement of far-OTM premium is common to both arms and cancels in
    the comparison. What does not cancel is the structural trade: a condor
    collects roughly twice the credit and needs BOTH strikes to hold, and the
    survival table already prices that at 92% against 75% for a four-dollar
    offset at 13:30.
    """
    print("")
    print("CONDOR vs ONE SIDE -- same entry, same wings, same exits, per contract")
    print("")
    for entry in (dtime(13, 30), dtime(14, 30)):
        for offset in (3.0, 4.0, 5.0):
            arms = {"call side only": [], "put side only": [], "condor (both)": []}
            for bars in sessions.values():
                hits = [i for i, ts in enumerate(bars.index) if ts.time() == entry]
                if not hits:
                    continue
                i = hits[0]
                for label, fn in (("call side only", lambda: _run_one_side(bars, i, offset, 3.0, True)),
                                  ("put side only", lambda: _run_one_side(bars, i, offset, 3.0, False)),
                                  ("condor (both)", lambda: _run_condor(bars, i, offset))):
                    r = fn()
                    if r:
                        arms[label].append(r)
            print(f"  {entry.strftime('%H:%M')} entry, ${offset:.0f} out:")
            for label, res in arms.items():
                _report("    " + label, res, len(sessions))


def sweep_trenddepth(sessions: dict):
    """Should a strong trend get a shallower long leg, and a higher ceiling?"""
    print("")
    print("TREND-CONDITIONAL PLACEMENT -- shallow long leg when ADX confirms")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    for depth, adx in ((0.0, 0), (2.0, 22), (2.0, 28), (3.0, 22), (3.0, 28)):
        N.TRENDING_LONG_DEPTH, N.TRENDING_ADX_MIN = depth, adx
        _, per_day = _run_arm(sessions, dtime(9, 45))
        label = "deep always (current)" if depth == 0 else f"long ${depth:.0f} ITM when ADX>={adx}"
        _report_daily(label, per_day)
    N.TRENDING_LONG_DEPTH = 0.0


def sweep_daytrend(sessions: dict):
    """How far down a session may be before longs are refused."""
    print("")
    print("SESSION TREND FILTER -- refuse bullish entries below this drop from the open")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    for drop in (0.0, 0.25, 0.5, 0.75, 1.0):
        N.DAY_TREND_MAX_DROP_PCT = drop
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily("off (current)" if drop == 0 else f"refuse longs below -{drop:.2f}%",
                      per_day)
    N.DAY_TREND_MAX_DROP_PCT = 0.0


def sweep_daytype(sessions: dict):
    """What the engine earns on days that go down, sideways and up.

    A backtest average hides regime. If the whole result comes from up days,
    a week of selling tells you nothing until it arrives -- and the question
    of whether anything covers a session that opens bad and keeps going is
    answered by looking at those sessions specifically, not at the mean.

    Buckets are the move from the 09:45 bar to the close, which is the span
    the engine can actually trade.
    """
    print("")
    print("BY DAY TYPE -- 09:45 to close, engine P&L per session")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    _Account.equity = EQUITY
    rows = []
    for day, bars in sessions.items():
        opens = [i for i, ts in enumerate(bars.index) if ts.time() >= dtime(9, 45)]
        if not opens:
            continue
        start_px = float(bars["Close"].iloc[opens[0]])
        end_px = float(bars["Close"].iloc[-1])
        move = end_px - start_px
        trades = replay_session(bars, dtime(9, 45), dtime(15, 45))
        rows.append({"move": move, "pct": move / start_px * 100,
                     "pnl": sum(t["pnl"] for t in trades), "trades": trades,
                     "n": len(trades)})

    buckets = [
        ("hard down  (< -0.75%)", lambda r: r["pct"] < -0.75),
        ("down       (-0.75..-0.25)", lambda r: -0.75 <= r["pct"] < -0.25),
        ("flat       (-0.25..+0.25)", lambda r: -0.25 <= r["pct"] <= 0.25),
        ("up         (+0.25..+0.75)", lambda r: 0.25 < r["pct"] <= 0.75),
        ("hard up    (> +0.75%)", lambda r: r["pct"] > 0.75),
    ]
    for label, test in buckets:
        group = [r for r in rows if test(r)]
        if not group:
            print(f"  {label:26s} no sessions")
            continue
        total = sum(r["pnl"] for r in group)
        green = len([r for r in group if r["pnl"] > 0])
        flat_days = len([r for r in group if r["n"] == 0])
        kinds = {}
        for r in group:
            for t in r["trades"]:
                kinds[t["strategy"]] = kinds.get(t["strategy"], 0) + 1
        print(f"  {label:26s} n={len(group):2d}  {total / len(group):+8.2f}/day  "
              f"{total:+9.2f} tot  {green}/{len(group)} green  "
              f"{flat_days} untraded  {kinds}")


def sweep_squeeze(sessions: dict):
    """Does a Bollinger squeeze mark a morning where short strikes survive?

    The claim behind a "neutral day iron condor" is that contracting bands,
    flat moving averages and a mid-range RSI identify a session that will go
    nowhere. That is a testable prediction about price, not about premium, so
    it needs no pricing model: split the sessions by how tight the bands are
    at the entry bar and compare how often each group's short strikes hold.
    """
    print("")
    print("VOLATILITY SQUEEZE -- share of sessions where the strike is never touched")
    print("   split by 20-period band width at the entry bar (tightest third vs rest)")
    print("")
    for entry in (dtime(9, 45), dtime(10, 15)):
        rows = []
        for bars in sessions.values():
            hits = [i for i, ts in enumerate(bars.index) if ts.time() == entry]
            if not hits:
                continue
            i = hits[0]
            close = _seen_at(bars.index[i])["Close"]
            if len(close) < 20:
                continue
            sd = float(close.rolling(20).std().iloc[-1])
            spot = float(close.iloc[-1])
            rest = bars.iloc[i + 1:]
            rest = rest[rest.index.map(lambda t: t.time() <= dtime(15, 45))]
            if rest.empty or sd != sd:
                continue
            rows.append({"sd": sd, "spot": spot,
                         "high": float(rest["High"].max()), "low": float(rest["Low"].min())})
        if not rows:
            continue
        rows.sort(key=lambda r: r["sd"])
        cut = max(len(rows) // 3, 1)
        groups = (("squeezed (tightest 3rd)", rows[:cut]), ("everything else", rows[cut:]))
        print(f"  {entry.strftime('%H:%M')} entry")
        for label, group in groups:
            out = []
            for offset in (3.0, 4.0, 5.0, 6.0):
                held = sum(1 for r in group
                           if r["high"] < r["spot"] + offset and r["low"] > r["spot"] - offset)
                out.append(f"${offset:.0f}: {held / len(group) * 100:3.0f}%")
            band = sum(r["sd"] for r in group) / len(group)
            print(f"    {label:26s} n={len(group):2d}  avg 20-bar sd {band:4.2f}   "
                  + "   ".join(out))


def sweep_bearside(sessions: dict):
    """May the morning window trade its short side?

    It is forbidden today on a measurement taken before this harness existed:
    16 bearish-stack mornings, ITM put spreads at -15.52 a trade, and a sign
    that moved when the judging bar moved. The window has changed since --
    $5 wide, deeper long leg, chain pricing, a different exit ladder -- so
    the prohibition deserves re-testing against the engine that exists now.
    """
    print("")
    print("MORNING SHORT SIDE -- long-only against both directions")
    print("")
    base = PB.WINDOWS
    for bull_only in (True, False):
        PB.WINDOWS = tuple(
            replace(w, bullish_only=bull_only) if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
        _Account.equity = EQUITY
        trades, _ = _run_arm(sessions, dtime(10, 15))
        _report("long only (current)" if bull_only else "both directions", trades, len(sessions))
        longs = [t for t in trades if t["strategy"] == "BULL_CALL_SPREAD"]
        shorts = [t for t in trades if t["strategy"] == "BEAR_PUT_SPREAD"]
        if shorts:
            _report("   ...of which long", longs, len(sessions))
            _report("   ...of which short", shorts, len(sessions))
    PB.WINDOWS = base
    _Account.equity = EQUITY


def sweep_ratchetstyle(sessions: dict):
    """A fixed offset from the peak, against a share of it.

    The engine gives back max(peak x share, floor) in RETURN POINTS. A fixed
    dollar offset is the same rule with the share switched off -- $0.25 on a
    $3.30 entry is 7.6 points, and it stays 7.6 points whether the trade has
    made 10% or 200%. That is the whole difference: a share widens as the
    position wins, a fixed offset does not.
    """
    print("")
    print("RATCHET STYLE -- fixed points from peak (a dollar offset) vs share of peak")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    base_share, base_floor = N.TRAIL_GIVEBACK, N.MIN_GIVEBACK_PCT
    base_windows = PB.WINDOWS
    for share, floor, label in (
        (0.30, base_floor, "30% of peak (current)"),
        (0.0, 5.0, "fixed 5 points"),
        (0.0, 7.6, "fixed 7.6 pts (= $0.25 on $3.30)"),
        (0.0, 10.0, "fixed 10 points"),
        (0.0, 20.0, "fixed 20 points"),
        (0.0, 30.0, "fixed 30 points"),
    ):
        # The credit window pins its own giveback, so the global alone would
        # change nothing -- which is exactly what the first run of this sweep
        # reported, five identical rows.
        PB.WINDOWS = tuple(
            replace(w, ratchet_giveback=share) if w.name == "AFTERNOON_CREDIT" else w
            for w in base_windows
        )
        N.TRAIL_GIVEBACK, N.MIN_GIVEBACK_PCT = share, floor
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)
    N.TRAIL_GIVEBACK, N.MIN_GIVEBACK_PCT = base_share, base_floor
    PB.WINDOWS = base_windows
    _Account.equity = EQUITY


def sweep_cooldown(sessions: dict):
    """How long to refuse the side that just lost, and how big the morning is.

    The cooldown was measured once before, on a different configuration and
    with a different tool. Re-measuring it here is not duplication: the
    engine it governs has changed underneath it -- new widths, chain pricing,
    a different ratchet -- and a stand-down length is only meaningful against
    the trades it is standing down from.
    """
    print("")
    print("POST-LOSS COOLDOWN -- minutes the losing side is refused")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
    for minutes in (0.0, 15.0, 30.0, 60.0, 120.0):
        _Cooldown.minutes = minutes
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(f"{minutes:.0f} min" + (" (current)" if minutes == 30 else ""),
                      per_day)
    _Cooldown.minutes = 30.0

    print("")
    print("MORNING SIZE -- capital the debit window may deploy")
    print("")
    base = PB.WINDOWS
    for fraction in (0.04, 0.08, 0.12, 0.20):
        PB.WINDOWS = tuple(
            replace(w, entry_fraction=fraction) if w.name == "MORNING_DRIFT" else w
            for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(f"morning fraction {fraction:.0%}" + (" (current)" if fraction == 0.04 else ""),
                      per_day)
    PB.WINDOWS = base
    _Account.equity = EQUITY


def sweep_daily(sessions: dict):
    """Would trading more per day have earned more per day?

    Every rule tested so far that ADDS trades has cost money: a longer
    morning window took +21.58 a trade to +1.74, a looser credit gate took
    89% wins to 76%, a longer credit window left the total flat. This asks
    the question at the level it was posed -- whole sessions, all windows
    switched on together, one position at a time as the engine actually runs.
    """
    print("")
    print("TRADES PER DAY vs DOLLARS PER DAY -- 60 sessions, one position at a time")
    print("")
    combos = [
        ("credit only", {"AFTERNOON_CREDIT"}),
        ("morning only", {"MORNING_DRIFT"}),
        ("morning + credit (current)", {"MORNING_DRIFT", "AFTERNOON_CREDIT"}),
        ("+ midday grinder", {"MORNING_DRIFT", "ITM_GRINDER", "AFTERNOON_CREDIT"}),
        ("all four windows", {w.name for w in PB.WINDOWS}),
    ]
    for label, names in combos:
        PB.ENABLED_WINDOWS = frozenset(names)
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(label, per_day)


def sweep_size(sessions: dict):
    """Same trades, more contracts: where does size stop paying?

    P&L scales linearly with contracts and risk does not, because the daily
    loss cap is a fixed share of equity: past some size one bad session hits
    the cap, entries stop, and the rest of that day's opportunities are
    forfeited. That break is what this looks for, and it is only visible now
    that the harness carries equity and halts like the live engine.
    """
    print("")
    print("SIZE -- credit window's share of the daily risk budget")
    print("")
    base = PB.WINDOWS
    for share in (0.20, 0.35, 0.50, 0.80):
        PB.WINDOWS = tuple(
            replace(w, risk_share=share) if w.name == "AFTERNOON_CREDIT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(f"credit share {share:.0%}" + (" (current)" if share == 0.20 else ""),
                      per_day)
        print(f"{'':36s} ending equity ${_Account.equity:,.2f}")
    PB.WINDOWS = base

    # Past 35% the risk slice stops binding and the ENTRY FRACTION does --
    # the slice permits contracts the capital allocation cannot buy. So the
    # second half of the size question is how much capital an entry may
    # deploy, held at a slice loose enough not to interfere.
    print("")
    print("SIZE -- capital an entry may deploy, credit slice held at 50%")
    print("")
    base_fraction = N.ENTRY_FRACTION
    PB.WINDOWS = tuple(
        replace(w, risk_share=0.50) if w.name == "AFTERNOON_CREDIT" else w for w in base
    )
    for fraction in (0.10, 0.15, 0.20, 0.30):
        N.ENTRY_FRACTION = fraction
        PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT", "AFTERNOON_CREDIT"})
        _, per_day = _run_arm(sessions, dtime(9, 45))
        _report_daily(f"entry fraction {fraction:.0%}" + (" (current)" if fraction == 0.10 else ""),
                      per_day)
        print(f"{'':36s} ending equity ${_Account.equity:,.2f}")
    N.ENTRY_FRACTION = base_fraction
    PB.WINDOWS = base
    _Account.equity = EQUITY


def sweep_creditexit(sessions: dict):
    """Where the credit window's P&L is actually decided.

    Its exits split 47% ratchet, 28% force close, 14% target, 9% stop -- so
    the ratchet's giveback is the single most consequential number in the
    window, and it was inherited from the debit side rather than chosen here.
    Window length is the second: a trade still open at 15:00 keeps running,
    but a NEW one cannot start, and today's closed at 15:19 with the window
    long shut.

    Same structure in every arm, so the model's premium bias cancels.
    """
    print("")
    print("CREDIT RATCHET GIVEBACK -- share of peak handed back before booking")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
    base_give, base_min = N.TRAIL_GIVEBACK, N.MIN_GIVEBACK_PCT
    base_w = PB.WINDOWS
    for give in (0.15, 0.20, 0.30, 0.50):
        # The window pins its own giveback, so setting the global alone tests
        # nothing -- which is exactly what this sweep reported once the
        # account bug was fixed: four identical rows, to the cent.
        PB.WINDOWS = tuple(
            replace(w, ratchet_giveback=give) if w.name == "AFTERNOON_CREDIT" else w
            for w in base_w
        )
        N.TRAIL_GIVEBACK = give
        trades, _ = _run_arm(sessions, dtime(13, 30))
        _report(f"giveback {give:.0%}" + (" (current)" if give == 0.20 else ""),
                trades, len(sessions))
    N.TRAIL_GIVEBACK = base_give
    PB.WINDOWS = base_w

    print("")
    print("CREDIT WINDOW LENGTH -- how late a NEW credit entry may open")
    print("")
    base_windows = PB.WINDOWS
    for end in (dtime(15, 0), dtime(15, 15), dtime(15, 30)):
        PB.WINDOWS = tuple(
            replace(w, end=end) if w.name == "AFTERNOON_CREDIT" else w for w in base_windows
        )
        PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
        trades, _ = _run_arm(sessions, dtime(13, 30))
        _report(f"entries until {end.strftime('%H:%M')}" + (" (current)" if end == dtime(15, 0) else ""),
                trades, len(sessions))
    PB.WINDOWS = base_windows


def sweep_creditgate(sessions: dict):
    """Does making the credit window wait for a directional tier pay for it?

    Both arms trade the same structure at the same width, so the model's
    known overstatement of far-OTM premium is common to them and cancels in
    the comparison -- which is the one thing a model-priced replay can still
    do honestly.
    """
    print("")
    print("CREDIT ENTRY GATE -- directional tier vs trend-only (THETA)")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
    for loose in (False, True):
        N.CREDIT_LOOSE_GATE = loose
        trades, _ = _run_arm(sessions, dtime(13, 30))
        if trades:
            mins = [t["ts"].hour * 60 + t["ts"].minute for t in trades]
            avg = sum(mins) / len(mins)
            when = f"avg entry {int(avg // 60):02d}:{int(avg % 60):02d}"
        else:
            when = ""
        _report(("trend-only gate" if loose else "directional tier (current)"),
                trades, len(sessions))
        print(f"{'':34s} {when}")
    N.CREDIT_LOOSE_GATE = False


def sweep_rideratchet(sessions: dict):
    """Should a riding position protect a gain, and from what level?

    A ride keeps only its stop and its deadline, which is what let the
    2026-08-21 morning trade peak at +26.4% and close at +16.7%. The counter-
    argument is measured too: booking rides at a fixed target earned +18.82 a
    trade against +36.40 for letting them run. A ratchet is the middle -- it
    only acts after a gain exists -- and the arm level decides whether it
    protects winners or truncates them.
    """
    print("")
    print("RIDE RATCHET -- arm level for a riding position (giveback 20%, floor 5 points)")
    print("")
    PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
    for arm in (0.0, 12.0, 18.0, 25.0, 32.0):
        N.RIDE_RATCHET_ARM_PCT = arm
        trades, _ = _run_arm(sessions, dtime(10, 15))
        label = "off (current)" if arm == 0 else f"arm at +{arm:.0f}%"
        _report(label, trades, len(sessions))
    N.RIDE_RATCHET_ARM_PCT = 0.0


def sweep_breach(sessions: dict):
    """How often a short strike survives -- from bars alone, no pricing.

    The one question about credit spreads and condors a replay can still
    answer honestly. Whether the model prices premium correctly is a separate
    argument; whether QQQ travels $4 from 13:30 is a fact in the bars. Combine
    a hold rate here with a real credit from the chain and the expected value
    follows.
    """
    print("")
    print("STRIKE SURVIVAL -- share of sessions where the short strike is never touched")
    print("   one-sided = a call short above spot; condor = either side breached")
    print("")
    for entry in (dtime(9, 45), dtime(10, 15), dtime(13, 30), dtime(14, 30)):
        row = []
        for offset in (2.0, 3.0, 4.0, 5.0, 6.0):
            held_call = held_condor = total = 0
            for bars in sessions.values():
                hits = [i for i, ts in enumerate(bars.index) if ts.time() == entry]
                if not hits:
                    continue
                i = hits[0]
                spot = float(bars["Close"].iloc[i])
                rest = bars.iloc[i + 1:]
                rest = rest[rest.index.map(lambda t: t.time() <= dtime(15, 45))]
                if rest.empty:
                    continue
                total += 1
                up_ok = float(rest["High"].max()) < spot + offset
                down_ok = float(rest["Low"].min()) > spot - offset
                held_call += 1 if up_ok else 0
                held_condor += 1 if (up_ok and down_ok) else 0
            if total:
                row.append(f"${offset:.0f}: {held_call / total * 100:3.0f}% / {held_condor / total * 100:3.0f}%")
        print(f"  {entry.strftime('%H:%M')} entry   " + "   ".join(row))


def sweep_credit(sessions: dict):
    print("\nAFTERNOON_CREDIT -- width, entries 13:30-15:00\n")
    base = PB.WINDOWS
    for width in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
        PB.WINDOWS = tuple(
            replace(w, width=width) if w.name == "AFTERNOON_CREDIT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
        trades, _ = _run_arm(sessions, dtime(13, 30))
        _report(f"${width:.0f} wide", trades, len(sessions))
    PB.WINDOWS = base


def _adx(bars: pd.DataFrame, period: int = 14):
    if len(bars) < period * 2:
        return None
    h, l, c = bars["High"], bars["Low"], bars["Close"]
    up, down = h.diff(), -l.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    mdi = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return float(dx.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])


def sweep_condor(sessions: dict):
    """The condor, priced the way the engine prices everything else.

    Not run through execution_risk_agent -- the engine has no condor
    structure to execute. This prices the same four legs the shadow log
    marks, entered at a fixed time, exited on a 90% decay target or a 2x
    credit stop, held to the force close otherwise. ADX is a variant because
    it is the gate the structure has been recommended behind.
    """
    print("\nIRON CONDOR -- 3-wide wings, 90% decay target, 2x credit stop\n")
    for entry_time in (dtime(9, 45), dtime(10, 15), dtime(13, 30)):
        for offset in (3.0, 4.0, 5.0):
            for adx_max in (None, 22.0):
                results = []
                for bars in sessions.values():
                    hits = [i for i, ts in enumerate(bars.index) if ts.time() == entry_time]
                    if not hits:
                        continue
                    i = hits[0]
                    if adx_max is not None:
                        adx = _adx(_seen_at(bars.index[i]))
                        if adx is None or adx >= adx_max:
                            continue
                    result = _run_condor(bars, i, offset)
                    if result:
                        results.append(result)
                tag = entry_time.strftime("%H:%M") + f" ${offset:.0f} out" + (
                    f", ADX<{adx_max:.0f}" if adx_max else ", unfiltered")
                _report(tag, results, len(sessions))


def _run_condor(bars: pd.DataFrame, i: int, offset: float, wing: float = 3.0) -> "dict | None":
    spot = float(bars["Close"].iloc[i])
    atm = round_to_strike(spot)
    cs, cl = atm + offset, atm + offset + wing
    ps, pl = atm - offset, atm - offset - wing
    mins = broker_mod.minutes_to_expiry(bars.index[i].to_pydatetime())
    credit = (fill_price(estimate_credit_value(CALL_CREDIT_SPREAD, cs, cl, spot, mins), "sell")
              + fill_price(estimate_credit_value(PUT_CREDIT_SPREAD, ps, pl, spot, mins), "sell"))
    if credit <= 0.02:
        return None

    pnl, reason = None, None
    for j in range(i + 1, len(bars)):
        ts = bars.index[j]
        if ts.time() > dtime(15, 45):
            break
        s = float(bars["Close"].iloc[j])
        m = broker_mod.minutes_to_expiry(ts.to_pydatetime())
        cost = (fill_price(estimate_credit_value(CALL_CREDIT_SPREAD, cs, cl, s, m), "buy")
                + fill_price(estimate_credit_value(PUT_CREDIT_SPREAD, ps, pl, s, m), "buy"))
        pnl, reason = (credit - cost) * 100, "FORCE_CLOSE"
        if cost <= credit * 0.10:
            reason = "TARGET"
            break
        if cost >= credit * 2.0:
            reason = "STOP"
            break
    if pnl is None:
        return None
    return {"pnl": round(pnl, 2), "reason": reason, "pct": round((pnl / 100) / credit * 100, 2)}


def _bs_call(spot: float, strike: float, years: float, iv: float) -> tuple:
    """Black-Scholes call price and delta, zero rate, zero dividend.

    broker.py prices VERTICALS with a CDF on the spread's midpoint, which has
    no single-leg equivalent -- so a single call needs its own pricer. Zero
    rate is a rounding error over three days.
    """
    import math
    if years <= 0:
        intrinsic = max(spot - strike, 0.0)
        return intrinsic, (1.0 if spot > strike else 0.0)
    vt = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / vt
    d2 = d1 - vt
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    nd2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    return spot * nd1 - strike * nd2, nd1


def sweep_orb(sessions: dict, iv: float = 0.185, dte: float = 3.0):
    """Buy a 60-delta weekly call when QQQ breaks the 09:30-09:45 high.

    Nothing here touches the engine -- this is a different instrument (one
    long call, not a vertical) and a different entry (opening-range break,
    not the CLEAN stack), so it is simulated on its own terms and judged
    against what the engine actually earns.

    IV is held constant at the level measured on the live chain for this
    tenor. That is the sim's main weakness: a real breakout often comes with
    a small IV bid, which would help the winners slightly.
    """
    print("")
    print(f"OPENING RANGE BREAK -> 60-delta call, {dte:.0f} DTE, IV {iv:.3f}")
    print("   +20% target / -15% stop, as the note specifies")
    print("")
    for exit_by in (dtime(12, 0), dtime(15, 55)):
        results = []
        for bars in sessions.values():
            opening = [i for i, ts in enumerate(bars.index) if dtime(9, 30) <= ts.time() < dtime(9, 45)]
            if not opening:
                continue
            or_high = float(bars["High"].iloc[opening].max())
            entry_i = None
            for i, ts in enumerate(bars.index):
                if ts.time() < dtime(9, 45) or ts.time() >= exit_by:
                    continue
                if float(bars["Close"].iloc[i]) > or_high:
                    entry_i = i
                    break
            if entry_i is None:
                continue
            spot = float(bars["Close"].iloc[entry_i])
            # The strike whose delta is nearest 0.60, on the listed $1 grid.
            strike = min((round(spot) + k for k in range(-8, 3)),
                         key=lambda K: abs(_bs_call(spot, K, dte / 365.0, iv)[1] - 0.60))
            entry_px, _ = _bs_call(spot, strike, dte / 365.0, iv)
            if entry_px <= 0:
                continue
            entry_ts = bars.index[entry_i]
            pnl_pct, reason = None, None
            for j in range(entry_i + 1, len(bars)):
                ts = bars.index[j]
                if ts.time() >= exit_by:
                    break
                elapsed_days = (ts - entry_ts).total_seconds() / 86400.0
                px, _ = _bs_call(float(bars["Close"].iloc[j]), strike,
                                 max(dte - elapsed_days, 0.0) / 365.0, iv)
                pnl_pct, reason = (px - entry_px) / entry_px * 100.0, "TIME_EXIT"
                if px >= entry_px * 1.20:
                    pnl_pct, reason = 20.0, "TARGET"
                    break
                if px <= entry_px * 0.85:
                    pnl_pct, reason = -15.0, "STOP"
                    break
            if pnl_pct is None:
                continue
            results.append({"pnl": round(pnl_pct / 100.0 * entry_px * 100, 2),
                            "pct": round(pnl_pct, 2), "reason": reason})
        _report(f"exit by {exit_by.strftime('%H:%M')}", results, len(sessions))


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    _patch_engine()
    sessions = _load_sessions()
    print(f"{len(sessions)} sessions, {min(sessions)} to {max(sessions)}")
    if which in ("morning", "all"):
        sweep_morning(sessions)
    if which in ("windows", "all"):
        sweep_windows(sessions)
    if which in ("retries", "all"):
        sweep_retries(sessions)
    if which in ("macro", "all"):
        sweep_macro(sessions)
    if which in ("placement", "all"):
        sweep_placement(sessions)
    if which in ("condorvs", "all"):
        sweep_condorvs(sessions)
    if which in ("trenddepth", "all"):
        sweep_trenddepth(sessions)
    if which in ("daytrend", "all"):
        sweep_daytrend(sessions)
    if which in ("daytype", "all"):
        sweep_daytype(sessions)
    if which in ("squeeze", "all"):
        sweep_squeeze(sessions)
    if which in ("bearside", "all"):
        sweep_bearside(sessions)
    if which in ("ratchetstyle", "all"):
        sweep_ratchetstyle(sessions)
    if which in ("cooldown", "all"):
        sweep_cooldown(sessions)
    if which in ("size", "all"):
        sweep_size(sessions)
    if which in ("daily", "all"):
        sweep_daily(sessions)
    if which in ("creditexit", "all"):
        sweep_creditexit(sessions)
    if which in ("creditgate", "all"):
        sweep_creditgate(sessions)
    if which in ("rideratchet", "all"):
        sweep_rideratchet(sessions)
    if which in ("breach", "all"):
        sweep_breach(sessions)
    if which in ("credit", "all"):
        sweep_credit(sessions)
    if which in ("condor", "all"):
        sweep_condor(sessions)
    if which in ("orb", "all"):
        sweep_orb(sessions)


if __name__ == "__main__":
    main()
