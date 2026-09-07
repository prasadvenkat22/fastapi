"""Did the news overlay move EV in the direction the outcome went?

THE CLAIM UNDER TEST, in the form it was put: "EV on Friday for 0DTE QQQ should
be drastically low on the news, EV on Friday for SNDK weekly should be way high
on the news." This measures that instead of asserting it.

WHY THIS USES weekly_shadow AND NOT weekly_pick.py. weekly_pick prices off the
LIVE chain, so it cannot price a Friday that has passed. weekly_shadow recorded
every structure at the time it was opened -- strikes, width, credit, short
delta, short IV, spot -- so a past cohort can be re-priced from what was
actually quoted. Anything else would be today's chain wearing Friday's date.

NO LOOKAHEAD. The terminal distribution for a structure opened on day D is
built from that name's history UP TO D and no further. The realized column is
printed beside it and takes no part in computing anything.

    EVdem   drift removed. The base case: no view.
    EVraw   drift included. What the last three years' trend implies.
    EVadj   EVdem + w*(EVraw - EVdem), w from the day's news verdict.

EVadj is a bridge, not a forecast: it cannot leave the interval between two
numbers already computed by other means, so a wrong verdict moves the answer
to a figure that was on the table anyway (section 121).

    python scripts/news_ev_backtest.py --day 2026-08-28
    python scripts/news_ev_backtest.py --day 2026-09-04 --symbols SNDK,QQQ
    python scripts/news_ev_backtest.py --day 2026-08-28 --track SNDK
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

import numpy as np
import psycopg2
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_engine.symbol_news import classify_day, session_headlines

# Same ladder weekly_pick.py uses. Stated, not fitted -- section 120.
NEWS_DRIFT_WEIGHT = {"VERY_BULLISH": 1.0, "BULLISH": 0.5, "NEUTRAL": 0.0,
                     "BEARISH": -0.5, "VERY_BEARISH": -1.0}


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def history(sym: str):
    h = yf.Ticker(sym).history(period="5y", interval="1d")
    h.index = [d.date() for d in h.index]
    H, L, C = h["High"], h["Low"], h["Close"]
    pc = C.shift(1)
    tr = (H - L).combine((H - pc).abs(), max).combine((L - pc).abs(), max)
    h = h.copy()
    h["atr14"] = tr.rolling(14).mean()
    return h


def terminals(h, upto: date, spot: float, fwd_days: int):
    """(demeaned, raw) terminal price samples, from history up to `upto` only.

    Log-return demeaning, not simple-return. Subtracting a simple mean shifts
    the MEDIAN as well as the mean -- volatility drag -- and that error showed
    up as a probability bias in opposite directions on calls and puts, which no
    market effect produces (section 120).
    """
    c = h.loc[[d for d in h.index if d <= upto], "Close"].to_numpy(dtype=float)
    if len(c) < fwd_days + 60:
        return None, None
    lr = np.log(c[fwd_days:] / c[:-fwd_days])
    return spot * np.exp(lr - lr.mean()), spot * np.exp(lr)


def payoff(term: np.ndarray, side: str, short_k: float, long_k: float,
           credit: float) -> np.ndarray:
    """Per-contract P&L in dollars for a credit vertical at expiry."""
    w = abs(long_k - short_k)
    if side == "call":
        loss = np.clip(term - short_k, 0.0, w)
    else:
        loss = np.clip(short_k - term, 0.0, w)
    return (credit - loss) * 100.0


def verdict_for(sym: str, day: date):
    """The stored verdict for that day, else grade the day's headlines now."""
    try:
        with psycopg2.connect(_dsn()) as c, c.cursor() as cur:
            cur.execute("SELECT verdict, confidence FROM news_verdicts "
                        "WHERE symbol=%s AND trading_day=%s", (sym.upper(), day))
            r = cur.fetchone()
        if r:
            return r[0], float(r[1] or 0.0), "stored"
    except Exception:
        pass
    heads = session_headlines(sym, day)
    if not heads:
        return None, 0.0, "no headlines in window"
    g = classify_day(sym, day)
    return g["verdict"], g["confidence"], f"graded now, {g['headline_count']} heads"


def cohort(day: date, syms):
    q = ("SELECT symbol, strategy, short_strike, long_strike, put_short_strike, "
         "put_long_strike, width, spot_at_entry, short_delta, short_iv, "
         "entry_credit_mid, expiration, expiry_return_pct, last_return_pct "
         "FROM weekly_shadow "
         "WHERE (opened_at AT TIME ZONE 'America/New_York')::date = %s "
         "AND strategy <> 'WEEKLY_CONDOR' ORDER BY symbol, strategy")
    with psycopg2.connect(_dsn()) as c, c.cursor() as cur:
        cur.execute(q, (day,))
        rows = cur.fetchall()
    return [r for r in rows if not syms or r[0].upper() in syms]


def run_cohort(day: date, syms, args):
    rows = cohort(day, syms)
    if not rows:
        print(f"no shadow structures opened on {day}")
        return
    print(f"\n{'='*100}")
    print(f"STRUCTURES OPENED {day} — re-priced from what was quoted that day, "
          f"news verdict from that day's window")
    print("="*100)
    print(f"{'sym':6s} {'side':5s} {'short':>8s} {'wide':>5s} {'cr':>5s} "
          f"{'Pwin':>6s} {'EVdem$':>8s} {'EVraw$':>8s} {'EVadj$':>8s} "
          f"{'EVfloor$':>9s} {'verdict':13s} {'w':>5s} {'realized':>9s}")

    hist, seen = {}, {}
    for (sym, strat, sk, lk, psk, plk, width, spot, sdelta, siv, credit,
         exp, exp_ret, last_ret) in rows:
        side = "call" if "CALL" in strat else "put"
        short_k = float(sk if side == "call" else psk) if (sk or psk) else None
        long_k = float(lk if side == "call" else plk) if (lk or plk) else None
        if short_k is None:
            # WEEKLY_PUT rows predate the put-strike columns being populated;
            # reconstruct from the recorded width and short delta rather than
            # dropping the row, and mark it so the reader knows.
            if not (spot and width and sdelta):
                continue
            short_k = round(float(spot) * (1.0 + float(sdelta) * 0.9), 2)
            long_k = short_k - float(width)
        if long_k is None:
            long_k = short_k + float(width) * (1 if side == "call" else -1)
        credit = float(credit or 0.0)
        if credit <= 0:
            continue

        if sym not in hist:
            hist[sym] = history(sym)
        # expiration is stored as text on older rows and as a date on newer
        # ones; both reach here.
        edt = (exp if isinstance(exp, date)
               else date(*(int(x) for x in str(exp)[:10].split("-"))))
        fwd = max(1, (edt - day).days * 5 // 7)
        dem, raw = terminals(hist[sym], day, float(spot), fwd)
        if dem is None:
            continue

        pl_dem, pl_raw = (payoff(t, side, short_k, long_k, credit)
                          for t in (dem, raw))
        if sym not in seen:
            seen[sym] = verdict_for(sym, day)
        v, conf, src = seen[sym]
        w = max(-1.0, min(1.0, NEWS_DRIFT_WEIGHT.get(v or "", 0.0) * conf))
        ev_dem, ev_raw = float(pl_dem.mean()), float(pl_raw.mean())
        ev_adj = ev_dem + w * (ev_raw - ev_dem)
        # THE DEFENSIVE READING OF THE SAME TWO NUMBERS. EVadj interpolates
        # toward the drift case when news agrees and away when it disagrees --
        # so with NO news it collapses to EVdem and throws the drift term away
        # entirely. On 2026-08-28 that is exactly what happened to the SNDK
        # short call: no headlines in the window, w = 0, EVadj = EVdem = -48.5,
        # while EVraw was -96.4, the most negative figure in the whole cohort.
        # It expired at -1011%. EVfloor takes the pessimistic side of the pair
        # instead of the interpolation, which needs no verdict at all.
        ev_floor = min(ev_dem, ev_raw)
        pwin = float((pl_dem > 0).mean()) * 100

        real = (f"{exp_ret:+8.0f}%" if exp_ret is not None else
                (f"{last_ret:+8.0f}%*" if last_ret is not None else "       -"))
        print(f"{sym:6s} {side:5s} {short_k:8.2f} {float(width):5.1f} "
              f"{credit:5.2f} {pwin:5.1f}% {ev_dem:8.1f} {ev_raw:8.1f} "
              f"{ev_adj:8.1f} {ev_floor:9.1f} {(v or '-'):13s} {w:+5.2f} "
              f"{real:>9s}")
    for sym, (v, conf, src) in sorted(seen.items()):
        print(f"       {sym}: {v or 'no verdict'} ({src})")
    print("\n* = open, marked. EVadj sits between EVdem and EVraw BY "
          "CONSTRUCTION; the test is whether it moves toward the realized "
          "outcome, not whether it is large.")


def track(sym: str, start: date, end: date):
    """Day by day through the holding period: what the news said, and how far
    the name had travelled. This is the POSITION REVIEW question, which is
    different from the entry question and is where a guard earns its place."""
    h = history(sym)
    print(f"\n{'='*100}")
    print(f"{sym} DAY BY DAY, {start} to {end} — the review a guard would have run")
    print("="*100)
    print(f"{'day':12s} {'close':>9s} {'move':>8s} {'ATR':>6s} {'n':>3s} "
          f"{'verdict':14s} {'conf':>5s}  rationale")
    d = start
    while d <= end:
        if d in h.index:
            row = h.loc[d]
            c0 = float(h.loc[[x for x in h.index if x < d][-1], "Close"])
            c = float(row["Close"])
            atr = float(row["atr14"]) if row["atr14"] == row["atr14"] else float("nan")
            heads = session_headlines(sym, d)
            if heads:
                g = classify_day(sym, d)
                v, conf, n = g["verdict"], g["confidence"], g["headline_count"]
                why = (g["rationale"] or "")[:52]
            else:
                v, conf, n, why = "(no news)", 0.0, 0, ""
            print(f"{str(d):12s} {c:9.2f} {(c/c0-1)*100:+7.2f}% "
                  f"{(c-c0)/atr:+6.2f} {n:3d} {v:14s} {conf:5.2f}  {why}")
        d += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="cohort open date YYYY-MM-DD")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--track", default="", help="also walk this symbol day by day")
    ap.add_argument("--track-days", type=int, default=7)
    args = ap.parse_args()
    day = date(*(int(x) for x in args.day.split("-")))
    syms = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}

    run_cohort(day, syms, args)
    if args.track:
        track(args.track.upper(), day, day + timedelta(days=args.track_days))


if __name__ == "__main__":
    main()
