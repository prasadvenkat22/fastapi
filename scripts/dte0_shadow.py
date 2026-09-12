"""A paper 0DTE book for the names with Monday and Wednesday expiries.

    python scripts/dte0_shadow.py --open      # 09:45 ET, once per session
    python scripts/dte0_shadow.py --mark      # during the session
    python scripts/dte0_shadow.py --settle    # after the close
    python scripts/dte0_shadow.py --report

IT NEVER TRADES. No order path, no broker call, no live slice. The engine's
0DTE record is QQQ-only and thin -- MORNING_DRIFT 6 live trades,
AFTERNOON_CREDIT 7 and negative -- and this repository holds no single-name
0DTE evidence at all. weekly_shadow records five-day holds; a Monday NVDA
spread is a different instrument with different gamma and nothing here says
whether it works. This is how that stops being a guess.

WHICH NAMES, checked against the chain on 2026-09-12 rather than assumed:

    QQQ                                   daily
    AMZN AVGO GOOGL META MSFT NVDA MU     Mon, Wed, Fri
    SNDK CRWV                             Friday only

STRUCTURE COMES FROM IV/RV, the one column section 50 measured on this book: a
credit spread's break-even win rate IS its risk ratio and delta IS the
market's probability estimate, so the only place an edge can come from is
implied exceeding realised.

    ratio >= SELL_ABOVE (1.05)   options rich   -> SELL premium, credit vertical
    ratio <= BUY_BELOW  (0.95)   options cheap  -> BUY premium, debit vertical
    between                      no edge        -> no row, and that is a result

BOTH SIDES ARE RECORDED EVERY SESSION. One row for CALL and one for PUT, so
the data answers which direction worked rather than only the one a signal
happened to pick. Direction selection is a separate question from structure
selection and mixing them makes neither answerable.

THE QUOTE WIDTH IS STORED, because on these names it is not a rounding error.
Median near-ATM width as a share of mid, Monday's chain:

    NVDA 2.6%   MU 2.8%   QQQ 3.9%   META 6.3%
    AMZN 11.9%  GOOGL 13.3%  MSFT 17.0%  AVGO 21.2%

A vertical pays that twice, on two legs, in and out. Against a maximum return
of 30-50%, AVGO's quote eats the trade before direction matters. A result that
cannot be separated from the cost of obtaining it is not a result.

SETTLEMENT IS ARITHMETIC, NOT A QUOTE. A 0DTE spread's value at expiry is
decided by the close, and the closing quote on an expiring option is the
widest of the day. Marking it from spot avoids inventing a loss or a gain out
of a stale bid.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2  # noqa: E402

from trading_engine.data_feed import (  # noqa: E402
    chain_vertical, fetch_option_chain, fetch_spot, strike_for_delta,
)
from trading_engine import weekly_signals  # noqa: E402

logger = logging.getLogger("dte0_shadow")
NY = ZoneInfo("America/New_York")

# Mon/Wed/Fri names plus QQQ. SNDK and CRWV are absent because their chains
# carry Friday expiries only -- verified, not assumed.
SYMBOLS = [s.strip().upper() for s in os.getenv(
    "TRADING_DTE0_SYMBOLS", "QQQ,NVDA,MU,META,AMZN,GOOGL,MSFT,AVGO").split(",") if s.strip()]

# IV/RV bands. Between them there is no measured reason to prefer either
# structure, so nothing is recorded -- an abstention is data.
SELL_ABOVE = float(os.getenv("TRADING_DTE0_SELL_ABOVE", "1.05"))
BUY_BELOW = float(os.getenv("TRADING_DTE0_BUY_BELOW", "0.95"))
SHORT_DELTA = float(os.getenv("TRADING_DTE0_SHORT_DELTA", "0.30"))
TARGET_PCT = float(os.getenv("TRADING_DTE0_TARGET_PCT", "30.0"))
# Refuse a chain whose near-ATM quote is wider than this share of mid. AVGO at
# 21% is the case this exists for: the structure cannot pay for its own exit.
MAX_QUOTE_PCT = float(os.getenv("TRADING_DTE0_MAX_QUOTE_PCT", "15.0"))
OPEN_AT = os.getenv("TRADING_DTE0_OPEN_AT", "09:45")


def _dsn() -> str:
    return (os.getenv("DATABASE_URL", "")
            .replace("postgresql+psycopg2://", "postgresql://")
            .replace("postgresql+asyncpg://", "postgresql://"))


def _conn():
    c = psycopg2.connect(_dsn())
    c.autocommit = True
    return c


def _expiry_today(symbol: str, day: date) -> "str | None":
    """Today's expiry for this symbol, or None if it does not have one.

    Asked of the chain rather than inferred from the weekday: which names
    carry Monday and Wednesday expiries is a fact about the listing, and it
    changes without telling anyone.
    """
    try:
        chain = fetch_option_chain(day.isoformat(), symbol)
    except Exception:
        return None
    return day.isoformat() if chain else None


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def _quote_width_pct(chain: dict, spot: float) -> "float | None":
    """Median near-ATM bid-ask as a share of mid. The cost of participating.

    `chain` is data_feed's dict keyed by (option_type, strike) holding
    OptionQuote, not a list of Tradier rows.
    """
    pcts = []
    for (kind, strike), q in chain.items():
        if kind != "call" or abs(strike - spot) > spot * 0.03:
            continue
        if q.bid <= 0 or q.ask <= 0:
            continue
        mid = (q.bid + q.ask) / 2
        if mid > 0:
            pcts.append((q.ask - q.bid) / mid * 100.0)
    return _median(pcts)


def _atm_iv(chain: dict, spot: float) -> "float | None":
    ivs = [q.iv for (_, strike), q in chain.items()
           if abs(strike - spot) <= spot * 0.02 and q.iv]
    return _median(ivs)


def _width_for(spot: float) -> float:
    """Spread width scaled to the name. A $5 width on QQQ and on MU are not
    the same trade; a share of spot keeps the structures comparable."""
    if spot >= 600:
        return 10.0
    if spot >= 300:
        return 5.0
    if spot >= 100:
        return 2.5
    return 1.0


def open_session(now: "datetime | None" = None) -> int:
    """One CALL row and one PUT row per eligible symbol. No orders."""
    now = now or datetime.now(NY)
    day = now.date()
    written = 0
    with _conn() as conn, conn.cursor() as cur:
        for sym in SYMBOLS:
            exp = _expiry_today(sym, day)
            if not exp:
                logger.info("%s has no expiry today — skipped.", sym)
                continue
            try:
                spot = float(fetch_spot(sym) or 0)
                chain = fetch_option_chain(exp, sym)
            except Exception:
                logger.warning("%s: chain unavailable — skipped.", sym, exc_info=True)
                continue
            if not spot or not chain:
                continue

            qw = _quote_width_pct(chain, spot)
            if qw is not None and qw > MAX_QUOTE_PCT:
                logger.info("%s quote is %.1f%% of mid, above the %.1f%% ceiling "
                            "— skipped. The structure cannot pay for its own exit.",
                            sym, qw, MAX_QUOTE_PCT)
                continue

            iv = _atm_iv(chain, spot)
            sig = weekly_signals.read(sym) or {}
            rv = sig.get("rv20")
            if not iv or not rv:
                logger.info("%s: IV or RV unavailable — skipped.", sym)
                continue
            ratio = iv / rv if rv else None
            if ratio is None:
                continue
            if ratio >= SELL_ABOVE:
                structure = "CREDIT"
            elif ratio <= BUY_BELOW:
                structure = "DEBIT"
            else:
                logger.info("%s IV/RV %.2f is inside [%.2f, %.2f] — no edge, "
                            "nothing recorded.", sym, ratio, BUY_BELOW, SELL_ABOVE)
                continue

            width = _width_for(spot)
            for variant in ("CALL", "PUT"):
                kind = "call" if variant == "CALL" else "put"
                short = strike_for_delta(chain, kind, SHORT_DELTA)
                if short is None:
                    continue
                if structure == "CREDIT":
                    long_ = short + width if kind == "call" else short - width
                else:
                    # A debit vertical is the same two strikes taken the other
                    # way round: long the nearer, short the further out.
                    long_, short = short, (short + width if kind == "call"
                                           else short - width)
                # chain_vertical(chain, kind, BUY leg, SELL leg). For a debit
                # that is long then short; for a credit it is the cost to
                # close, which buys back the short and sells the long. min/max
                # looks equivalent and silently inverts the put credit.
                q = (chain_vertical(chain, kind, long_, short) if structure == "DEBIT"
                     else chain_vertical(chain, kind, short, long_))
                if q is None:
                    continue
                cur.execute("""
                    INSERT INTO dte0_shadow
                        (symbol, trading_day, expiration, variant, structure,
                         long_strike, short_strike, width, spot_at_entry,
                         atm_iv, rv20, iv_rv_ratio, entry_mid, entry_natural,
                         quote_width_pct, short_delta)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, trading_day, variant) DO NOTHING
                """, (sym, day, exp, variant, structure, long_, short, width,
                      spot, iv, rv, ratio, q["mid"], q["ask"], qw, SHORT_DELTA))
                written += cur.rowcount
                logger.info("%s %s %s %.0f/%.0f w%.1f IV/RV %.2f mid %.2f quote %.1f%%",
                            sym, structure, variant, long_, short, width, ratio,
                            q["mid"], qw or 0)
    logger.info("%d row(s) opened", written)
    return written


def _intrinsic(variant: str, structure: str, long_s: float, short_s: float,
               spot: float) -> float:
    """Value of the vertical at expiry, from spot. Arithmetic, not a quote."""
    lo, hi = min(long_s, short_s), max(long_s, short_s)
    width = hi - lo
    if variant == "CALL":
        iv = min(max(spot - lo, 0.0), width)
    else:
        iv = min(max(hi - spot, 0.0), width)
    # For a DEBIT the long leg is the nearer strike, so intrinsic IS the value.
    # For a CREDIT the position is short that vertical, so what it is worth to
    # the seller is the width minus what it costs to buy back.
    return iv if structure == "DEBIT" else width - iv


def settle(now: "datetime | None" = None) -> int:
    now = now or datetime.now(NY)
    done = 0
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, symbol, variant, structure, long_strike, short_strike,
                   entry_mid, width
            FROM dte0_shadow
            WHERE expiry_return_pct IS NULL AND trading_day <= %s
        """, (now.date(),))
        rows = cur.fetchall()
        for rid, sym, variant, structure, lo, sh, entry, width in rows:
            try:
                spot = float(fetch_spot(sym) or 0)
            except Exception:
                continue
            if not spot:
                continue
            val = _intrinsic(variant, structure, float(lo), float(sh), spot)
            if structure == "DEBIT":
                ret = ((val - float(entry)) / float(entry) * 100.0) if entry else None
            else:
                # Sold for `entry`; what is kept is the credit minus the cost
                # to close, as a share of the credit collected.
                ret = (((float(entry) - (float(width) - val)) / float(entry) * 100.0)
                       if entry else None)
            cur.execute("UPDATE dte0_shadow SET expiry_value=%s, expiry_return_pct=%s "
                        "WHERE id=%s", (val, ret, rid))
            done += 1
    logger.info("%d row(s) settled", done)
    return done


def report() -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT count(*), count(expiry_return_pct),
                   min(trading_day), max(trading_day)
            FROM dte0_shadow
        """)
        n, settled, first, last = cur.fetchone()
        print(f"{n} rows, {settled} settled, {first} to {last}\n")
        if not settled:
            print("NOTHING SETTLED YET. One session produces at most two rows per "
                  "name, so this is answerable after roughly twenty sessions --\n"
                  "about seven weeks at three 0DTE days a week.")
            return
        for group, label in (("symbol", "by name"), ("structure", "by structure"),
                             ("variant", "by side")):
            cur.execute(f"""
                SELECT {group}, count(*),
                       round(100.0*count(*) FILTER (WHERE expiry_return_pct>0)/count(*),0),
                       round(avg(expiry_return_pct)::numeric,1),
                       round(avg(quote_width_pct)::numeric,1),
                       round(avg(iv_rv_ratio)::numeric,2)
                FROM dte0_shadow WHERE expiry_return_pct IS NOT NULL
                GROUP BY 1 ORDER BY 4 DESC
            """)
            print(f"--- {label} ---")
            print(f"{'':10s} {'n':>4s} {'win%':>6s} {'avg ret':>9s} "
                  f"{'quote%':>8s} {'IV/RV':>7s}")
            for r in cur.fetchall():
                print(f"{str(r[0]):10s} {r[1]:4d} {r[2]:5.0f}% {r[3]:+8.1f}% "
                      f"{r[4]:7.1f}% {r[5]:7.2f}")
            print()
        print("SHADOW ONLY. Nothing here has ever placed an order.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="ignore the clock and the trading-day check")
    args = ap.parse_args()

    if args.report:
        report()
        return

    now = datetime.now(NY)
    if not args.force:
        from trading_engine import market_calendar

        if not market_calendar.is_trading_day(now.date()):
            print(f"{now:%Y-%m-%d %H:%M %Z} — not a trading day.")
            return

    if args.settle:
        settle(now)
        return
    if args.open:
        hh, mm = (int(x) for x in OPEN_AT.split(":"))
        if not args.force and now.time() < dtime(hh, mm):
            print(f"{now:%H:%M} is before {OPEN_AT} — nothing opened.")
            return
        open_session(now)
        return
    report()


if __name__ == "__main__":
    main()
