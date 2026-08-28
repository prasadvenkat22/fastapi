"""Weekly credit structures across many underlyings, observed and never traded.

Started as a QQQ call spread and now marks CALL, PUT and CONDOR on every
symbol in TRADING_WEEKLY_SYMBOLS. Nothing here places an order or consumes
buying power; it writes rows on Friday and marks them until expiry.

WHY ALL THREE STRUCTURES, PER SYMBOL. On QQQ the two sides are not
symmetric and the asymmetry is measured, not assumed: over 5 years of 5-day
outcomes the 0.12-delta CALL finished in the money 20.9% of the time against
11.7% priced, while the PUT finished 12.3% against 11.4% priced. Delta is
priced driftless and QQQ drifts up, so a call seller is short the index's own
direction and a condor pairs a negative-EV wing with a positive-EV one.

Whether that holds on a single name is an open question, and a different one
per name -- a high-IV single stock has no reason to share an index's drift.
Recording the wings separately costs nothing and is the only way to find out;
collapsing them into a condor number would hide exactly the effect worth
knowing about.

WHAT THIS CANNOT TELL YOU YET. Whether the premium is worth selling at all is
a question about implied against realised, and scripts/rv_vs_iv.py answers it
from the snapshots capture_chain.py collects. Those started on 2026-08-27, so
the first period-matched single-name pairs land in early September. Until
then this is accumulating structure records with no verdict attached, which
is the intended order: the 0DTE credit window was live for weeks before its
own numbers said it loses at every setting.

Original QQQ note follows.

A weekly call credit spread, observed and never traded.

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
from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from models_pgdb.trading_models import WeeklyShadow

from .data_feed import chain_vertical, fetch_option_chain, fetch_spot, strike_for_delta
from . import weekly_signals

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

# Width can be expressed in STRIKES instead, which is the unit that travels
# across symbols. Left at 0 the dollar figure above is used, so QQQ keeps the
# exact width its existing record was built on.
WIDTH_STRIKES = int(os.getenv("TRADING_WEEKLY_WIDTH_STRIKES", "0"))

# Which underlyings to mark. The same list capture_chain.py snapshots, so the
# implied-vol history and the structure record cover the same names.
#
# Nothing here is traded. These are candidates for a single-name book and the
# only honest evidence about them is forward evidence: there is no historical
# option-chain feed to backtest a weekly condor against, which is the same
# reason this module exists for QQQ.
SYMBOLS = tuple(
    v.strip().upper()
    for v in os.getenv(
        "TRADING_WEEKLY_SYMBOLS",
        "QQQ,SNDK,MU,CRWV,STX,WDC,DELL,META,MSFT,GOOGL,AMZN,MRVL"
    ).split(",")
    if v.strip()
)
TARGET_PCT = float(os.getenv("TRADING_WEEKLY_TARGET_PCT", "25.0"))
MIN_CREDIT = float(os.getenv("TRADING_WEEKLY_MIN_CREDIT", "0.10"))


def _strike_spacing(chain) -> float:
    """The gap this symbol actually lists strikes on.

    Read from the chain rather than configured, because it varies by symbol
    and by price: QQQ and CRWV quote $1, while MU, STX, WDC, DELL and META
    quote $5. The most common gap, not the smallest -- a chain often carries
    a few half-strikes near the money that would otherwise be mistaken for
    the grid.
    """
    strikes = sorted({float(key[1]) for key in chain})
    gaps = [round(b - a, 4) for a, b in zip(strikes, strikes[1:]) if b > a]
    if not gaps:
        return 1.0
    return Counter(gaps).most_common(1)[0][0]


def _width_for(chain) -> float:
    """Spread width in dollars, respecting the symbol's own strike grid.

    WIDTH is a dollar figure tuned on QQQ's $1 grid. On a $5-strike name a
    narrower spread does not exist, so taking the larger of the two leaves
    QQQ exactly as it was and gives every other name the tightest spread it
    actually lists.
    """
    spacing = _strike_spacing(chain)
    if WIDTH_STRIKES:
        return round(WIDTH_STRIKES * spacing, 2)
    return round(max(WIDTH, spacing), 2)


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

    # One chain fetch per (symbol, expiration), not per row. Three variants
    # on eleven symbols is 33 open rows, and fetching per row would be 33
    # requests every cycle for 11 chains' worth of information.
    groups: dict = {}
    for row in _open_rows(db):
        groups.setdefault((row.symbol or "QQQ", row.expiration), []).append(row)

    for (symbol, expiration), group in groups.items():
        chain = fetch_option_chain(expiration, symbol=symbol)
        spot = fetch_spot(symbol) if chain else None
        # Passed even when empty: a row past its expiration still has to
        # settle, and settling reads nothing from the chain.
        for row in group:
            _mark(db, row, now, chain, spot)

    # A symbol with nothing open is a candidate to open, independently of
    # what the others are carrying.
    #
    # The old check was "are there any open rows AT ALL", which with a single
    # underlying meant the same thing. With eleven it would let QQQ's open
    # position block every other name from ever starting one -- and it would
    # have done so silently, since a shadow that never opens looks exactly
    # like a shadow whose entry conditions were not met.
    open_symbols = {sym for sym, _ in groups}
    for symbol in SYMBOLS:
        if symbol not in open_symbols:
            _maybe_open(db, now, symbol)


def _legs(chain, variant, width: float):
    """Strikes for one variant, or None when the chain cannot supply them.

    Returns (call_short, call_long, put_short, put_long) with None where a
    side is not used.
    """
    cs = cl = ps = pl = None
    if variant in ("CALL", "CONDOR"):
        cs = strike_for_delta(chain, "call", SHORT_DELTA)
        if cs is None:
            return None
        cl = cs + width
    if variant in ("PUT", "CONDOR"):
        ps = strike_for_delta(chain, "put", SHORT_DELTA)
        if ps is None:
            return None
        pl = ps - width
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


def _maybe_open(db, now: datetime, symbol: str) -> None:
    if now.weekday() != ENTRY_WEEKDAY or now.time() < ENTRY_TIME:
        return

    expiry = _next_weekly_expiry(now.date())
    chain = fetch_option_chain(expiry, symbol=symbol)
    if not chain:
        logger.info("Weekly shadow %s: no chain for %s yet.", symbol, expiry)
        return
    spot = fetch_spot(symbol)
    if spot is None:
        logger.info("Weekly shadow %s: no spot available — skipped.", symbol)
        return
    width = _width_for(chain)

    for variant in VARIANTS:
        legs = _legs(chain, variant, width)
        if legs is None:
            logger.info("Weekly shadow %s %s: no strike near %.2f delta on %s.",
                        symbol, variant, SHORT_DELTA, expiry)
            continue
        cs, cl, ps, pl = legs
        priced = _price(chain, cs, cl, ps, pl)
        if priced is None:
            logger.info("Weekly shadow %s %s: %s chain missing a leg.",
                        symbol, variant, expiry)
            continue
        if priced["mid"] < MIN_CREDIT:
            logger.info("Weekly shadow %s %s: collects only %.3f, below the %.2f floor.",
                        symbol, variant, priced["mid"], MIN_CREDIT)
            continue

        short_leg = chain.get(("call", float(cs))) if cs else chain.get(("put", float(ps)))
        short_iv = (round(short_leg.iv, 4)
                    if short_leg and short_leg.iv is not None else None)
        # Notes beside the row, never a gate. weekly_signals returns {} when
        # the daily bars are unavailable, so a data outage writes nulls rather
        # than losing the observation -- the credit and the strikes are the
        # part that cannot be reconstructed later.
        signals = weekly_signals.entry_signals(symbol, short_iv, now.date())
        db.add(WeeklyShadow(
            symbol=symbol, expiration=expiry, strategy=f"WEEKLY_{variant}",
            short_strike=cs, long_strike=cl,
            put_short_strike=ps, put_long_strike=pl,
            width=width, spot_at_entry=round(spot, 2),
            short_delta=round(short_leg.delta, 4) if short_leg and short_leg.delta is not None else None,
            short_iv=short_iv, **signals,
            entry_credit_mid=round(priced["mid"], 4),
            entry_credit_natural=round(max(priced["natural_sell"], 0.0), 4),
            entry_spread_width=round(priced["cross"], 4),
            call_side_at_entry=priced["call_side"],
            put_side_at_entry=priced["put_side"],
            peak_return_pct=0.0, worst_return_pct=0.0,
        ))
        logger.info(
            "Weekly shadow OPENED %s %s %s ($%.2f wide) — calls %s/%s puts %s/%s, "
            "credit %.3f mid / %.3f natural, %.3f to cross, spot %.2f.",
            symbol, variant, expiry, width, cs, cl, ps, pl,
            priced["mid"], max(priced["natural_sell"], 0.0), priced["cross"], spot,
        )
    db.commit()


def _mark(db, row: WeeklyShadow, now: datetime, chain, spot) -> None:
    """Reprice one open shadow and record both outcomes as they arrive.

    The chain and spot are passed in rather than fetched here so that all
    variants on one symbol share a single request -- see observe().
    """
    if now.date().isoformat() > row.expiration:
        _settle(db, row, now)
        return

    if not chain or spot is None:
        return
    priced = _price(chain, row.short_strike, row.long_strike,
                    row.put_short_strike, row.put_long_strike)
    if priced is None:
        return

    cost = max(priced["mid"], 0.0)
    credit = row.entry_credit_mid
    ret = ((credit - cost) / credit * 100.0) if credit else 0.0

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
        logger.warning("Weekly shadow %s %s BREACHED: spot %.2f.",
                       row.symbol, row.strategy, spot)

    if ret >= TARGET_PCT and row.target_hit_at is None:
        row.target_hit_at = now
        row.target_return_pct = round(ret, 2)
        logger.info(
            "Weekly shadow %s %s hit the %.0f%% target at %+.2f%% — the Monday rule would "
            "book here. Still marking to expiry to see whether holding beat it.",
            row.symbol, row.strategy, TARGET_PCT, ret,
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
        "Weekly shadow SETTLED %s %s %s: %+.2f%% held to expiry, target %s, "
        "peak %+.2f%%, worst %+.2f%%, breached %s.",
        row.symbol, row.strategy, row.expiration, row.expiry_return_pct,
        f"hit at {row.target_return_pct:+.2f}%" if row.target_hit_at else "never hit",
        row.peak_return_pct or 0.0, row.worst_return_pct or 0.0, bool(row.breached),
    )
