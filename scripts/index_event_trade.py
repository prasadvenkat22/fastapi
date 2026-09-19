"""The index-event rule: a call debit into a scheduled inclusion. Section 197.

    python scripts/index_event_trade.py            # act on active events
    python scripts/index_event_trade.py --backfill 30   # scan stored headlines
    python scripts/index_event_trade.py --list

WHAT IT DOES. For every JOIN event whose effective close is still ahead, and
not on the effective day itself, pick the best call debit spread on the name
by the screener's edge ranking (same maths as /trading/screener/verticals):
expiry the latest available on or before the effective close, R:R between
1 and 3, Pwin at least MIN_PWIN, EV_adj above zero, one contract costing at
most the budget. One trade per event, recorded on the event row.

WHY NOT ON THE EFFECTIVE DAY. The funds buy at that close and the demand ends
there; section 193's 540-day study found the closing tape predicts nothing
for the next session and leans negative over five. The window is
announcement to the day before.

LIVE ONLY WHEN TRADING_INDEX_EVENT_LIVE=true. Otherwise the pick is logged
as "would place". Measured in the literature, not on this account -- one
event (SanDisk, 09-04 -> 09-18, low 1500s -> 1793) is a story, not a sample.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2  # noqa: E402

from trading_engine import index_events, tradier_orders  # noqa: E402
from trading_engine.screener import rank  # noqa: E402

logger = logging.getLogger("index_event_trade")
LIVE = os.getenv("TRADING_INDEX_EVENT_LIVE", "false").lower() == "true"
BUDGET = float(os.getenv("TRADING_INDEX_EVENT_BUDGET", "1000"))
MIN_PWIN = float(os.getenv("TRADING_INDEX_EVENT_MIN_PWIN", "0.40"))
MIN_DAYS = int(os.getenv("TRADING_INDEX_EVENT_MIN_DAYS", "1"))   # expiry at least this many days out


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def backfill(conn, days: int) -> int:
    with conn.cursor() as cur:
        cur.execute("""SELECT guid, source, title, coalesce(published, first_seen)
                       FROM news_seen WHERE coalesce(published, first_seen) >= now() - %s * interval '1 day'""",
                    (days,))
        rows = cur.fetchall()
        cur.execute("""SELECT id::text, source, headline_text, publication_date
                       FROM market_news_vectors WHERE publication_date >= now() - %s * interval '1 day'""",
                    (days,))
        rows += cur.fetchall()
    n = index_events.record(conn, index_events.scan(rows))
    print(f"scanned {len(rows)} stored headlines over {days} days: {n} new event row(s)")
    return n


def _held(symbol: str) -> bool:
    try:
        return any(str(p.get("symbol", "")).startswith(symbol.upper())
                   for p in (tradier_orders.open_positions() or []))
    except Exception:
        return False


def _expiry_for(symbol: str, effective: date) -> "str | None":
    """Latest listed expiry on or before the effective close, at least MIN_DAYS out."""
    try:
        exps = sorted(str(e) for e in (tradier_orders.expirations(symbol) or []))
    except Exception:
        exps = []
    floor = (date.today() + timedelta(days=MIN_DAYS)).isoformat()
    ok = [e for e in exps if floor <= e <= effective.isoformat()]
    return ok[-1] if ok else None


def act(conn) -> None:
    today = date.today()
    events = index_events.active(conn, today)
    if not events:
        print("no active index events")
        return
    for ev in events:
        sym, eff = ev["symbol"], ev["effective_date"]
        tag = f"{sym} -> {ev['index_name']} effective close {eff}"
        if ev.get("traded_order_id"):
            print(f"{tag}: already traded (order {ev['traded_order_id']})")
            continue
        if eff <= today:
            print(f"{tag}: effective day — the demand ends at this close; no new long")
            continue
        if _held(sym):
            print(f"{tag}: account already holds {sym} — skipped")
            continue
        exp = _expiry_for(sym, eff)
        if not exp:
            print(f"{tag}: no expiry between +{MIN_DAYS}d and the effective close — skipped")
            continue
        res = rank([sym], "call", by="edge", top=5, rr_min=1.0, rr_max=3.0,
                   structure="debit", per_symbol=5, expiry=exp)
        rows = [r for r in (res.get("rows") or [])
                if (r.get("cost") or 0) * 100 <= BUDGET and r["pwin"] >= MIN_PWIN
                and (r.get("ev_adj", r["ev_dem"]) or 0) > 0]
        if not rows:
            print(f"{tag}: nothing on the {exp} board clears Pwin>={MIN_PWIN:.2f}, EV>0, "
                  f"cost<=${BUDGET:.0f}")
            continue
        r = rows[0]
        line = (f"{tag}: {'PLACE' if LIVE else 'would place'} {sym} CALL {r['lo']:g}/{r['hi']:g} "
                f"exp {exp} x1 @ {r['cost']:.2f} | Pwin {100*r['pwin']:.1f}% need {100*r['need']:.1f}% "
                f"edge {100*(r['pwin']-r['need']):+.1f} EV {r.get('ev_adj', r['ev_dem']):+.0f}")
        print(line); logger.info(line)
        if not LIVE:
            continue
        try:
            out = tradier_orders.submit_vertical(sym, exp, "call", long_strike=float(r["lo"]),
                                                 short_strike=float(r["hi"]), quantity=1,
                                                 opening=True, limit_price=float(r["cost"]),
                                                 is_credit=False, preview=False)
            oid = str((out or {}).get("id") or "")
            with conn.cursor() as cur:
                cur.execute("UPDATE index_events SET traded_order_id=%s, traded_at=now() WHERE id=%s",
                            (oid or "sent", ev["id"]))
            conn.commit()
            print(f"   ORDER SENT: {out}")
        except Exception as exc:
            print(f"   order FAILED: {type(exc).__name__}: {exc}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    conn = psycopg2.connect(_dsn())
    try:
        if args.backfill:
            backfill(conn, args.backfill)
        if args.list or args.backfill:
            with conn.cursor() as cur:
                cur.execute("SELECT symbol, index_name, action, announced_at::date, effective_date, basis, left(headline,80), traded_order_id FROM index_events ORDER BY effective_date DESC LIMIT 20")
                for row in cur.fetchall():
                    print("  ", row)
            if not args.backfill:
                return
            return
        act(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
