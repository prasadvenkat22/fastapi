"""What the VWAP flow gate would have said at each past rotation entry, and
what that entry then did. Section 194.

    docker compose ... exec -T -w /app -e PYTHONPATH=/app app \\
        python scripts/flow_gate_replay.py /app/.dte0-trade.log.copy

The rotation log lives on the HOST (/var/log/dte0-trade.log) and the Tradier
client and the database live in the container, so copy the log into the bind
mount first:  cp /var/log/dte0-trade.log /opt/fastapi/.dte0-trade.log.copy

For every pick line the rotation actually sent an order for, this rebuilds the
session's 5-minute bars up to the entry minute, runs the gate exactly as
dte0_trade now does, and joins the realised result from trading_history by
(day, underlying, strikes). Tradier keeps 5-minute bars for about 40 days,
which covers the whole life of the rotation.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, "/app")
from trading_engine import vwap_gate  # noqa: E402

PICK = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d):(\d\d):\d\d,\d+ ([A-Z]+)\s+(CALL|PUT)\s+"
                  r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?) w[\d.]+ x(\d+) @ ([\d.]+)")


def entries(path: str) -> list:
    out, pending = [], None
    for line in open(path, encoding="utf-8", errors="replace"):
        m = PICK.match(line)
        if m:
            pending = m.groups()
            continue
        if pending and ("ORDER SENT" in line or "Order submitted" in line):
            out.append(pending)
            pending = None
    return out


def outcome(cur, day, sym, lo, hi):
    cur.execute("""SELECT coalesce(sum(realized_pnl_dollars),0), count(*)
                   FROM trading_history
                   WHERE underlying=%s AND abs(long_strike-%s) < 0.6 AND abs(short_strike-%s) < 0.6
                     AND (opened_at::date=%s OR closed_at::date=%s)""",
                (sym, lo, hi, day, day))
    pnl, n = cur.fetchone()
    return (float(pnl), int(n)) if n else (None, 0)


def main():
    path = sys.argv[1]
    import psycopg2
    from scripts.news_hourly import _dsn
    picks = entries(path)
    print(f"{len(picks)} rotation entries with an order sent")
    bars_cache: dict = {}
    rows = []
    with psycopg2.connect(_dsn()) as c, c.cursor() as cur:
        for day, hh, mm, sym, side, lo, hi, qty, px in picks:
            key = (sym, day)
            if key not in bars_cache:
                try:
                    bars_cache[key] = vwap_gate.bars_for(sym, day)
                except Exception as e:
                    bars_cache[key] = []
                    print(f"  {sym} {day}: bars unavailable ({type(e).__name__})")
            # log is UTC; bar times are ET
            t_et = datetime.strptime(f"{day} {hh}:{mm}", "%Y-%m-%d %H:%M") - timedelta(hours=4)
            upto = [b for b in bars_cache[key]
                    if b["time"] and datetime.fromisoformat(b["time"][:16]) <= t_et]
            flow = vwap_gate.flow_from_bars(upto)
            ok, why = vwap_gate.gate("bullish" if side == "CALL" else "bearish", flow)
            pnl, n = outcome(cur, day, sym, float(lo), float(hi))
            rows.append((day, t_et.strftime("%H:%M"), sym, side, lo, hi, ok, pnl, why))
    print()
    print("%-10s %-5s %-5s %-4s %-11s %-6s %9s  %s" % ("day", "ET", "sym", "side", "strikes", "gate", "pnl", "why (if refused)"))
    for r in rows:
        print("%-10s %-5s %-5s %-4s %-11s %-6s %9s  %s" % (
            r[0], r[1], r[2], r[3], f"{r[4]}/{r[5]}", "ok" if r[6] else "REFUSE",
            ("%+.0f" % r[7]) if r[7] is not None else "n/a", "" if r[6] else r[8][:70]))
    agg = defaultdict(lambda: [0, 0.0, 0])
    for r in rows:
        if r[7] is None:
            continue
        k = "allowed" if r[6] else "refused"
        agg[k][0] += 1; agg[k][1] += r[7]; agg[k][2] += (r[7] > 0)
    print()
    print("%-8s %4s %10s %9s %5s" % ("gate", "n", "total", "avg", "win"))
    for k, (n, tot, w) in sorted(agg.items()):
        print("%-8s %4d %+10.0f %+9.0f %4.0f%%" % (k, n, tot, tot / n, 100 * w / n))
    unk = sum(1 for r in rows if r[7] is None)
    if unk:
        print(f"{unk} entries with no trading_history match (still open, expired, or closed by hand)")


if __name__ == "__main__":
    main()
