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

# Which structures to mark. All three by default, because the measurement that
# motivated this cannot separate them:
#
#   CALL   the strategy as proposed. Measured against 5 years of 5-day
#          outcomes, the 0.12-delta call finished in the money 20.9% of the
#          time while the market charged 11.7% for it -- delta is priced
#          driftless and QQQ drifts up, so a call seller is short the index's
#          own direction. Crude EV -11.00 a contract.
#   PUT    the same structure facing the same drift from the other side:
#          12.3% realised against 11.4% priced, crude EV +8.14.
#   CONDOR both at once. Its EV is not the sum, which is why it is marked
#          rather than reasoned about: on the 0DTE side a condor beat either
#          leg alone in every cell, because the package exit lets one side's
#          decay pay for the other side's move.
VARIANTS = tuple(
    v.strip().upper()
    for v in os.getenv("TRADING_WEEKLY_VARIANTS", "CALL,PUT,CONDOR").split(",")
    if v.strip()
)
WIDTH = float(os.getenv("TRADING_WEEKLY_WIDTH", "5.0"))
TARGET_PCT = float(os.getenv("TRADING_WEEKLY_TARGET_PCT", "25.0"))
MIN_CREDIT = float(os.getenv("TRADING_WEEKLY_MIN_CREDIT", "0.10"))


def _next_weekly_expiry(today: date) -> str:
    """The Friday after this one. QQQ lists daily expiries; this deliberately
    takes the weekly rather than Monday's, so the position carries the weekend
    AND a full week of decay."""
    ahead = 7 - today.weekday() + 4 if today.weekday() > 4 else 4 - today.weekday()
    return (today + timedelta(days=ahead or 7)).isoformat()


def _open_rows(db):
    return (
        db.query(WeeklyShadow)
        .filter(WeeklyShadow.expiry_return_pct.is_(None))
        .order_by(WeeklyShadow.opened_at.desc())
        .all()
    )


def observe(db, now: "datetime | None" = None) -> None:
    """One cycle's worth of work: open a shadow on Friday, mark it otherwise.

    Wrapped by the caller; this must never be able to interrupt a trading
    cycle, because it exists only to take notes.
    """
    if not ENABLED:
        return
    now = now or datetime.now(NY)
    rows = _open_rows(db)
    if not rows:
        _maybe_open(db, now)
        return
    for row in rows:
        _mark(db, row, now)


def _legs(chain, variant):
    """Strikes for one variant, or None when the chain cannot supply them.

    Returns (call_short, call_long, put_short, put_long) with None where a
    side is not used.
    """
    cs = cl = ps = pl = None
    if variant in ("CALL", "CONDOR"):
        cs = strike_for_delta(chain, "call", SHORT_DELTA)
        if cs is None:
            return None
        cl = cs + WIDTH
    if variant in ("PUT", "CONDOR"):
        ps = strike_for_delta(chain, "put", SHORT_DELTA)
        if ps is None:
            return None
        pl = ps - WIDTH
    return cs, cl, ps, pl


def _price(chain, cs, cl, ps, pl):
    """Cost to BUY the structure back: mid, natural, and the spread to cross.

    chain_vertical prices the vertical you would buy to close a credit spread,
    so these are already the right way round and must not be negated -- the
    mid is the buy-back cost and the bid is what selling collects.
    """
    mid = nat_buy = nat_sell = cross = 0.0
    sides = {"call": None, "put": None}
    for short, long_, kind in ((cs, cl, "call"), (ps, pl, "put")):
        if short is None:
            continue
        q = chain_vertical(chain, kind, short, long_)
        if q is None:
            return None
        mid += q["mid"]
        nat_buy += q["ask"]
        nat_sell += q["bid"]
        cross += q["ask"] - q["bid"]
        sides[kind] = round(q["mid"], 4)
    return {"mid": mid, "natural_buy": nat_buy, "natural_sell": nat_sell, "cross": cross,
            "call_side": sides["call"], "put_side": sides["put"]}


def _maybe_open(db, now: datetime) -> None:
    if now.weekday() != ENTRY_WEEKDAY or now.time() < ENTRY_TIME:
        return

    expiry = _next_weekly_expiry(now.date())
    chain = fetch_option_chain(expiry)
    if not chain:
        logger.info("Weekly shadow: no chain for %s yet.", expiry)
        return
    spot = fetch_qqq_spot()

    for variant in VARIANTS:
        legs = _legs(chain, variant)
        if legs is None:
            logger.info("Weekly shadow %s: no strike near %.2f delta on %s.",
                        variant, SHORT_DELTA, expiry)
            continue
        cs, cl, ps, pl = legs
        priced = _price(chain, cs, cl, ps, pl)
        if priced is None:
            logger.info("Weekly shadow %s: %s chain missing a leg.", variant, expiry)
            continue
        if priced["mid"] < MIN_CREDIT:
            logger.info("Weekly shadow %s: collects only %.3f, below the %.2f floor.",
                        variant, priced["mid"], MIN_CREDIT)
            continue

        short_leg = chain.get(("call", float(cs))) if cs else chain.get(("put", float(ps)))
        db.add(WeeklyShadow(
            expiration=expiry, strategy=f"WEEKLY_{variant}",
            short_strike=cs, long_strike=cl,
            put_short_strike=ps, put_long_strike=pl,
            width=WIDTH, spot_at_entry=round(spot, 2),
            short_delta=round(short_leg.delta, 4) if short_leg and short_leg.delta is not None else None,
            short_iv=round(short_leg.iv, 4) if short_leg and short_leg.iv is not None else None,
            entry_credit_mid=round(priced["mid"], 4),
            entry_credit_natural=round(max(priced["natural_sell"], 0.0), 4),
            entry_spread_width=round(priced["cross"], 4),
            call_side_at_entry=priced["call_side"],
            put_side_at_entry=priced["put_side"],
            peak_return_pct=0.0, worst_return_pct=0.0,
        ))
        logger.info(
            "Weekly shadow OPENED %s %s — calls %s/%s puts %s/%s, credit %.3f mid / %.3f natural, "
            "%.3f to cross, spot %.2f.",
            variant, expiry, cs, cl, ps, pl,
            priced["mid"], max(priced["natural_sell"], 0.0), priced["cross"], spot,
        )
    db.commit()


def _mark(db, row: WeeklyShadow, now: datetime) -> None:
    """Reprice one open shadow and record both outcomes as they arrive."""
    if now.date().isoformat() > row.expiration:
        _settle(db, row, now)
        return

    chain = fetch_option_chain(row.expiration)
    if not chain:
        return
    priced = _price(chain, row.short_strike, row.long_strike,
                    row.put_short_strike, row.put_long_strike)
    if priced is None:
        return

    cost = max(priced["mid"], 0.0)
    credit = row.entry_credit_mid
    ret = ((credit - cost) / credit * 100.0) if credit else 0.0
    spot = fetch_qqq_spot()

    row.last_marked_at = now
    row.last_value_mid = round(cost, 4)
    row.last_call_side = priced["call_side"]
    row.last_put_side = priced["put_side"]
    row.last_return_pct = round(ret, 2)
    row.peak_return_pct = round(max(row.peak_return_pct or 0.0, ret), 2)
    row.worst_return_pct = round(min(row.worst_return_pct or 0.0, ret), 2)

    breached = ((row.short_strike is not None and spot >= row.short_strike)
                or (row.put_short_strike is not None and spot <= row.put_short_strike))
    if breached and not row.breached:
        row.breached = now.isoformat()
        logger.warning("Weekly shadow %s BREACHED: spot %.2f.", row.strategy, spot)

    if ret >= TARGET_PCT and row.target_hit_at is None:
        row.target_hit_at = now
        row.target_return_pct = round(ret, 2)
        logger.info(
            "Weekly shadow %s hit the %.0f%% target at %+.2f%% — the Monday rule would book "
            "here. Still marking to expiry to see whether holding beat it.",
            row.strategy, TARGET_PCT, ret,
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
        "Weekly shadow SETTLED %s %s: %+.2f%% held to expiry, target %s, "
        "peak %+.2f%%, worst %+.2f%%, breached %s.",
        row.strategy, row.expiration, row.expiry_return_pct,
        f"hit at {row.target_return_pct:+.2f}%" if row.target_hit_at else "never hit",
        row.peak_return_pct or 0.0, row.worst_return_pct or 0.0, bool(row.breached),
    )
