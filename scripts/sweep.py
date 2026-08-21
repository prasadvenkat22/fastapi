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


class _Clock:
    """The simulated 'now', shared by every patched time function."""
    now = datetime(2026, 1, 1, 9, 45, tzinfo=NY)


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _Clock.now


def _patch_engine():
    """Point the engine at simulated time and account state."""
    N.datetime = _FakeDatetime
    PB.datetime = _FakeDatetime
    broker_mod.datetime = _FakeDatetime
    N.blocked_direction = lambda: None
    N.consecutive_losses_today = lambda: 0
    N.current_equity = lambda *_: EquityState(EQUITY, 0.0, EQUITY, 0.0, EQUITY * 0.06, False)
    # Observational only, and it would fire a live HTTP request per bar.
    N.log_price_divergence = lambda *a, **k: None


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


def _load_sessions(period: str = "60d") -> dict:
    bars = yf.Ticker("QQQ").history(period=period, interval="5m")
    if bars.empty:
        raise SystemExit("yfinance returned no bars")
    bars = bars.tz_convert(NY)
    return {d: g for d, g in bars.groupby(bars.index.date) if len(g) > 40}


def _mark(position, spot: float) -> float:
    if is_credit(position.strategy):
        return fill_price(estimate_credit_value(position.strategy, position.short_strike,
                                                position.long_strike, spot), "buy")
    return fill_price(estimate_spread_value(position.strategy, position.long_strike,
                                            position.short_strike, spot), "sell")


def replay_session(day_bars: pd.DataFrame, start: dtime, end: dtime) -> list:
    """One session through the engine. Returns the trades it closed."""
    broker = MockBrokerClient(position=None, available_cash=EQUITY)
    trades, entry = [], None

    for i in range(len(day_bars)):
        ts = day_bars.index[i]
        if not (start <= ts.time() <= end):
            continue
        _Clock.now = ts.to_pydatetime()
        seen = day_bars.iloc[: i + 1]
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
            trades.append(_close(entry, before, spot, ts, out.get("exit_reason", "")))
            entry = None

    position = broker.get_open_position()
    if position is not None and entry is not None:
        spot = float(day_bars["Close"].iloc[-1])
        trades.append(_close(entry, position, spot, day_bars.index[-1], "EOD"))
    return trades


def _close(entry: dict, position, spot: float, ts, reason: str) -> dict:
    exit_value = _mark(position, spot)
    per = ((entry["debit"] - exit_value) if is_credit(entry["strategy"])
           else (exit_value - entry["debit"]))
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
    for width in (3.0, 4.0, 5.0):
        for depth in (None, 2.0):
            PB.WINDOWS = tuple(
                replace(w, width=width, long_depth=depth) if w.name == "MORNING_DRIFT" else w
                for w in base
            )
            PB.ENABLED_WINDOWS = frozenset({"MORNING_DRIFT"})
            trades = []
            for bars in sessions.values():
                trades += replay_session(bars, dtime(10, 15), dtime(15, 45))
            depth_label = "deep (long = width)" if depth is None else f"long ${depth:.0f} ITM"
            _report(f"${width:.0f} wide, {depth_label}", trades, len(sessions))
    PB.WINDOWS = base


def sweep_credit(sessions: dict):
    print("\nAFTERNOON_CREDIT -- width, entries 13:30-15:00\n")
    base = PB.WINDOWS
    for width in (2.0, 3.0, 4.0, 5.0):
        PB.WINDOWS = tuple(
            replace(w, width=width) if w.name == "AFTERNOON_CREDIT" else w for w in base
        )
        PB.ENABLED_WINDOWS = frozenset({"AFTERNOON_CREDIT"})
        trades = []
        for bars in sessions.values():
            trades += replay_session(bars, dtime(13, 30), dtime(15, 45))
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
                        adx = _adx(bars.iloc[: i + 1])
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
    if which in ("credit", "all"):
        sweep_credit(sessions)
    if which in ("condor", "all"):
        sweep_condor(sessions)
    if which in ("orb", "all"):
        sweep_orb(sessions)


if __name__ == "__main__":
    main()
