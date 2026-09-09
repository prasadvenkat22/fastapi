"""Rank real weekly verticals on drift-corrected EV, priced at fills you could get.

Three things this does that a naive version does not, each because a naive
version produced a wrong answer on 2026-09-06:

  REALISTIC FILLS. Cost is ask(long) - bid(short), never mid-to-mid. The first
  run ranked a DELL 465/525 call spread top at a mid-to-mid 43.75 against 59.14
  of intrinsic -- buying $59 for $44, which does not exist. Its 465C bid was
  itself below intrinsic: a stale Friday close on an illiquid deep-ITM strike.

  A QUOTE-WIDTH FILTER. Open interest is not liquidity. The same run ranked a
  GOOGL 300/338 put spread second on a short leg quoted 0.03/0.08 -- a market
  91% of mid wide, where the mid is fiction. MAX_SPREAD_PCT rejects those.

  A PER-SYMBOL HORIZON. tk.options[0] is a different expiry for different
  names -- DELL's front week was 2026-09-11 and GOOGL's 2026-09-09 -- and the
  first run scored every candidate over a hardcoded 4 days. The forward-return
  window is now derived from each name's own expiry.

And the headline number is DEMEANED EV. Section 106: on SNDK the raw empirical
EV was +1192 a contract and 109% of it was drift, so the same structure at the
same price lost money once the trend was removed. Raw is printed beside it as
a reminder of how large the difference is, never as the ranking key.

    python scripts/weekly_pick.py --symbols NVDA,MRVL --side call
    python scripts/weekly_pick.py --symbols PANW --side put
"""

import argparse
import math
import os
import sys
from datetime import date, datetime

import numpy as np
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.greeks import leg_greeks, spread_greeks

# A market wider than this fraction of its own mid is not a price, it is an
# absence of one. 0.25 still admits wide weekly strikes; it rejects the 91%
# quote that ranked second on the first run.
MAX_SPREAD_PCT = float(os.getenv("PICK_MAX_SPREAD_PCT", "0.25"))
MIN_OI = int(os.getenv("PICK_MIN_OI", "50"))
# Demean against a BOUNDED history. Max history pulls 2008 and 2020 into a
# 2026 vol regime and flatters far-OTM tails; three years keeps the estimate
# in something resembling the present.
HISTORY = os.getenv("PICK_HISTORY", "3y")


def atr14(h):
    H, L, C = h["High"], h["Low"], h["Close"]
    pc = C.shift(1)
    tr = (H - L).combine((H - pc).abs(), max).combine((L - pc).abs(), max)
    return float(tr.rolling(14).mean().iloc[-1])


def rv20(h):
    r = np.diff(np.log(h["Close"].values[-21:]))
    return float(np.std(r, ddof=1) * math.sqrt(252)) if len(r) > 2 else float("nan")


def trading_days_to(exp: str) -> int:
    """Sessions from the next trading day to expiry inclusive, holidays aware."""
    try:
        from trading_engine.market_calendar import is_trading_day
    except Exception:
        def is_trading_day(d):
            return d.weekday() < 5
    y, m, d = (int(x) for x in exp.split("-"))
    end, cur, n = date(y, m, d), date.today(), 0
    while cur <= end:
        if cur > date.today() and is_trading_day(cur):
            n += 1
        cur = date.fromordinal(cur.toordinal() + 1)
    return max(n, 1)


MC_PATHS = int(os.getenv("PICK_MC_PATHS", "10000"))

# HOW A NEWS VERDICT IS ALLOWED TO MOVE THE EV.
#
# The two EVs already bracket the answer. EVdem assumes the name's drift is
# unpredictable; EVraw assumes it continues exactly as it has. Neither is a
# forecast on its own -- which of them is right is a question about whether
# there is a REASON for the drift, and a same-day catalyst read is exactly
# that question.
#
# So sentiment does not invent a probability. It sets a weight between two
# numbers we already have:
#
#     EV_adj = EV_dem + w * confidence * (EV_raw - EV_dem)
#
# w = 0 is the drift-removed number, w = 1 the drift-inclusive one, and the
# verdict picks a point between. A NEUTRAL read leaves EVdem untouched, which
# is the correct default and the one this book has been using.
#
# THIS IS A STATED ASSUMPTION, NOT A FITTED MODEL. The weights below were
# chosen, not measured -- news_verdicts began accumulating 2026-09-07 and has
# no history to fit against. They are here so the assumption is explicit,
# versioned and testable: once a few hundred labelled days exist, compare the
# realised outcome against EV_adj at several weight settings and find out
# whether any of them beat w = 0. Section 119 is what happens when a number
# like this is fitted instead of stated on 14 samples.
NEWS_DRIFT_WEIGHT = {
    "VERY_BULLISH": 1.0, "BULLISH": 0.5, "NEUTRAL": 0.0,
    "BEARISH": -0.5, "VERY_BEARISH": -1.0,
}


# A GUARD, WHICH IS NOT A FORECAST.
#
# The overlay above asks "should the drift be believed", which needs sentiment
# to carry information and is still unmeasured. THIS asks something weaker and
# structural: are we about to take the wrong side of a KNOWN event?
#
# On 2026-09-04 a short call on SNDK was nearly written into an S&P 100
# inclusion -- forced index-fund buying on a published date. That is not a
# prediction failure; nothing needs to be forecast to say that selling upside
# into mechanical buying is a bad structure. The guard fires on VERY_* only,
# because an ordinary read against a position is noise at this sample size and
# a flag that fires constantly stops being read (section 120).
# The old (side, verdict) table lived here until credit structures arrived and
# made `side` the wrong key: a call CREDIT spread is bearish, and a table keyed
# on the option type would have cleared it against very bullish news. See
# direction() and conflict_for().


def direction(side: str, structure: str) -> str:
    """Which way the STRUCTURE is exposed, which is not the option type.

    A call DEBIT spread is bullish; a call CREDIT spread is bearish -- you
    sold the upside. Every guard below keys on this rather than on `side`,
    because keying on the option type would have flagged a bear call spread
    for conflicting with bearish news, i.e. exactly backwards.
    """
    if structure == "credit":
        return "bearish" if side == "call" else "bullish"
    return "bullish" if side == "call" else "bearish"


def conflict_for(side: str, verdict: "str | None",
                 structure: str = "debit") -> "str | None":
    if not verdict:
        return None
    d = direction(side, structure)
    if d == "bullish" and verdict == "VERY_BEARISH":
        return f"{structure} {side} spread is BULLISH, into a very bearish catalyst"
    if d == "bearish" and verdict == "VERY_BULLISH":
        return f"{structure} {side} spread is BEARISH, into a very bullish catalyst"
    return None


def news_verdict(symbol: str):
    """(verdict, confidence) from today's news_verdicts row, or None."""
    try:
        import psycopg2
        from datetime import datetime
        from zoneinfo import ZoneInfo
        url = os.getenv("DATABASE_URL", "")
        dsn = url.replace("postgresql+psycopg2://", "postgresql://").replace(
            "postgresql+asyncpg://", "postgresql://")
        day = datetime.now(ZoneInfo("America/New_York")).date()
        with psycopg2.connect(dsn) as c, c.cursor() as cur:
            cur.execute("SELECT verdict, confidence FROM news_verdicts "
                        "WHERE symbol=%s AND trading_day=%s", (symbol.upper(), day))
            r = cur.fetchone()
        return (r[0], float(r[1] or 0.0)) if r else None
    except Exception:
        return None




# INTRADAY FLOW, SHOWN AND NOT USED. The screener prints today's tape reading
# beside every candidate and lets it change NOTHING -- not the EV, not the
# probabilities, not the ranking. Section 22's rule, the same one that keeps
# crude and the macro verdict out of the 0DTE gates: a term nobody has scored
# against outcomes is recorded next to the decision, never inside it.
#
# It earns its place on the page because it answers a question the EV columns
# cannot: EV is a four-day distribution, and this is what the tape is doing
# right now, while you are deciding whether to pay the ask.
#
# READ IT AS TWO NUMBERS THAT MUST AGREE. Signed volume alone called SNDK
# bought on 2026-09-08 (74% up-volume, +1.28m net) with price BELOW a flat
# VWAP -- buyers who were not winning. Only the pair separates that from
# 2026-09-04, when VWAP rose 2.56% and no bar closed beneath it (section 129).
_FLOW_CACHE: dict = {}


def flow_read(sym: str) -> dict:
    """{label, vs_vwap, up_pct} for today's tape, or {} when unavailable."""
    if sym in _FLOW_CACHE:
        return _FLOW_CACHE[sym]
    out: dict = {}
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        from flow import analyse

        r = analyse(sym, _dt.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
                    "5min", False)
        if r and r.get("vwap"):
            vs = (r["last"] / r["vwap"] - 1.0) * 100.0
            tot = r["up"] + r["dn"]
            up = (r["up"] / tot * 100.0) if tot else 50.0
            # ONE rule, imported. It lived here AND in screener.flow_table
            # until 2026-09-08, which is how two callers end up disagreeing
            # about what BUY means.
            from flow import flow_label

            slope = r.get("slope") or 0.0
            above = r.get("above_pct") or 0.0
            out = dict(label=flow_label(vs, up, slope, above),
                       vs_vwap=vs, up_pct=up, slope=slope, above_pct=above)
    except Exception:
        out = {}
    _FLOW_CACHE[sym] = out
    return out


def flow_conflict(side: str, flow: dict, structure: str = "debit") -> "str | None":
    """A long structure into a tape being sold, or the reverse.

    NOT a forecast and not a gate -- an observation that the thing you are
    about to buy is being sold right now, which is worth seeing before you pay
    the ask rather than after.
    """
    if not flow:
        return None
    lab = flow.get("label")
    d = direction(side, structure)
    if d == "bullish" and lab == "SELL":
        return "bullish structure into a tape being sold"
    if d == "bearish" and lab == "BUY":
        return "bearish structure into a tape being bought"
    return None


def monte_carlo_terminal(spot: float, atr: float, days: int, seed: int = 7):
    """Terminal prices from a driftless random walk calibrated to ATR.

    A THIRD probability estimate, and it exists for one reason the historical
    bands cannot cover: resampling three years of moves assumes the last three
    years describe next week. For a name that just gapped 11.9% -- SNDK on
    2026-09-04 -- that assumption is doing real work. A walk calibrated to
    TODAY's ATR does not care what the name did in 2024.

    ATR -> sigma. For a driftless random walk the expected high-low range over
    one period is about 1.596 sigma, so sigma ~ ATR / 1.596. Using ATR/price
    directly as a daily sigma overstates volatility by roughly 60%, which is
    what makes a naive ATR Monte Carlo look far more dangerous than the stock.

    DRIFTLESS on purpose, to match the demeaned historical bands. A walk with
    drift would disagree with them for a reason that is not about method.
    """
    if not (spot > 0 and atr > 0 and days > 0):
        return None
    sigma_d = (atr / 1.596) / spot
    rng = np.random.default_rng(seed)
    steps = rng.normal(-0.5 * sigma_d ** 2, sigma_d, size=(MC_PATHS, days))
    return spot * np.exp(steps.sum(axis=1))


def usable(row):
    b, a = float(row["bid"]), float(row["ask"])
    if b <= 0 or a <= 0 or float(row.get("openInterest") or 0) < MIN_OI:
        return None
    mid = (a + b) / 2
    if mid <= 0 or (a - b) / mid > MAX_SPREAD_PCT:
        return None
    return b, a, mid, float(row.get("impliedVolatility") or 0)


def evaluate(sym, side, structure: str = "debit", expiry: str = ""):
    tk = yf.Ticker(sym)
    h = tk.history(period=HISTORY, interval="1d")
    if len(h) < 120:
        return [], None
    spot, a14, rv = float(h["Close"].iloc[-1]), atr14(h), rv20(h)
    # NEAREST BY DEFAULT, WHICH IS NOT ALWAYS A WEEK. Measured 2026-09-09:
    # NVDA, MU and AVGO listed 09-09 as options[0] -- the SAME DAY -- while
    # MRVL, DELL and PANW listed 09-11. A screen asking for "weekly call
    # spreads" silently returned same-day structures for half the book, and
    # the IV on an expiring contract is unreliable enough that the EV built on
    # it is not a number worth ranking.
    #
    # `expiry` takes an exact date, or a MINIMUM number of calendar days with
    # a leading "+": "+5" picks the first expiry at least five days out.
    exps = list(tk.options)
    if not exps:
        return [], None
    exp = exps[0]
    if expiry.startswith("+"):
        from datetime import date as _d, timedelta as _td

        floor = _d.today() + _td(days=int(expiry[1:]))
        later = [e for e in exps
                 if _d(*(int(x) for x in e.split("-"))) >= floor]
        if later:
            exp = later[0]
    elif expiry:
        if expiry not in exps:
            raise ValueError(f"{sym} has no {expiry} expiry; has {exps[:5]}")
        exp = expiry
    fwd_days = trading_days_to(exp)
    c = h["Close"].values
    fwd = c[fwd_days:] / c[:-fwd_days] - 1.0
    # DEMEAN LOG RETURNS, not simple ones. Setting the arithmetic mean of
    # simple returns to zero and applying spot*(1+r) leaves the MEDIAN below
    # spot by about 0.5*sigma^2*t -- volatility drag -- while delta's
    # log-normal carries a -0.5*sigma^2*t term that centres it. The mismatch
    # made every call EV pessimistic and every put EV optimistic. Caught
    # 2026-09-07 by scripts/delta_calibration.py: the apparent bias against
    # delta was +1.8 points on calls and -2.0 on puts, in OPPOSITE directions,
    # which no market effect produces. Log-demeaning took both inside a point.
    lr = np.log(c[fwd_days:] / c[:-fwd_days])
    dem_prices_factor = np.exp(lr - lr.mean())
    # A bullish read helps a CALL and hurts a PUT, so the sign flips with side.
    nv = news_verdict(sym)
    news_w = 0.0
    if nv:
        # NO SIGN FLIP FOR PUTS. The drift term (EVraw - EVdem) is computed
        # from the SIDE'S OWN payoff, so it already carries the right sign: on
        # a rising name EVraw exceeds EVdem for a call and falls below it for a
        # put. Flipping w on top of that double-negates, and the guard caught
        # it on 2026-09-07 -- an SNDK put under a VERY_BULLISH verdict was
        # reporting EVadj +1238.7 against an EVdem of +127.8, i.e. bullish news
        # making a put ten times better. Bounded to [-1, 1] so a confidence
        # above 1.0 from the model cannot extrapolate past EVraw.
        news_w = max(-1.0, min(1.0, NEWS_DRIFT_WEIGHT.get(nv[0], 0.0) * nv[1]))

    fl = flow_read(sym)
    mc = monte_carlo_terminal(spot, a14, fwd_days)
    chain = tk.option_chain(exp)
    calls = side == "call"
    df = chain.calls if calls else chain.puts

    q = {}
    for _, r in df.iterrows():
        u = usable(r)
        if u:
            q[float(r["strike"])] = u
    ks = sorted(q)
    ivs = [q[k][3] for k in ks if q[k][3] > 0]
    atm_iv = float(np.median(ivs)) if ivs else float("nan")
    out = []
    for lo in ks:
        for hi in ks:
            if hi <= lo:
                continue
            w = hi - lo
            if not (0.02 * spot <= w <= 0.12 * spot):
                continue
            # CREDIT AND DEBIT SHARE EVERY COLUMN BELOW because `cost` is set
            # to the MAX RISK either way. For a debit that is what you paid;
            # for a credit it is width minus the credit received. With that
            # one substitution need = cost/w, rr = (w-cost)/cost and
            # ev_pct = EV/cost all stay correct without a second code path,
            # and `edge` -- Pwin minus the break-even win rate -- keeps its
            # meaning across both. A credit spread's break-even win rate is
            # 1 - credit/w, which is exactly (w - credit)/w.
            if structure == "credit":
                if calls:                  # BEAR CALL: short lo, long hi
                    credit = q[lo][0] - q[hi][1]
                    long_k, short_k, ivl, ivs_ = hi, lo, q[hi][3], q[lo][3]
                    payoff = lambda p: credit - np.clip(p - lo, 0, w)
                    room = (lo - spot) / a14     # cushion to the short strike
                else:                      # BULL PUT: short hi, long lo
                    credit = q[hi][0] - q[lo][1]
                    long_k, short_k, ivl, ivs_ = lo, hi, q[lo][3], q[hi][3]
                    payoff = lambda p: credit - np.clip(hi - p, 0, w)
                    room = (spot - hi) / a14
                if credit <= 0.05 or credit >= w:
                    continue
                cost = w - credit          # MAX RISK, the common denominator
            elif calls:                    # long lo, short hi -- buy ask, sell bid
                cost = q[lo][1] - q[hi][0]
                long_k, short_k, ivl, ivs_ = lo, hi, q[lo][3], q[hi][3]
                payoff = lambda p: np.clip(p - lo, 0, w) - cost
                room = (hi - spot) / a14   # OTM room above the short strike
            else:                          # PUT debit: long hi, short lo
                cost = q[hi][1] - q[lo][0]
                long_k, short_k, ivl, ivs_ = hi, lo, q[hi][3], q[lo][3]
                payoff = lambda p: np.clip(hi - p, 0, w) - cost
                room = (spot - lo) / a14
            if cost <= 0.05 or cost >= w:
                continue
            # A DEBIT SPREAD CANNOT COST LESS THAN ITS INTRINSIC VALUE, and a
            # credit cannot pay more than the width less that intrinsic. When
            # the quote says otherwise the quote is stale, not the market
            # generous -- deep-in-the-money strikes barely trade and their
            # bid/ask can sit untouched for days.
            #
            # Measured 2026-09-09: ranked by edge, the top three rows were
            # DELL 105/125 with DELL at 543.88, SNDK 610/650 with SNDK at
            # 1777, and MRVL 45/55 with MRVL at 237. Each showed Pwin 100.0%,
            # an edge near +88 points and EV over a thousand dollars, because
            # a 20-dollar-wide spread carrying its full 20 of intrinsic was
            # quoted at 2.40. Every probability was right and the price was
            # fiction, which is the combination that puts nonsense at the TOP
            # of a ranking rather than the bottom.
            #
            # 0.9 rather than 1.0 leaves room for the small legitimate
            # discount on a deep structure with rate and dividend effects.
            intrinsic_now = (min(max(spot - lo, 0.0), w) if calls
                             else min(max(hi - spot, 0.0), w))
            if structure == "credit":
                if (w - cost) > (w - intrinsic_now * 0.9):
                    continue
            elif cost < intrinsic_now * 0.9:
                continue
            prices = spot * dem_prices_factor
            dm = payoff(prices)
            raw = payoff(spot * (1 + fwd))   # raw KEEPS the drift, by design
            # THE DECOMPOSITION, explicitly. EV is not P(win) x reward +
            # P(lose) x risk -- a vertical has a THIRD outcome, finishing
            # between the strikes, and for a deep-ITM structure that middle
            # band is where a large share of the probability sits. Ignoring
            # it overstates both tails. P comes from the name's own
            # drift-removed move distribution; the strikes decide where the
            # bands fall inside it, which is what ITM depth actually controls.
            # KEYED ON DIRECTION, NOT ON THE OPTION TYPE. A bull put credit
            # spread makes its maximum ABOVE the short strike, exactly like a
            # bull call debit spread does -- and a bear call credit spread
            # makes its maximum below. Branching on `calls` here would have
            # inverted every probability on the credit side while leaving the
            # payoff correct, which reconciles to nothing and is the hardest
            # class of bug to see in a table.
            bullish = direction(side, structure) == "bullish"
            if bullish:
                p_max = float((prices >= hi).mean())
                p_min = float((prices <= lo).mean())
            else:
                p_max = float((prices <= lo).mean())
                p_min = float((prices >= hi).mean())
            p_mid = max(0.0, 1.0 - p_max - p_min)
            if mc is None:
                mc_max = float("nan")
            elif bullish:
                mc_max = float((mc >= hi).mean())
            else:
                mc_max = float((mc <= lo).mean())
            g = spread_greeks(spot, long_k, short_k, fwd_days / 252.0, ivl, ivs_,
                              call=calls)
            # DELTA AS PROBABILITY, beside the realised bands. A leg's delta
            # approximates P(that strike finishes in the money), so the SHORT
            # leg's delta is P(max profit) and the difference -- the net delta
            # people quote as "the probability of the trade" -- is actually
            # P(finishing BETWEEN the strikes), the partial band.
            #
            # Checked 2026-09-06 against three years of drift-removed moves:
            #   DELL 430/480  net delta 10.8% vs 9.9% realised   (0.9p out)
            #   NVDA 230/240  net delta 46.0% vs 39.1%           (6.9p)
            #   MRVL 220/235  net delta 32.5% vs 36.4%           (3.9p)
            # Good on the body, weakest on P(max) -- off by -6.0p and +5.2p --
            # which is exactly where the payoff lives. A sanity check on the
            # empirical numbers, not a replacement for them.
            dl = abs(leg_greeks(spot, long_k, fwd_days / 252.0, ivl, calls)["delta"])
            dh = abs(leg_greeks(spot, short_k, fwd_days / 252.0, ivs_, calls)["delta"])
            # THE CHAIN'S OWN P(max profit). For a debit that is the short
            # leg's delta -- the structure pays its maximum when that strike
            # finishes in the money. For a CREDIT it is the complement: the
            # maximum is kept when the short strike is NOT breached. Storing
            # the probability rather than the raw delta keeps the Pimp column
            # meaning one thing in both tables.
            p_imp = (1.0 - dh) if structure == "credit" else dh
            out.append(dict(
                sym=sym, lo=lo, hi=hi, w=w, cost=cost, spot=spot, atr=a14, rv=rv,
                iv=atm_iv, exp=exp, days=fwd_days,
                news=(nv[0] if nv else None), news_w=news_w,
                structure=structure, direction=direction(side, structure),
                credit=(w - cost) if structure == "credit" else None,
                conflict=conflict_for(side, nv[0] if nv else None, structure),
                flow=fl, flow_conflict=flow_conflict(side, fl, structure),
                p_imp=p_imp,
                ev_dem=float(dm.mean()) * 100, ev_raw=float(raw.mean()) * 100,
                ev_adj=(float(dm.mean()) + news_w * (float(raw.mean()) - float(dm.mean()))) * 100,
                pwin=float((dm > 0).mean()), need=cost / w,
                rr=(w - cost) / cost, room=room, n=len(lr),
                p_max=p_max, p_mid=p_mid, p_min=p_min, mc_max=mc_max,
                d_long=dl, d_short=dh, d_net=dl - dh,
                # How deep the LONG leg sits, in the name's own ATR. THIS IS
                # THE KNOB: deeper ITM buys probability and sells payoff, and
                # they move against each other along a frontier rather than
                # one being simply better. In ATR, not dollars, so it means
                # the same on a 1740 stock and a 230 one.
                itm=((spot - lo) / a14) if bullish else ((hi - spot) / a14),
                # EV as a percent of capital at risk. A dollar EV is not
                # comparable between a 258 risk and a 2090 one; this is.
                ev_pct=(float(dm.mean()) / cost * 100.0),
                **g))
    return out, dict(spot=spot, atr=a14, rv=rv, iv=atm_iv, exp=exp, days=fwd_days,
                     strikes=len(ks))




# THE FOUR SORTS, AND WHY `edge` WAS ADDED (2026-09-08).
#
# Both ends of the original three produce structures nobody should take, and
# the runs on 2026-09-08 showed each failing in its own direction:
#
#   --by prob    reaches for deep-ITM verticals where the reward is already
#                spent. AVGO 345/358 asked 1250 to make NOTHING: break-even
#                win rate 100.0%, EV -62.8.
#   --by evpct   reaches for the opposite trap, the OTM lottery ticket. SNDK
#                2100/2200 at 1:39 on a 6.7% chance of any profit.
#
# `edge` is Pwin minus need -- the probability of profit minus the break-even
# win rate the price demands. It is the only one of the four that asks whether
# you are being PAID for the odds, which is the question both traps answer no
# to while scoring top of their own sort.
SORT_KEY = {
    "ev": lambda x: -x["ev_dem"],
    "evpct": lambda x: -x["ev_pct"],
    "prob": lambda x: -x["pwin"],
    "edge": lambda x: -(x["pwin"] - x["need"]),
}
SORT_LABEL = {
    "ev": "DEMEANED EV ($)",
    "evpct": "EV PER $ RISKED",
    "prob": "PROBABILITY OF PROFIT",
    "edge": "EDGE (Pwin - break-even win rate)",
}


def rank(symbols, side: str, by: str = "evpct", top: int = 10,
         rr_min: float = 0.0, rr_max: float = 0.0,
         structure: str = "debit", per_symbol: int = 0,
         expiry: str = "") -> dict:
    """The screener as a CALLABLE, so the CLI and the HTTP endpoint cannot
    drift apart. Returns {rows, meta, warnings} with the rows already filtered
    and sorted -- everything main() prints, minus the printing.

    Errors on one symbol are collected into `warnings` rather than raised: a
    screen over six names should return the five that worked.
    """
    if by not in SORT_KEY:
        raise ValueError(f"unknown sort {by!r}; expected one of {sorted(SORT_KEY)}")
    rows, meta, warnings = [], [], []
    for sym in [x.strip().upper() for x in symbols if str(x).strip()]:
        try:
            r, m = evaluate(sym, side, structure, expiry)
            if m:
                m = dict(m, symbol=sym, candidates=len(r))
                meta.append(m)
            rows += r
        except Exception as exc:
            warnings.append(f"{sym}: {exc}")
    if rr_min > 0:
        rows = [r for r in rows if r["rr"] >= rr_min]
    if rr_max > 0:
        rows = [r for r in rows if r["rr"] <= rr_max]
    ordered = sorted(rows, key=SORT_KEY[by])
    if per_symbol > 0:
        # ONE NAME OTHERWISE TAKES THE WHOLE PAGE. Measured 2026-09-08: a
        # screen over CRWV, AVGO and SNDK returned twelve CRWV rows and
        # nothing else, because a single favourable IV/RV lifts every strike
        # on that name above every strike on the others. A cross-name screen
        # that cannot show the other names is not a cross-name screen.
        seen: dict = {}
        capped = []
        for r in ordered:
            n = seen.get(r["sym"], 0)
            if n >= per_symbol:
                continue
            seen[r["sym"]] = n + 1
            capped.append(r)
        ordered = capped
    ranked = ordered[:top]
    return {"rows": ranked, "meta": meta, "warnings": warnings,
            "sort": by, "sort_label": SORT_LABEL[by], "side": side,
            "structure": structure, "considered": len(rows)}


def _flow_cell(r: dict) -> str:
    """Compact tape reading: the label and the up-volume share behind it."""
    f = r.get("flow") or {}
    if not f:
        return "-"
    return f"{f['label']} {f['up_pct']:.0f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--side", choices=("call", "put"), required=True)
    ap.add_argument("--structure", choices=("debit", "credit"), default="debit",
                    help="debit = you BUY the spread, credit = you SELL it. "
                         "The direction flips: a call CREDIT spread is "
                         "bearish, a put CREDIT spread is bullish.")
    ap.add_argument("--expiry", default="",
                    help="exact date (2026-09-18), or \"+N\" for the first "
                         "expiry at least N days out. Default is the NEAREST, "
                         "which on some names is the same day.")
    ap.add_argument("--per-symbol", type=int, default=0,
                    help="at most this many rows per name, so one symbol "
                         "cannot take the whole page")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--rr-min", type=float, default=0.0,
                    help="only structures paying at least this reward per unit "
                         "of risk; --rr-min 2 gives R:R 1:2 or better")
    ap.add_argument("--rr-max", type=float, default=0.0,
                    help="upper bound on reward per unit risk. Pair with "
                         "--rr-min to see a BAND: --rr-min 1.5 --rr-max 3 is "
                         "the moderate geometry, between the deep-ITM trap "
                         "(high probability, no payoff) and the OTM lottery "
                         "ticket (huge payoff, ~90% total loss).")
    ap.add_argument("--by", choices=("ev", "evpct", "prob", "edge"),
                    default="evpct",
                    help="evpct = EV per dollar risked (default), prob = highest "
                         "probability of profit, ev = raw dollar EV, edge = "
                         "Pwin minus the break-even win rate")
    args = ap.parse_args()

    rows = []
    for s in [x.strip().upper() for x in args.symbols.split(",") if x.strip()]:
        try:
            r, meta = evaluate(s, args.side, args.structure, args.expiry)
            if meta:
                ivrv = meta["iv"] / meta["rv"] if meta["rv"] else float("nan")
                print(f"{s:6s} spot {meta['spot']:8.2f}  ATR {meta['atr']:7.2f}  "
                      f"RV {meta['rv']*100:4.0f}%  IV {meta['iv']*100:4.0f}%  "
                      f"IV/RV {ivrv:4.2f}  exp {meta['exp']} ({meta['days']}d)  "
                      f"{meta['strikes']} usable strikes  {len(r)} candidates")
            rows += r
        except Exception as e:
            print(f"{s:6s} error {e}")

    if args.rr_min > 0:
        rows = [r for r in rows if r["rr"] >= args.rr_min]
    if args.rr_max > 0:
        rows = [r for r in rows if r["rr"] <= args.rr_max]
    if not rows:
        print("\nNothing passed the quote filter. That is a result, not a failure: "
              "on stale weekend marks it is the correct answer.")
        return
    bad = [r for r in rows if r.get("conflict")]
    if bad:
        print(f"\n!! GUARD: {len(bad)} of {len(rows)} candidates take the wrong "
              f"side of a known catalyst -- {bad[0]['conflict']}.")
        print("   These are NOT filtered out. The guard names the conflict and "
              "leaves the decision with you; a structure that is cheap enough "
              "may still be worth it, but not by accident.")

    label = SORT_LABEL[args.by]
    dirn = direction(args.side, args.structure).upper()
    fills = ("bid/ask -- short leg at the BID, long leg at the ASK"
             if args.structure == "credit" else "ask/bid")
    print(f"\n=== {args.side.upper()} {args.structure.upper()} SPREADS "
          f"({dirn}) — ranked by {label}, priced at {fills} ===")
    print(f"{'sym':6s} {'strikes':>14s} {'ITMatr':>7s} {'risk':>7s} {'reward':>7s} "
          f"{'R:R':>7s} {'Pimp':>6s} {'Phist':>6s} {'Pmc':>6s} {'Pwin':>6s} "
          f"{'need':>6s} {'edge':>7s} {'EV$':>8s} {'EVadj':>8s} {'news':>13s} "
          f"{'flow':>13s}")
    ordered = sorted(rows, key=SORT_KEY[args.by])
    if args.per_symbol > 0:
        seen, capped = {}, []
        for r in ordered:
            if seen.get(r["sym"], 0) >= args.per_symbol:
                continue
            seen[r["sym"]] = seen.get(r["sym"], 0) + 1
            capped.append(r)
        ordered = capped
    for r in ordered[:args.top]:
        print(f"{r['sym']:6s} {r['lo']:6.0f}/{r['hi']:<7.0f} {r['itm']:+7.2f} "
              f"{r['cost']*100:7.0f} {(r['w']-r['cost'])*100:7.0f} "
              f"1:{r['rr']:<5.2f} {r['p_imp']*100:5.1f}% {r['p_max']*100:5.1f}% "
              f"{r['mc_max']*100:5.1f}% {r['pwin']*100:5.1f}% "
              f"{r['need']*100:5.1f}% "
              f"{(r['pwin']-r['need'])*100:+6.1f}p "
              f"{r['ev_dem']:+8.1f} {r['ev_adj']:+8.1f} "
              f"{(r['news'] or '-'):>13s}"
              f"{_flow_cell(r):>13s}"
              f"{'  <-- NEWS CONFLICT' if r.get('conflict') else ''}"
              f"{'  <-- FLOW CONFLICT' if r.get('flow_conflict') else ''}")
    print("\nflow is TODAY'S TAPE and changes nothing above it -- "
          "not the EV, not the probabilities, not the order. BUY and SELL "
          "require price-vs-VWAP and up-volume share to AGREE; MIXED means "
          "they do not, which on 2026-09-08 was SNDK at 74% up-volume with "
          "price below a flat VWAP. An unscored term goes beside the "
          "decision, never inside it (sections 22, 129).")
    if args.structure == "credit":
        print("\nCREDIT: risk is width MINUS the credit, reward is the "
              "credit itself, and need is 1 - credit/width. Those three "
              "substitutions are why every other column means the same "
              "thing in both tables, edge included.")
    print("\nrisk/reward are per CONTRACT. need = cost/width = the break-even "
          "win rate. R:R sizes the WIN and says nothing about the ODDS, which "
          "is why a 1:5.78 payoff can still lose money.")
    print("EV$ is the drift-REMOVED number. EVadj moves it toward the "
          "drift-inclusive one in proportion to the day's news verdict and its "
          "confidence: EVadj = EV + w*conf*(EVraw - EV). A NEUTRAL read leaves "
          "them identical, which is the default. The weights are STATED, not "
          "fitted -- see the table in the source and section 120.")
    print("Pimp/Phist/Pmc are P(MAX profit) -- finishing beyond the SHORT "
          "strike. Pwin is P(ANY profit) -- beyond the BREAKEVEN -- and Pwin "
          "is what `edge` subtracts `need` from. Printing P(max) beside a "
          "need/edge computed from P(win) is why earlier tables could not be "
          "reconciled by hand.")
    print("EVdem is the forecast: what the structure earns if drift is "
          "unpredictable, which over two to five days it is. P(win) is "
          "measured on that same drift-removed history.")
    print("drift = EVraw - EVdem, what the name's past trend CONTRIBUTES. "
          "Large and positive means the raw number is mostly trend-following "
          "and will not survive a flat tape. NEGATIVE means the trade is "
          "fighting the drift and only pays if it stops. On SNDK that column "
          "was 109% of the raw figure, which is how section 106 caught it.")


if __name__ == "__main__":
    main()
