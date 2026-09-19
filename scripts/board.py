"""The board: best call and put debit spreads by the ranker, with the why.

    python scripts/board.py 2026-09-21            # a 0DTE/near expiry
    python scripts/board.py 2026-09-25 --weekly   # the full weekly universe

Same maths as /trading/screener/verticals and weekly_pick.py (one import,
trading_engine.screener.rank), printed as the endpoint would return it:
edge-sorted, R:R band 1..3 (the moderate geometry), two rows per name. news
and flow are shown beside the row and do not move it (sections 22, 130).
"""
import sys

sys.path.insert(0, "/app")
from trading_engine.screener import rank  # noqa: E402

MON = ["MU", "NVDA", "TSLA", "AMZN", "AAPL", "META", "MSFT", "GOOGL", "AMD", "AVGO", "INTC"]
WK = MON + ["SNDK", "CRWV", "MRVL", "PANW", "DELL", "STX", "WDC"]


def show(title, res):
    rows = res.get("rows") or []
    print("==", title, "| rows", len(rows), "| warnings", (res.get("warnings") or [])[:3])
    if rows:
        print("   fields:", sorted(rows[0].keys()))
    hdr = "   %-5s %-11s %6s %6s %6s %6s %7s %7s %8s %-9s %s"
    print(hdr % ("sym", "strikes", "cost", "width", "Pwin", "need", "edge", "EV", "EVadj", "news", "conflict / flow"))
    for r in rows:
        cost = r.get("cost", r.get("debit", 0)) or 0
        print("   %-5s %-11s %6.2f %6.1f %5.1f%% %5.1f%% %+6.1fp %+7.1f %+8.1f %-9s %s" % (
            r["sym"], "%g/%g" % (r["lo"], r["hi"]), cost, abs(r["hi"] - r["lo"]),
            100 * r["pwin"], 100 * r["need"], 100 * (r["pwin"] - r["need"]),
            r["ev_dem"], r.get("ev_adj", r["ev_dem"]), str(r.get("news") or "-"),
            (str(r.get("conflict") or "")[:40] + " " + str(r.get("flow") or "")).strip()))


def main():
    expiry = sys.argv[1]
    syms = WK if "--weekly" in sys.argv else MON
    for side in ("call", "put"):
        show("%s %s DEBIT, by edge, R:R 1..3" % (expiry, side.upper()),
             rank(syms, side, by="edge", top=8, rr_min=1.0, rr_max=3.0,
                  structure="debit", per_symbol=2, expiry=expiry))


if __name__ == "__main__":
    main()
