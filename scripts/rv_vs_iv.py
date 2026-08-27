"""Realized versus implied volatility, per symbol, period-matched.

The question this exists to answer
----------------------------------
Every premium-selling strategy in this repo has lost, and the arithmetic that
explains it is that a credit spread's break-even win rate IS its risk ratio
while delta IS the market's probability estimate. Selling a market-priced
option is a fair bet before costs and a losing one after.

The only thing that makes it a WINNING bet is the variance risk premium --
implied vol systematically exceeding what the underlying goes on to realise.
So the question that decides whether a single-name book is worth building is
not "which name has the richest premium" but "on which names, if any, does
implied exceed realised".

A rough version was run on 2026-08-27 comparing THAT DAY's implied against
six months of trailing realised, and ten of eleven names came back with
realised HIGHER -- ratios of 1.17 to 1.93 and breach rates of 36-60% against
the ~30% a 0.15-delta condor expects. That comparison is not sound: one day's
implied against six months of realised is two different periods, and the
realised window contains an earnings gap for every name.

This script does it period-matched. For each snapshot it reads the implied
vol the market was quoting for a horizon, then measures what the underlying
actually did over exactly that horizon, and reports the ratio.

    python scripts/rv_vs_iv.py                    # all symbols, 4-day horizon
    python scripts/rv_vs_iv.py --horizon 7        # a week
    python scripts/rv_vs_iv.py --symbols MU,DELL

What it needs, and why it cannot be run retroactively
-----------------------------------------------------
Implied vol history. There is no free historical option-chain feed, which is
why scripts/capture_chain.py records snapshots forward four times a session.
Until enough of those accumulate this prints small samples and says so.

READ THE SAMPLE COLUMN BEFORE THE RATIO. Overlapping horizons mean 20 rows
covering 24 sessions are nowhere near 20 independent observations, and a
ratio computed on four pairs is noise wearing a decimal point.
"""
import argparse
import json
import math
import os
import statistics as st
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NY = ZoneInfo("America/New_York")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_PATH = os.getenv(
    "CHAIN_SNAPSHOT_PATH", os.path.join(REPO_ROOT, "data", "qqq-chain-snapshots.jsonl")
)

# A 0.15-delta strike sits about this many standard deviations out, which is
# what turns a vol ratio into an expected breach rate.
DELTA_15_SD = 1.036

# Trading days a year, for annualising. 252 rather than 365: realised vol is
# measured over trading sessions and implied is quoted annualised on the same
# convention.
TRADING_DAYS = 252


def load_snapshots(path):
    """Every snapshot line, newest schema and the older QQQ-only one.

    Lines written before 2026-08-26 carry `spot`/`expiries` at the top level
    and are QQQ only; later lines carry a `symbols` list. Both are real
    observations and both are used.
    """
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            snap = json.loads(line)
        except ValueError:
            continue
        ts = snap.get("ts")
        if "symbols" in snap:
            for sym in snap["symbols"]:
                out.append((ts, sym["symbol"], sym["spot"], sym["expiries"]))
        elif "expiries" in snap:
            out.append((ts, "QQQ", snap.get("spot"), snap["expiries"]))
    return out


def atm_iv(expiries, spot, horizon_days, tolerance=2.0):
    """ATM implied vol for the expiry closest to the horizon.

    At the money rather than at the 0.15-delta strike: the skew makes the
    wing IV a statement about the wing, and this is asking about the
    underlying's expected movement.
    """
    best, best_gap = None, 9e9
    for e in expiries:
        days = e["minutes"] / 1440.0
        gap = abs(days - horizon_days)
        if gap < best_gap and gap <= tolerance:
            best, best_gap = e, gap
    if best is None:
        return None, None
    near = [r for r in best["rows"] if abs(r[0] - spot) <= max(spot * 0.01, 1.0)]
    if not near:
        return None, None
    return st.mean(r[4] for r in near), best["minutes"] / 1440.0


def daily_closes(symbol, rng="6mo"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol, safe='')}?range={rng}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    r = json.load(urllib.request.urlopen(req, timeout=30))["chart"]["result"][0]
    stamps = r["timestamp"]
    closes = r["indicators"]["quote"][0]["close"]
    return [(datetime.fromtimestamp(t, NY).date(), c)
            for t, c in zip(stamps, closes) if c is not None]


def realised_move(closes, start_date, horizon_days):
    """The underlying's actual log move over `horizon_days` trading days
    starting at the first session on or after `start_date`."""
    idx = next((i for i, (d, _) in enumerate(closes) if d >= start_date), None)
    if idx is None or idx + horizon_days >= len(closes):
        return None
    a, b = closes[idx][1], closes[idx + horizon_days][1]
    if a <= 0 or b <= 0:
        return None
    return math.log(b / a)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=int, default=4,
                    help="trading days to compare over (default 4)")
    ap.add_argument("--symbols", default="",
                    help="comma list; default is every symbol in the snapshots")
    ap.add_argument("--path", default=SNAPSHOT_PATH)
    args = ap.parse_args()

    snaps = load_snapshots(args.path)
    if not snaps:
        raise SystemExit(f"No snapshots at {args.path} — has capture_chain.py run?")

    wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    by_symbol = {}
    for ts, sym, spot, expiries in snaps:
        if wanted and sym not in wanted:
            continue
        by_symbol.setdefault(sym, []).append((ts, spot, expiries))

    print(f"Realised vs implied over a {args.horizon}-trading-day horizon")
    print(f"snapshots: {args.path}\n")
    print(f"{'sym':6s} {'pairs':>6s} {'sessions':>9s} {'impl move':>10s} "
          f"{'real move':>10s} {'RV/IV':>7s} {'breach':>7s} {'expected':>9s}  verdict")

    for sym in sorted(by_symbol):
        rows = by_symbol[sym]
        try:
            closes = daily_closes(sym)
        except Exception:
            print(f"{sym:6s} price history unavailable")
            continue

        pairs = []
        for ts, spot, expiries in rows:
            iv, tenor = atm_iv(expiries, spot, args.horizon)
            if iv is None:
                continue
            when = datetime.fromisoformat(ts).date()
            move = realised_move(closes, when, args.horizon)
            if move is None:
                continue
            implied_sd = iv * math.sqrt(args.horizon / TRADING_DAYS)
            if implied_sd <= 0:
                continue
            pairs.append((implied_sd, abs(move)))

        if len(pairs) < 4:
            print(f"{sym:6s} {len(pairs):6d} {'':>9s}  too few pairs to say anything")
            continue

        sessions = len({datetime.fromisoformat(t).date() for t, _, _ in rows})
        imp = st.mean(p[0] for p in pairs)
        # Realised "sd" from absolute moves: E|X| = sd * sqrt(2/pi) for a
        # normal, so scale back up rather than comparing |move| to an sd.
        rea = st.mean(p[1] for p in pairs) * math.sqrt(math.pi / 2)
        breach = len([p for p in pairs if p[1] > DELTA_15_SD * p[0]]) / len(pairs) * 100
        ratio = rea / imp if imp else 0.0
        # Two-sided 0.15 delta: about 30% of horizons breach if IV is fair.
        verdict = "sells well" if ratio < 0.95 else ("fair" if ratio < 1.05 else "MOVES MORE")
        print(f"{sym:6s} {len(pairs):6d} {sessions:9d} {imp*100:9.2f}% {rea*100:9.2f}% "
              f"{ratio:7.2f} {breach:6.0f}% {30:8d}%  {verdict}")

    print()
    print("  RV/IV below 1.00 is the variance risk premium -- the only thing that")
    print("  makes selling a market-priced option a winning bet rather than a fair")
    print("  one. Above 1.00 the underlying moves more than the premium pays for,")
    print("  and no strike, width or stop fixes that.")
    print()
    print("  READ 'pairs' AND 'sessions' FIRST. Horizons overlap, so 40 pairs")
    print("  across 10 sessions is closer to 2 independent observations than 40.")


if __name__ == "__main__":
    main()
