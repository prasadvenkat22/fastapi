"""Score both shadow books by the implied-to-realised ratio at entry.  Section 213.

    docker compose ... exec -T -w /app -e PYTHONPATH=/app app python scripts/shadow_iv_rv_report.py

The question behind the credit branch: does a rich implied vol at entry make
SELLING a spread pay, and a cheap one make BUYING pay? Two paper books already
record what is needed and neither had a report that asked:

    weekly_shadow   Friday credit verticals at ~0.12 delta on fifteen names,
                    settled at expiry; stores sig_rv_iv_ratio (RV over IV --
                    inverted here so every table reads IV/RV like the board)
    dte0_shadow     same-day verticals on the Mon/Wed/Fri names, structure
                    CHOSEN by iv_rv_ratio at 09:45 (>= 1.05 credit, <= 0.95
                    debit), both directions recorded every session

Buckets match the board's vol_regime column: CHEAP <= 0.8, FAIR, RICH >= 1.2.
Return is the book's own expiry_return_pct: for a credit row, the share of
the credit kept (100 = all of it, negative = the wing was breached); for a
debit row, the return on the debit paid.

Read the counts before the averages. A bucket of two is a bucket of two.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return (url.replace("postgresql+psycopg2://", "postgresql://")
               .replace("postgresql+asyncpg://", "postgresql://"))


def bucket(iv_rv: "float | None") -> str:
    if iv_rv is None:
        return "no read"
    if iv_rv >= 1.2:
        return "RICH  >= 1.2"
    if iv_rv <= 0.8:
        return "CHEAP <= 0.8"
    return "FAIR"


def table(title: str, rows: list) -> None:
    """rows: (group, iv_rv, return_pct). Prints n / avg / median / win / worst per (group, bucket)."""
    agg = defaultdict(list)
    for group, iv_rv, ret in rows:
        agg[(group, bucket(iv_rv))].append(float(ret))
    print(f"\n{title}: {len(rows)} settled rows")
    print("  %-22s %-14s %4s %9s %9s %5s %9s" % ("group", "IV/RV at entry", "n", "avg %", "median %", "win", "worst %"))
    for (group, b), rets in sorted(agg.items()):
        rets.sort()
        n = len(rets)
        print("  %-22s %-14s %4d %+9.1f %+9.1f %4.0f%% %+9.1f" % (
            group, b, n, sum(rets) / n, rets[n // 2], 100.0 * sum(r > 0 for r in rets) / n, rets[0]))


def main() -> None:
    with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute("""SELECT strategy, sig_rv_iv_ratio, expiry_return_pct
                       FROM weekly_shadow
                       WHERE expiry_return_pct IS NOT NULL
                         AND strategy IN ('WEEKLY_CALL', 'WEEKLY_PUT')""")
        weekly = [(s.replace("WEEKLY_", "sell ").lower() + "s",
                   (1.0 / float(r)) if r else None, ret) for s, r, ret in cur.fetchall()]
        table("WEEKLY credit shadow (0.12-delta verticals, five-day hold)", weekly)

        cur.execute("""SELECT structure, variant, iv_rv_ratio, expiry_return_pct
                       FROM dte0_shadow WHERE expiry_return_pct IS NOT NULL""")
        dte0 = [(f"{str(st).lower()} {str(v).lower()}", (float(r) if r is not None else None), ret)
                for st, v, r, ret in cur.fetchall()]
        table("0DTE shadow (structure chosen BY the ratio: credit when rich, debit when cheap)", dte0)

        cur.execute("SELECT count(*), count(expiry_return_pct), min(trading_day), max(trading_day) FROM dte0_shadow")
        n, settled, lo, hi = cur.fetchone()
        print(f"\n0DTE shadow: {n} rows, {settled} settled, {lo} to {hi}")
        cur.execute("SELECT count(*), count(expiry_return_pct), min(opened_at)::date, max(opened_at)::date FROM weekly_shadow")
        n, settled, lo, hi = cur.fetchone()
        print(f"weekly shadow: {n} rows, {settled} settled, {lo} to {hi}")


if __name__ == "__main__":
    main()
