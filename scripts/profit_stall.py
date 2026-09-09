"""Watch open positions for a WINNER that is turning, and say so.

    python scripts/profit_stall.py                       # report, no alert
    python scripts/profit_stall.py --giveback 5          # alert on -5% from peak
    python scripts/profit_stall.py --giveback 5 --status
    python scripts/profit_stall.py --reset

IT ALERTS. IT DOES NOT TRADE. Every exit in this repository that fires by
itself lives in trading_engine/orphans.py behind measured settings; this is a
watcher, and the decision stays with the person reading the mail.

WHY A GIVEBACK AND NOT A STOP. A stop fires on losers. A stall fires on
WINNERS, which is where giving back actually happens: a position peaks, the
catalyst passes, and the gain bleeds away while the thesis is still nominally
intact. SNDK on 2026-09-08 is the case -- it reached 1800, then fell after
2:30pm on war headlines. Nothing about the position was wrong at 2:30; what
changed is that the peak stopped being available.

TWO BASES, AND THEY DISAGREE, SO BOTH ARE PRINTED.

    mark        what the position is worth at the midpoint now. It is what
                you would actually realise, and it is NOISY: six legs at
                1.30-1.90 wide means several hundred dollars of quote noise on
                a $17k position, which is a large fraction of a 5% trigger.
    intrinsic   what it would be worth at expiry if the underlying stopped
                here. Immune to quote noise, blind to time value.

orphans.py settled this for the automated path: DECIDE ON INTRINSIC, execute at
the mark, because a structure can print a peak on one wide quote and a giveback
on the next. The same reasoning applies here, so --basis defaults to intrinsic.
The mark is shown beside it every time so the two can never silently diverge.

ONLY WINNERS ARM. A position whose peak never exceeded its cost cannot "give
back" anything -- it is simply losing, which is a stop's question and not this
one. Peak is tracked per underlying in a state file so it survives a container
recreate; without that the peak resets on every deploy and the watcher goes
quiet exactly when it matters.

AND A STALL NEEDS TIME, WHICH IS THE PART A DRAWDOWN RULE ALONE GETS WRONG.
A pure "5% below peak" fires on the first observation that clears the line,
including a single wide quote. orphans.py does not do that, and its settings
are measured rather than guessed:

    ORPHAN_LATER_STALL_MINUTES = 15      minutes since the LAST NEW HIGH
    books_a_gain                         the exit must actually realise a gain

QUIET IS THE IDEA. A position still making new highs is not stalling, whatever
its drawdown from a peak set thirty seconds ago. The clock measures time since
the peak was last SET, so a structure that keeps printing new highs never trips
regardless of how it wiggles in between.

BOOKS_A_GAIN SEPARATES THE TWO RULES CLEANLY. If the exit would realise a loss,
that is a stop's decision and not a stall's -- a stall exists to protect a gain
that is bleeding away, and firing it under water just renames a stop.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NY = ZoneInfo("America/New_York")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.getenv("PROFIT_STALL_STATE",
                       os.path.join(REPO_ROOT, "data", "profit_stall.json"))
OPEN_T, CLOSE_T = dtime(9, 30), dtime(16, 0)

OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def parse_occ(sym: str):
    """SNDK260911C01700000 -> ('SNDK', '2026-09-11', 'C', 1700.0), or None."""
    m = OCC.match(sym.strip().upper())
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    exp = f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}"
    return root, exp, cp, int(strike) / 1000.0


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def snapshot() -> dict:
    """{underlying: {cost, mark, intrinsic, spot, legs:[...]}} from the broker."""
    from trading_engine.tradier_orders import open_positions, quotes

    legs = [p for p in (open_positions() or []) if parse_occ(p.get("symbol", ""))]
    if not legs:
        return {}
    roots = sorted({parse_occ(p["symbol"])[0] for p in legs})
    q = quotes([p["symbol"] for p in legs] + roots)

    def mid(sym):
        d = q.get(sym) or {}
        b, a = float(d.get("bid") or 0), float(d.get("ask") or 0)
        if b > 0 and a > 0:
            return (b + a) / 2
        return float(d.get("last") or 0)

    out: dict = defaultdict(lambda: {"cost": 0.0, "mark": 0.0,
                                     "intrinsic": 0.0, "spot": None, "legs": []})
    for p in legs:
        root, exp, cp, strike = parse_occ(p["symbol"])
        qty = float(p.get("quantity") or 0)
        cost = float(p.get("cost_basis") or 0)
        px = mid(p["symbol"])
        spot = float((q.get(root) or {}).get("last") or 0) or None
        # Intrinsic per contract, then signed by quantity like the mark is.
        intr = 0.0
        if spot:
            intr = max(spot - strike, 0.0) if cp == "C" else max(strike - spot, 0.0)
        e = out[root]
        e["cost"] += cost
        e["mark"] += px * qty * 100.0
        e["intrinsic"] += intr * qty * 100.0
        e["spot"] = spot or e["spot"]
        e["legs"].append({"symbol": p["symbol"], "qty": qty, "mid": round(px, 2),
                          "strike": strike, "cp": cp, "exp": exp})
    return dict(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--giveback", type=float, default=0.0,
                    help="alert when value falls this %% from its peak. 0 = report only")
    ap.add_argument("--quiet-minutes", type=float, default=15.0,
                    help="minutes since the LAST NEW HIGH before a stall can "
                         "fire. Matches ORPHAN_LATER_STALL_MINUTES. A position "
                         "still making highs is not stalling; 0 disables the "
                         "wait and fires on the first observation, which is "
                         "what a single wide quote needs to fool it.")
    ap.add_argument("--basis", choices=("intrinsic", "mark"), default="intrinsic",
                    help="what DECIDES the stall; the other is printed beside it")
    ap.add_argument("--to", default=os.getenv("ALERT_EMAIL", ""))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    state = load_state()
    if args.reset:
        save_state({})
        print("peaks cleared")
        return

    now = datetime.now(NY)
    if not (args.force or args.status):
        from trading_engine import market_calendar

        if not market_calendar.is_trading_day(now.date()):
            print(f"{now:%Y-%m-%d %H:%M %Z} — not a trading day.")
            return
        if not (OPEN_T <= now.time() <= CLOSE_T):
            print(f"{now:%Y-%m-%d %H:%M %Z} — outside regular hours.")
            return

    snap = snapshot()
    if not snap:
        print("no option positions at the broker")
        return

    print(f"PROFIT STALL  {now:%Y-%m-%d %H:%M %Z}  deciding on {args.basis.upper()}"
          + (f", giveback {args.giveback:.1f}% after {args.quiet_minutes:.0f}m quiet"
             if args.giveback else ", report only"))
    print(f"{'sym':6s} {'spot':>9s} {'cost':>10s} {'mark':>10s} {'intrinsic':>10s} "
          f"{'P&L':>9s} {'peak':>10s} {'from peak':>10s} {'quiet':>8s}  state")

    fired = []
    for root in sorted(snap):
        e = snap[root]
        cost, mark, intr = e["cost"], e["mark"], e["intrinsic"]
        cur = intr if args.basis == "intrinsic" else mark
        key = f"{root}:{args.basis}"
        prev = state.get(key, {})
        prev_peak = float(prev.get("peak", cur))
        peak = max(prev_peak, cur)
        made_new_high = cur >= prev_peak or "peak_at" not in prev
        # The clock runs from the last NEW HIGH, not from when the position
        # was opened. Still climbing means still not stalling.
        peak_at = (now.isoformat() if made_new_high
                   else prev.get("peak_at", now.isoformat()))
        try:
            quiet = (now - datetime.fromisoformat(peak_at)).total_seconds() / 60.0
        except Exception:
            quiet = 0.0

        # A position that never got above its cost has nothing to give back.
        winner = peak > cost
        # ...and an exit that realises a loss is a STOP's decision, not this one.
        books_a_gain = cur > cost
        drop = ((peak - cur) / peak * 100.0) if peak > 0 else 0.0
        already = bool(prev.get("fired"))

        hit = bool(args.giveback and winner and books_a_gain
                   and drop >= args.giveback
                   and quiet >= args.quiet_minutes and not already)
        if not args.status:
            state[key] = {"peak": peak, "peak_at": peak_at,
                          "fired": already or hit,
                          "seen": now.strftime("%Y-%m-%d %H:%M %Z")}
        if hit:
            fired.append((root, cost, mark, intr, peak, drop))

        if hit:
            tag = "FIRED"
        elif already:
            tag = "fired earlier"
        elif not winner:
            tag = "not a winner yet"
        elif not books_a_gain:
            tag = "under water — a stop's job, not a stall's"
        elif args.giveback and drop >= args.giveback and quiet < args.quiet_minutes:
            tag = f"giveback met, waiting {args.quiet_minutes - quiet:.0f}m more"
        else:
            tag = "watching"
        print(f"{root:6s} {(e['spot'] or 0):9.2f} {cost:10.0f} {mark:10.0f} "
              f"{intr:10.0f} {mark-cost:+9.0f} {peak:10.0f} {drop:9.1f}% "
              f"{quiet:7.0f}m  {tag}")

    if args.status:
        return
    save_state(state)

    if not fired:
        return
    lines = [f"{r}: {args.basis} {c:.0f} -> peak {p:.0f}, now down {d:.1f}% "
             f"(mark {m:.0f}, cost {co:.0f})"
             for r, co, m, c, p, d in
             [(r, co, m, (i if args.basis == "intrinsic" else m), p, d)
              for r, co, m, i, p, d in fired]]
    subject = f"Profit stall: {', '.join(r for r, *_ in fired)} gave back {args.giveback:.0f}%"
    body = (subject + "\n\n" + "\n".join(lines)
            + f"\n\nDecided on {args.basis}; the mark is shown for each so the "
              "two can be compared.\nThis is a WATCHER. Nothing has been "
              "traded. Each name fires once until --reset.\n")
    for ln in lines:
        print("FIRED: " + ln)

    from helpers import mailer

    if args.to and mailer.send(args.to, subject, body):
        print("emailed " + args.to)
    else:
        print(f"not emailed (recipient or transport missing); recorded in {STATE_PATH}")


if __name__ == "__main__":
    main()
