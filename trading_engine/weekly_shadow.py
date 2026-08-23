"""A weekly call credit spread, observed and never traded.

Sell a call spread late on Friday, hold it over the weekend for the decay,
and look at it again on Monday morning. The idea is sound and the engine has
no way to backtest it: weekly option prices are not available historically,
so the only honest evidence is forward evidence, exactly as with the shadow
condor.

Two measurements decided the shape of what gets marked here.

STRIKES ARE CHOSEN BY DELTA, NOT BY DOLLARS. "Two dollars out of the money"
sounds far and is not: measured on a 5-day expiry with QQQ at 713.41, the $2
OTM call carried a 0.478 delta -- a coin flip -- while the 0.12-delta strike
sat nineteen dollars out. Selling the near strike is a 1:1 payoff on a 48%
chance, which is not what a credit spread is for.

AND THE WEEKEND IS WHERE THE RISK LIVES. Over 150 weekends, QQQ gapped up two
dollars or more on 32.7% of them, four dollars or more on 19.3%, and seven or
more on 8.0% -- and those were measured when QQQ averaged $517, so at current
prices they are about 38% larger. A near strike is gapped through while the
position cannot be managed, hedged or stopped for sixty-five hours. A
0.12-delta strike needs a gap most weekends never produce.

Nothing here places an order or consumes buying power. It writes one row on
Friday and marks it every cycle until expiry.
"""

import logging
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from models_pgdb.trading_models import WeeklyShadow

from .data_feed import chain_vertical, fetch_option_chain, fetch_qqq_spot, strike_for_delta

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

ENABLED = os.getenv("TRADING_WEEKLY_SHADOW", "true").lower() == "true"

# Entry: Friday, late enough that the weekend is most of what is left to
# decay, early enough that quotes have not widened into the bell.
_entry_raw = os.getenv("TRADING_WEEKLY_ENTRY_TIME", "15:45")
try:
    _h, _m = (int(p) for p in _entry_raw.split(":"))
    ENTRY_TIME = time(_h, _m)
except ValueError:
    ENTRY_TIME = time(15, 45)
ENTRY_WEEKDAY = int(os.getenv("TRADING_WEEKLY_ENTRY_WEEKDAY", "4"))   # 4 = Friday

SHORT_DELTA = float(os.getenv("TRADING_WEEKLY_SHORT_DELTA", "0.12"))
WIDTH = float(os.getenv("TRADING_WEEKLY_WIDTH", "5.0"))
TARGET_PCT = float(os.getenv("TRADING_WEEKLY_TARGET_PCT", "25.0"))
MIN_CREDIT = float(os.getenv("TRADING_WEEKLY_MIN_CREDIT", "0.10"))


def _next_weekly_expiry(today: date) -> str:
    """The Friday after this one. QQQ lists daily expiries; this deliberately
    takes the weekly rather than Monday's, so the position carries the weekend
    AND a full week of decay."""
    ahead = 7 - today.weekday() + 4 if today.weekday() > 4 else 4 - today.weekday()
    return (today + timedelta(days=ahead or 7)).isoformat()


def _open_row(db):
    return (
        db.query(WeeklyShadow)
        .filter(WeeklyShadow.expiry_return_pct.is_(None))
        .order_by(WeeklyShadow.opened_at.desc())
        .first()
    )


def observe(db, now: "datetime | None" = None) -> None:
    """One cycle's worth of work: open a shadow on Friday, mark it otherwise.

    Wrapped by the caller; this must never be able to interrupt a trading
    cycle, because it exists only to take notes.
    """
    if not ENABLED:
        return
    now = now or datetime.now(NY)
    row = _open_row(db)
    if row is None:
        _maybe_open(db, now)
    else:
        _mark(db, row, now)


def _maybe_open(db, now: datetime) -> None:
    if now.weekday() != ENTRY_WEEKDAY or now.time() < ENTRY_TIME:
        return

    expiry = _next_weekly_expiry(now.date())
    chain = fetch_option_chain(expiry)
    if not chain:
        logger.info("Weekly shadow: no chain for %s yet.", expiry)
        return

    short = strike_for_delta(chain, "call", SHORT_DELTA)
    if short is None:
        logger.info("Weekly shadow: no strike near %.2f delta on %s.", SHORT_DELTA, expiry)
        return
    long_ = short + WIDTH
    quote = chain_vertical(chain, "call", short, long_)
    if quote is None:
        logger.info("Weekly shadow: %s chain missing %.0f/%.0f.", expiry, short, long_)
        return

    # chain_vertical(buy=short, sell=long_) prices the vertical you would buy
    # to CLOSE this spread, so its numbers are already the right way round and
    # must not be negated: its mid is the buy-back cost, and its BID is what
    # selling it collects, since opening the credit means selling that same
    # vertical and crossing to the bid.
    sell_mid = quote["mid"]
    sell_natural = quote["bid"]
    if sell_mid < MIN_CREDIT:
        logger.info(
            "Weekly shadow: %.0f/%.0f collects only %.3f, below the %.2f floor — not marked.",
            short, long_, sell_mid, MIN_CREDIT,
        )
        return

    leg = chain.get(("call", float(short)))
    spot = fetch_qqq_spot()
    db.add(WeeklyShadow(
        expiration=expiry, strategy="CALL_CREDIT_SPREAD",
        short_strike=short, long_strike=long_, width=WIDTH,
        spot_at_entry=round(spot, 2),
        short_delta=round(leg.delta, 4) if leg and leg.delta is not None else None,
        short_iv=round(leg.iv, 4) if leg and leg.iv is not None else None,
        entry_credit_mid=round(sell_mid, 4),
        entry_credit_natural=round(max(sell_natural, 0.0), 4),
        entry_spread_width=round(quote["ask"] - quote["bid"], 4),
        peak_return_pct=0.0, worst_return_pct=0.0,
    ))
    db.commit()
    logger.info(
        "Weekly shadow OPENED %s %.0f/%.0f — credit %.3f mid / %.3f natural, "
        "short delta %s, spot %.2f, %.2f to cross.",
        expiry, short, long_, sell_mid, max(sell_natural, 0.0),
        leg.delta if leg else None, spot, quote["ask"] - quote["bid"],
    )


def _mark(db, row: WeeklyShadow, now: datetime) -> None:
    """Reprice the open shadow and record both outcomes as they arrive."""
    if now.date().isoformat() > row.expiration:
        _settle(db, row, now)
        return

    chain = fetch_option_chain(row.expiration)
    if not chain:
        return
    quote = chain_vertical(chain, "call", row.short_strike, row.long_strike)
    if quote is None:
        return

    cost = max(quote["mid"], 0.0)          # what buying it back costs
    credit = row.entry_credit_mid
    ret = ((credit - cost) / credit * 100.0) if credit else 0.0
    spot = fetch_qqq_spot()

    row.last_marked_at = now
    row.last_value_mid = round(cost, 4)
    row.last_return_pct = round(ret, 2)
    row.peak_return_pct = round(max(row.peak_return_pct or 0.0, ret), 2)
    row.worst_return_pct = round(min(row.worst_return_pct or 0.0, ret), 2)
    if spot >= row.short_strike and not row.breached:
        row.breached = now.isoformat()
        logger.warning(
            "Weekly shadow BREACHED: spot %.2f is at or through the %.0f short strike.",
            spot, row.short_strike,
        )
    if ret >= TARGET_PCT and row.target_hit_at is None:
        row.target_hit_at = now
        row.target_return_pct = round(ret, 2)
        logger.info(
            "Weekly shadow hit the %.0f%% target at %+.2f%% — the Monday rule would book here. "
            "Still marking to expiry to see whether holding beat it.",
            TARGET_PCT, ret,
        )
    db.commit()

    if now.date().isoformat() == row.expiration and now.time() >= time(15, 45):
        _settle(db, row, now)


def _settle(db, row: WeeklyShadow, now: datetime) -> None:
    """Close the record out at expiry, whatever it is worth."""
    credit = row.entry_credit_mid
    cost = row.last_value_mid if row.last_value_mid is not None else 0.0
    row.expiry_value = round(cost, 4)
    row.expiry_return_pct = round(((credit - cost) / credit * 100.0) if credit else 0.0, 2)
    db.commit()
    logger.info(
        "Weekly shadow SETTLED %s %.0f/%.0f: %+.2f%% held to expiry, target %s, "
        "peak %+.2f%%, worst %+.2f%%, breached %s.",
        row.expiration, row.short_strike, row.long_strike, row.expiry_return_pct,
        f"hit at {row.target_return_pct:+.2f}%" if row.target_hit_at else "never hit",
        row.peak_return_pct or 0.0, row.worst_return_pct or 0.0, bool(row.breached),
    )
