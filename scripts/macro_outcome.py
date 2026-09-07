"""Record the morning's macro read against the session that followed, and
report on the accumulated rows.

WHY THIS EXISTS. TRADING_MACRO_LLM_GATE was switched off on 2026-08-25 after
refusing 55 of 55 cycles on a day QQQ rose $6.50 off its low. That was a live
observation, not a harness artifact, and it was still two days -- reconstructed
by elimination, because the verdict was overwritten in one row instead of kept.

Deciding whether a macro read should gate, scale or be ignored needs the two
questions August could not answer:

    1. did BEARISH mornings precede losing sessions?
    2. how many WINNING sessions would a gate have refused?

The second killed the gate and is the one a SIZE SCALER answers differently
from a refusal: scaling down a winner costs part of it, refusing costs all.

    python scripts/macro_outcome.py            # record today after the close
    python scripts/macro_outcome.py --backfill 60
    python scripts/macro_outcome.py --report
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import psycopg2
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NY = ZoneInfo("America/New_York")


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def _qqq_frame():
    h = yf.Ticker("QQQ").history(period="1y", interval="1d")
    H, L, C = h["High"], h["Low"], h["Close"]
    pc = C.shift(1)
    tr = (H - L).combine((H - pc).abs(), max).combine((L - pc).abs(), max)
    h = h.copy()
    h["atr14"] = tr.rolling(14).mean()
    h.index = [d.date() for d in h.index]
    return h


def record(day: date, frame, cur) -> Optional[dict]:
    """One row. Verdict columns stay NULL when no read exists for that day --
    a null says 'not measured', which is not the same claim as 'NEUTRAL'."""
    if day not in frame.index:
        return None
    row = frame.loc[day]
    o, c = float(row["Open"]), float(row["Close"])
    atr = float(row["atr14"]) if row["atr14"] == row["atr14"] else None

    cur.execute("SELECT verdict, confidence, headline_count FROM news_verdicts "
                "WHERE symbol='QQQ' AND trading_day=%s", (day,))
    nv = cur.fetchone()

    # The macro gate verdict nearest the open, if the engine ran that day.
    cur.execute(
        "SELECT verdict, confidence FROM trading_macro_verdicts "
        "WHERE (recorded_at AT TIME ZONE 'America/New_York')::date = %s "
        "ORDER BY recorded_at ASC LIMIT 1", (day,))
    mg = cur.fetchone()

    cur.execute(
        "SELECT count(*), COALESCE(sum(realized_pnl_dollars), 0) "
        "FROM trading_history WHERE (closed_at AT TIME ZONE 'America/New_York')::date = %s "
        "AND playbook NOT LIKE 'MANUAL%%'", (day,))
    n_tr, pnl = cur.fetchone()

    rec = dict(
        trading_day=day,
        qqq_news_verdict=nv[0] if nv else None,
        qqq_news_confidence=float(nv[1]) if nv and nv[1] is not None else None,
        qqq_news_headlines=int(nv[2]) if nv and nv[2] is not None else None,
        macro_gate_verdict=mg[0] if mg else None,
        macro_gate_confidence=float(mg[1]) if mg and mg[1] is not None else None,
        qqq_open=round(o, 4), qqq_close=round(c, 4),
        qqq_ret_pct=round((c / o - 1) * 100, 4),
        qqq_move_atr=round((c - o) / atr, 4) if atr else None,
        qqq_atr14=round(atr, 4) if atr else None,
        engine_trades=int(n_tr or 0), engine_pnl=float(pnl or 0.0),
    )
    cols = ", ".join(rec)
    ph = ", ".join(["%s"] * len(rec))
    upd = ", ".join(f"{k}=EXCLUDED.{k}" for k in rec if k != "trading_day")
    cur.execute(
        f"INSERT INTO macro_session_outcomes ({cols}) VALUES ({ph}) "
        f"ON CONFLICT (trading_day) DO UPDATE SET {upd}", list(rec.values()))
    return rec


def report(cur) -> None:
    cur.execute(
        "SELECT trading_day, qqq_news_verdict, qqq_news_confidence, "
        "macro_gate_verdict, qqq_ret_pct, qqq_move_atr, engine_trades, engine_pnl "
        "FROM macro_session_outcomes ORDER BY trading_day")
    rows = cur.fetchall()
    if not rows:
        print("no rows yet")
        return
    graded = [r for r in rows if r[1]]
    print(f"{len(rows)} sessions recorded, {len(graded)} with a QQQ news verdict\n")

    if not graded:
        print("NO GRADED SESSIONS YET. The outcome side is backfilled and the "
              "verdict side starts accumulating from the first 09:30 run, so "
              "this report becomes answerable about twenty sessions after that.")
    else:
        print("QUESTION 1 -- did the verdict separate the sessions?")
        print(f"  {'verdict':14s} {'n':>3s} {'mean QQQ%':>10s} {'mean ATR':>9s} "
              f"{'up days':>8s} {'engine P&L':>11s}")
        for v in ("VERY_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "VERY_BEARISH"):
            sel = [r for r in graded if r[1] == v]
            if not sel:
                continue
            rets = [r[4] for r in sel if r[4] is not None]
            atrs = [r[5] for r in sel if r[5] is not None]
            pnl = [r[7] for r in sel if r[7] is not None]
            print(f"  {v:14s} {len(sel):3d} {np.mean(rets):9.2f}% "
                  f"{np.mean(atrs) if atrs else float('nan'):9.2f} "
                  f"{sum(1 for x in rets if x > 0)/len(rets)*100:7.0f}% "
                  f"{np.sum(pnl):+11.2f}")

        print("\nQUESTION 2 -- what would a GATE have cost?")
        bear = [r for r in graded if r[1] in ("BEARISH", "VERY_BEARISH")]
        refused_wins = [r for r in bear if (r[7] or 0) > 0]
        avoided_loss = [r for r in bear if (r[7] or 0) < 0]
        print(f"  bearish sessions           {len(bear)}")
        print(f"  ...that the engine WON     {len(refused_wins)}  "
              f"forgone {sum(r[7] for r in refused_wins):+.2f}")
        print(f"  ...that the engine LOST    {len(avoided_loss)}  "
              f"avoided  {sum(r[7] for r in avoided_loss):+.2f}")
        net = -sum(r[7] for r in bear)
        print(f"  NET effect of refusing every bearish morning: {net:+.2f}")
        print("\n  A gate is worth having only if that net is positive by more "
              "than the sample can explain. Refusing costs the whole of a "
              "winning session; scaling down costs part of it, which is why "
              "the two need judging separately.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0,
                    help="also record the last N calendar days")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    cur = conn.cursor()

    if args.report:
        report(cur)
        cur.close(); conn.close()
        return

    frame = _qqq_frame()
    today = datetime.now(NY).date()
    days = [today]
    if args.backfill:
        days = [today - timedelta(days=i) for i in range(args.backfill)]
    n = 0
    for d in sorted(days):
        r = record(d, frame, cur)
        if r:
            n += 1
            if args.backfill == 0:
                print(f"{d}  QQQ {r['qqq_ret_pct']:+.2f}% "
                      f"({r['qqq_move_atr'] if r['qqq_move_atr'] is not None else float('nan'):+.2f} ATR)  "
                      f"news {r['qqq_news_verdict'] or '-'}  "
                      f"gate {r['macro_gate_verdict'] or '-'}  "
                      f"engine {r['engine_trades']} tr {r['engine_pnl']:+.2f}")
    if args.backfill:
        print(f"{n} sessions recorded")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
