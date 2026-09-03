"""Positions the engine did not open, managed by the engine's own rules.

Why this exists
---------------
2026-09-02: a manual QQQ 700/708 spread was opened at 09:43 and ran unmanaged
all session while RECONCILE logged an ERROR once a minute and did nothing
else. Later the same day a manual 20-lot 709 credit spread peaked at +152.50
and closed at -657.50 with no rule watching it. Neither loss counted toward
the daily loss limit or the consecutive-loss breaker, because both read
TradeHistory and TradeHistory only records trades the ENGINE closed. The
account's real risk was invisible to every circuit breaker it had.

RECONSTRUCTED FROM ORDERS, NOT FROM POSITIONS
---------------------------------------------
The position list says what is held. Only the orders say how it got there,
and two things this module needs live only there:

  PAIRING. Nine long calls against nine short calls across five strikes admit
  several readings. Pairing by strike is a guess -- and the engine's own
  naked-leg check made exactly that guess on 2026-09-02 and called a fully
  covered book naked. A multileg order states which legs were put on together.

  ENTRY PRICE. Tradier averages cost_basis across contracts bought and sold
  under one position id: a 700 leg read 3540.00, then 4251.92, at an unchanged
  quantity of 5. Fill prices do not drift. Every rule below is expressed as a
  RETURN, and a return is meaningless without a true entry price. The previous
  version of this module measured value as a percent of spread WIDTH precisely
  because it could not trust the basis -- fine for a report, useless as a
  trigger.

WHAT IT DOES
------------
Marks each reconstructed structure, applies the same ladder the engine applies
to its own positions, and -- only when MANAGE_ORPHANS is set -- acts on it.
When a structure disappears from the account it books a TradeHistory row, so
manual results reach the daily loss limit and the consecutive-loss breaker
like any other trade.

THE STOP IS STRUCTURE-AWARE, and it has to be. A -25% stop is right for a
debit spread and absurd for a credit one, where the return is measured against
the credit collected and -100% is an ordinary bad day. Applying one number to
both would close every credit spread within minutes. Debit and credit get
their own, matched to what the playbook uses for the engine's own windows.

INTENT IS THE PART THIS CANNOT SEE. On 2026-09-02 a -25% stop would have
closed the credit book hours before the operator chose to ride it to 15:45.
MANAGE_ORPHANS is therefore opt-in and off by default, and MANAGE_UNDERLYING
narrows it further to the engine's own symbol.
"""
import json
import logging
import os
from datetime import datetime, timezone

from . import tradier_orders

logger = logging.getLogger(__name__)

# Watch orphans at all. Off leaves the old behaviour: a RECONCILE line, and
# nothing else.
WATCH_ORPHANS = os.getenv("TRADING_WATCH_ORPHANS", "false").lower() == "true"

# ACT on them. Separate from watching on purpose -- see the intent paragraph
# above. Watching costs nothing and can only inform; acting closes positions a
# human opened for reasons the engine cannot read.
MANAGE_ORPHANS = os.getenv("TRADING_MANAGE_ORPHANS", "false").lower() == "true"
# ... and only in these underlyings, comma separated. Empty means every symbol.
#
# A LIST rather than one symbol: the account holds spreads on several names at
# once, and "manage the engine's own underlying" was too narrow the first
# morning it ran. Naming them explicitly rather than defaulting to everything,
# because the failure modes are asymmetric -- forgetting to add a symbol leaves
# a position watched but unmanaged and visible in the log, while managing a
# name by accident closes something the operator never offered up.
MANAGE_UNDERLYING = {
    s.strip().upper()
    for s in os.getenv("TRADING_MANAGE_UNDERLYING", "QQQ").split(",")
    if s.strip()
}

# The ladder. Same shape as the engine's own, same variables where they are
# genuinely the same question.
ORPHAN_TAKE_PROFIT_PCT = float(os.getenv("TRADING_ORPHAN_TAKE_PROFIT", "50"))
ORPHAN_STOP_PCT = float(os.getenv("TRADING_ORPHAN_STOP_PCT", "-25"))
# Credit gets its own, for the reason in the docstring: -25% of a collected
# credit is a few cents and would close everything.
ORPHAN_CREDIT_STOP_PCT = float(os.getenv("TRADING_ORPHAN_CREDIT_STOP_PCT", "-600"))
# REFUSE TO ACT ON A QUOTE THAT IS NOT A QUOTE.
#
# Caught before this ever ran, on the night it was deployed. After the close on
# 2026-09-02 a QQQ 700/710 held for the next session quoted:
#
#     700 call   bid 7.50   ask 12.46   last 9.86    <- 50% of its own value
#     710 call   bid 2.25   ask  2.33
#
# The natural -- bid minus ask, which is what an exit would actually pay --
# came to 5.17 against a 7.49 entry, or -31%, and would have tripped the -25%
# stop on the first cycle of the morning. At sane mids the same spread is worth
# 7.69, or +2.7%. The position was flat and the engine would have dumped it.
#
# A wide book is not a price. Every rule here is a return, a return is only as
# good as the mark, and the mark is only as good as the quote. When either leg
# is quoted wider than this fraction of its own mid, the structure is REPORTED
# and not acted on -- the reading is still logged, so the artefact is visible
# rather than silently skipped.
#
# 0.25 is loose enough for an ordinary 0DTE book late in the day and tight
# enough to reject the case above at 0.50.
ORPHAN_MAX_LEG_SPREAD = float(os.getenv("TRADING_ORPHAN_MAX_LEG_SPREAD", "0.25"))

# THE STALL, WITH ITS OWN GIVEBACK.
#
# TRADING_STALL_GIVEBACK_PCT is shared by the morning ride, the afternoon
# credit trade and this module, and five points means something different in
# each. On the engine's morning debit spread it is a fraction of a premium
# priced near the money. On a credit spread it is five points of the credit
# COLLECTED, which can be a couple of cents. On a manual deep-ITM spread --
# a QQQ 700/710 bought at 7.49, $10 wide -- five points is 37 cents of value,
# which a position drifting with the underlying gives back without the thesis
# changing at all.
#
# One number cannot serve three structures. These default to the shared values
# so nothing moves until they are set deliberately.
STALL_MINUTES = float(os.getenv("TRADING_ORPHAN_STALL_MINUTES",
                                os.getenv("TRADING_STALL_MINUTES", "0")))
STALL_GIVEBACK_PCT = float(os.getenv("TRADING_ORPHAN_STALL_GIVEBACK_PCT",
                                     os.getenv("TRADING_STALL_GIVEBACK_PCT", "0")))

# Peak tracking must survive a container recreate or the stall resets on every
# deploy and can never fire. A file under the mounted working directory.
# CEILING: book when there is no meaningful upside left to hold for.
#
# The engine has this for its own ride (RIDE_CEILING_FRACTION) and orphans did
# not. It matters more here than there, because of a shape the stall cannot
# handle: as a debit spread approaches its full width it stops MOVING, and a
# position that is not moving cannot give back enough to trip a stall. The
# protection weakens exactly as the remaining upside disappears.
#
# A QQQ 700/710 bought at 7.49 can be worth at most 10.00, so 90% of maximum
# return is a value of about 9.75. Holding past that risks the whole width for
# the last 25 cents.
#
# For a credit structure the maximum is the credit itself -- the spread decays
# to zero and you keep all of it -- so the ceiling is 90% of the credit.
ORPHAN_CEILING_FRACTION = float(os.getenv("TRADING_ORPHAN_CEILING", "0.90"))

# FORCE CLOSE. The engine flattens its own book at 15:45; orphans had no time
# exit at all and would have ridden into expiry.
#
# These are physically settled. A spread left to expire is not a cash
# settlement, it is an exercise and an assignment in shares -- and if the
# underlying finishes between the strikes it is the pin case. On 2026-09-02
# the operator deliberately flattened before the 16:00 settlement print for
# exactly that reason, and leaving orphans to expire contradicts it.
ORPHAN_FORCE_CLOSE = os.getenv("TRADING_ORPHAN_FORCE_CLOSE", "15:45").strip()

STATE_PATH = os.getenv("TRADING_ORPHAN_STATE", "orphan_peaks.json")


def _load() -> dict:
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        with open(STATE_PATH, "w") as fh:
            json.dump(state, fh)
    except Exception:
        logger.exception("Could not persist orphan state — the stall will restart cold.")


def _parse(symbol: str) -> "tuple | None":
    """(root, expiry, right, strike) from an OCC symbol, or None."""
    try:
        root = tradier_orders.occ_root(symbol)
        tail = symbol[len(root):]
        if len(tail) != 15:
            return None
        return root, tail[:6], tail[6], int(tail[7:]) / 1000.0
    except Exception:
        return None


def open_structures(engine_symbols: "set | None" = None) -> list:
    """Reconstruct what is open, from the orders that opened it.

    Opening fills add, closing fills subtract, and whatever still has quantity
    left is open. Matched on the leg PAIR rather than on single symbols, so a
    strike used by two different spreads does not merge them.

    Cross-checked against the position list: a structure the account no longer
    holds is dropped even if the closing order was never seen, because an
    assignment or an expiry leaves no closing fill at all.
    """
    engine_symbols = engine_symbols or set()
    orders = tradier_orders.filled_spread_orders()
    if not orders:
        return []
    held = {}
    try:
        for p in tradier_orders.open_positions():
            held[p.get("symbol")] = int(float(p.get("quantity") or 0))
    except Exception:
        logger.exception("Position cross-check unavailable — reporting from orders alone.")

    book = {}
    for o in orders:
        syms = tuple(sorted(l["symbol"] for l in o["legs"]))
        if any(s in engine_symbols for s in syms):
            continue
        rec = book.get(syms)
        if rec is None:
            rec = book[syms] = {"symbols": syms, "qty": 0, "net": o["net"],
                                "credit": o["credit"], "opened": o["created"]}
        if o["opening"]:
            # Weighted so a scaled-in structure carries a true average.
            total = rec["qty"] + o["qty"]
            if total > 0:
                rec["net"] = round((rec["net"] * rec["qty"] + o["net"] * o["qty"]) / total, 4)
            rec["qty"] = total
            rec["credit"] = o["credit"]
        elif o["closing"]:
            rec["qty"] -= o["qty"]

    out = []
    for syms, rec in book.items():
        if rec["qty"] <= 0:
            continue
        # Still actually held? An expiry or assignment leaves no closing fill.
        if held and not all(abs(held.get(s, 0)) > 0 for s in syms):
            continue
        legs = [(_parse(s), s) for s in syms]
        if any(p is None for p, _ in legs):
            continue
        root = legs[0][0][0]
        right = legs[0][0][2]
        expiry = legs[0][0][1]
        # For a debit the LONG is the leg we paid for; identify it by strike
        # and structure rather than by re-reading the order sides.
        strikes = sorted((p[3], s) for p, s in legs)
        low, high = strikes[0], strikes[1]
        if rec["credit"]:
            short_strike, short_sym = low
            long_strike, long_sym = high
        else:
            long_strike, long_sym = low
            short_strike, short_sym = high
        out.append({
            "root": root, "expiry": expiry, "right": right,
            "long": long_sym, "short": short_sym,
            "long_strike": long_strike, "short_strike": short_strike,
            "qty": rec["qty"], "entry": rec["net"], "credit": rec["credit"],
            "opened": rec["opened"], "key": "|".join(syms),
        })
    return out


def _max_return_pct(st: dict) -> "float | None":
    """The most this structure can ever return, as a percent of what was risked.

    A debit spread cannot exceed its width; a credit spread cannot exceed the
    credit collected. Both are hard ceilings set by the structure, not by the
    market, which is what makes a fraction of them a meaningful place to stop.
    """
    entry = abs(st.get("entry") or 0.0)
    if entry <= 0:
        return None
    if st["credit"]:
        return 100.0
    width = abs(st["short_strike"] - st["long_strike"])
    if width <= 0:
        return None
    return (width - entry) / entry * 100.0


def _past_force_close() -> bool:
    """Is it past the orphan flatten time, in New York?"""
    if not ORPHAN_FORCE_CLOSE:
        return False
    try:
        from zoneinfo import ZoneInfo
        hh, mm = (int(x) for x in ORPHAN_FORCE_CLOSE.split(":"))
        now = datetime.now(ZoneInfo("America/New_York"))
        return (now.hour, now.minute) >= (hh, mm)
    except Exception:
        return False


def _quotes_tradeable(q: dict, st: dict) -> bool:
    """Is either leg quoted too wide to price an exit from?

    See ORPHAN_MAX_LEG_SPREAD. Returns False on a missing or nonsensical
    quote too -- absence of a price is not a reason to act on one.
    """
    for sym in (st["long"], st["short"]):
        row = q.get(sym) or {}
        try:
            bid, ask = float(row.get("bid") or 0.0), float(row.get("ask") or 0.0)
        except (TypeError, ValueError):
            return False
        if bid <= 0 or ask <= 0 or ask < bid:
            return False
        mid = (bid + ask) / 2.0
        if mid <= 0 or (ask - bid) / mid > ORPHAN_MAX_LEG_SPREAD:
            logger.warning(
                "ORPHAN quote unusable: %s bid %.2f ask %.2f is %.0f%% of mid — "
                "reporting but not acting.",
                sym, bid, ask, (ask - bid) / mid * 100.0,
            )
            return False
    return True


def _mark(st: dict) -> "tuple | None":
    """(value, return_pct) at prices we could actually transact at.

    Return is against the REAL entry price from the fills. For a debit that is
    (value - paid) / paid; for a credit, (collected - cost to close) /
    collected -- the same convention the engine uses for its own windows, so
    the thresholds mean the same thing in both places.
    """
    try:
        q = tradier_orders.quotes([st["long"], st["short"]])
        lq, sq = q.get(st["long"]), q.get(st["short"])
        if not lq or not sq:
            return None
        if st["credit"]:
            # Cost to buy the structure back: pay the ask on the short, receive
            # the bid on the long wing.
            cost = float(sq.get("ask") or 0.0) - float(lq.get("bid") or 0.0)
            collected = abs(st["entry"])
            if collected <= 0:
                return None
            return cost, (collected - cost) / collected * 100.0
        value = float(lq.get("bid") or 0.0) - float(sq.get("ask") or 0.0)
        paid = st["entry"]
        if paid <= 0:
            return None
        return value, (value - paid) / paid * 100.0
    except Exception:
        return None


def _close(st: dict, reason: str, limit_price: float) -> bool:
    """Send the closing order. Only reached when MANAGE_ORPHANS is set."""
    try:
        res = tradier_orders.submit_vertical(
            st["root"],
            f"20{st['expiry'][:2]}-{st['expiry'][2:4]}-{st['expiry'][4:6]}",
            "call" if st["right"].upper() == "C" else "put",
            long_strike=st["long_strike"], short_strike=st["short_strike"],
            quantity=st["qty"], opening=False,
            limit_price=abs(limit_price), is_credit=st["credit"],
        )
        logger.error("ORPHAN %s: closing %s %.0f/%.0f x%d — %s",
                     reason, st["root"], st["long_strike"], st["short_strike"],
                     st["qty"], res)
        return True
    except Exception:
        logger.exception("ORPHAN close failed for %s — position left open.", st["key"])
        return False


def _book(st: dict, value: float, ret_pct: float, reason: str) -> None:
    """Write a TradeHistory row so manual results reach the circuit breakers.

    Without this, a manual loss is invisible to the daily loss limit and the
    consecutive-loss breaker -- both read TradeHistory, which only ever
    recorded trades the engine itself closed. On 2026-09-02 a -657.50 manual
    loss counted toward neither, and the engine's risk budget was untouched by
    it.
    """
    try:
        from config.db_pgrs import SessionLocal
        from models_pgdb.trading_models import TradeHistory
        entry = abs(st["entry"])
        pnl = ((entry - value) if st["credit"] else (value - entry)) * st["qty"] * 100
        db = SessionLocal()
        try:
            db.add(TradeHistory(
                strategy=("CALL_CREDIT_SPREAD" if st["credit"] else "BULL_CALL_SPREAD"),
                underlying=st["root"], quantity=st["qty"],
                long_strike=st["long_strike"], short_strike=st["short_strike"],
                entry_net_debit=entry, exit_net_value=value,
                realized_pnl_dollars=round(pnl, 2), realized_pnl_pct=round(ret_pct, 2),
                close_reason=reason, playbook="MANUAL",
            ))
            db.commit()
            logger.info("ORPHAN booked to history: %s %.0f/%.0f x%d %+.2f (%s)",
                        st["root"], st["long_strike"], st["short_strike"],
                        st["qty"], pnl, reason)
        finally:
            db.close()
    except Exception:
        logger.exception("Could not book orphan result — it will not reach the loss limit.")


def review(engine_symbols: "set | None" = None) -> list:
    """Mark every structure the engine did not open, and say what the ladder says.

    Returns the structures it reported. Never raises: this must not be able to
    break a trading cycle.
    """
    if not WATCH_ORPHANS:
        return []
    try:
        structures = open_structures(engine_symbols)
        state = _load()
        now = datetime.now(timezone.utc)
        seen, reported = set(), []

        for st in structures:
            key = st["key"]
            seen.add(key)
            mark = _mark(st)
            if mark is None:
                continue
            value, ret_pct = mark

            rec = state.get(key) or {"peak": ret_pct, "peak_at": now.isoformat()}
            if ret_pct > rec["peak"]:
                rec = {"peak": ret_pct, "peak_at": now.isoformat()}
            state[key] = rec
            quiet = (now - datetime.fromisoformat(rec["peak_at"])).total_seconds() / 60.0
            stop_pct = ORPHAN_CREDIT_STOP_PCT if st["credit"] else ORPHAN_STOP_PCT

            max_ret = _max_return_pct(st)
            ceiling = (ORPHAN_CEILING_FRACTION * max_ret
                       if (max_ret and ORPHAN_CEILING_FRACTION > 0) else None)

            reason = None
            if ret_pct <= stop_pct:
                reason = "STOP_LOSS"
            elif _past_force_close():
                # Time beats everything. These settle in shares, not cash.
                reason = "FORCE_CLOSE"
            elif ceiling is not None and ret_pct >= ceiling:
                reason = "CEILING"
            elif (STALL_MINUTES > 0 and rec["peak"] > 0 and quiet >= STALL_MINUTES
                  and ret_pct <= rec["peak"] - STALL_GIVEBACK_PCT):
                # The take-profit ARMS this rather than firing it, exactly as
                # the engine's own credit window now does: a structure that
                # keeps making new highs is not finished.
                reason = "STALL"
            manageable = (MANAGE_ORPHANS
                          and (not MANAGE_UNDERLYING or st["root"] in MANAGE_UNDERLYING)
                          and _quotes_tradeable(
                              tradier_orders.quotes([st["long"], st["short"]]), st))
            verdict = reason or (
                f"holding (ceiling {ceiling:+.0f}%, stop {stop_pct:+.0f}%, "
                f"stall {STALL_GIVEBACK_PCT:.0f}pts)" if ceiling is not None
                else f"holding (stop {stop_pct:+.0f}%)")
            logger.info(
                "ORPHAN %s %s %.0f/%.0f x%d %s: entry %.2f value %.2f %+.1f%% "
                "(peak %+.1f%%, %.0f min ago) — %s%s",
                st["root"], st["right"], st["long_strike"], st["short_strike"],
                st["qty"], "credit" if st["credit"] else "debit",
                abs(st["entry"]), value, ret_pct, rec["peak"], quiet, verdict,
                "" if manageable else "  [observation only]",
            )
            if reason and manageable and _close(st, reason, value):
                _book(st, value, ret_pct, reason)
                state.pop(key, None)
            reported.append(st)

        # A structure that has vanished since the last pass was closed by
        # someone -- the human, an assignment, an expiry. Book what we can and
        # stop tracking its peak.
        for gone in [k for k in state if k not in seen]:
            state.pop(gone, None)
        _save(state)
        return reported
    except Exception:
        logger.exception("Orphan review failed — continuing; this must not break a cycle.")
        return []
