"""Trailing PROFIT stop: sell after giving back N% of the peak profit.

Not measured from cost -- from the highest profit the position ever showed.
The rule only arms once there IS a profit to give back; a position that never
rose above entry can never trigger it.

Two variants, because "profit" is ambiguous on an in-the-money spread:

  MARK      profit you could actually realise now (value - entry). On these
            spreads the mark sits under entry most of the day, so a peak
            profit rarely exists and the rule rarely arms.
  INTRINSIC profit at expiry (intrinsic - entry). Exists almost immediately on
            an ITM spread, so the rule arms early and often.

Hold is valued at true expiry against the underlying's close (section 81).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep_giveback import load, pnl, expiry_value


def run_trail(rows, pct, basis):
    ent = rows[0]["entry"]
    credit = rows[0]["credit"]
    peak = None
    for idx, r in enumerate(rows):
        v = r["value"] if basis == "mark" else r["intr"]
        prof = ((ent - v) if credit else (v - ent))
        if peak is None or prof > peak:
            peak = prof
        if peak > 0 and prof <= peak * (1.0 - pct / 100.0):
            return r["value"], idx
    return None, None


def main():
    series = {k: v for k, v in load(sys.argv[1]).items() if len(v) >= 10}
    hold_total = 0.0
    for (day, root, right, lo, hi, _e), rs in series.items():
        ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
        if ev is not None:
            hold_total += pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
    print(f"  {len(series)} structure-days.  hold to expiry = {hold_total:+.0f}\n")
    print("  %-10s %-6s %6s %6s %10s %11s" % (
        "giveback", "basis", "fires", "helps", "P&L", "vs hold"))
    for basis in ("mark", "intrinsic"):
        for pct in (5, 10, 20, 30, 50):
            tot = 0.0
            fires = helps = 0
            for (day, root, right, lo, hi, _e), rs in series.items():
                ev = expiry_value(root, day, right, lo, hi, rs[-1]["credit"])
                if ev is None:
                    continue
                hold = pnl(rs[-1]["entry"], ev, rs[-1]["qty"], rs[-1]["credit"])
                v, i = run_trail(rs, pct, basis)
                if v is None:
                    tot += hold
                else:
                    fires += 1
                    got = pnl(rs[i]["entry"], v, rs[i]["qty"], rs[i]["credit"])
                    if got > hold:
                        helps += 1
                    tot += got
            print("  %-10s %-6s %6d %6d %+10.0f %+11.0f" % (
                f"{pct}%", basis, fires, helps, tot, tot - hold_total))
        print()


if __name__ == "__main__":
    main()
