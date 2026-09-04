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
import time
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
# ONLY WHAT EXPIRES TODAY.
#
# The ladder is built for 0DTE: a -25% stop, a 90% ceiling and a 15:45 flatten
# all assume the position has hours to live, not days. Applied to a spread
# expiring later they are simply wrong -- the force close would flatten a
# Friday position on Wednesday for no reason, and a stall would book a
# multi-day thesis on one quiet afternoon.
#
# So the scope is the EXPIRY, not the symbol. A name is managed on the day its
# contracts expire and left alone before that, which is what makes one rule
# correct for every position rather than a list that has to be maintained.
ORPHAN_TODAY_ONLY = os.getenv("TRADING_ORPHAN_TODAY_ONLY", "true").lower() == "true"

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

# ... OR THIS MANY CENTS WIDE, WHICHEVER IS KINDER.
#
# A percentage alone rejects every cheap option. A 0DTE short quoted 0.02/0.03
# is a ONE CENT market -- as tight as an option can be -- and 40% of its own
# mid. The percentage test called that unusable and blocked the close.
#
# The absolute width is what an exit actually pays. Five cents on a
# nearly-worthless leg is a real market; five cents on a 40-dollar leg is
# also fine, and there the percentage never binds anyway. So a quote passes
# if EITHER test passes: tight in cents, or tight in proportion.
ORPHAN_MAX_LEG_SPREAD_ABS = float(os.getenv("TRADING_ORPHAN_MAX_LEG_SPREAD_ABS", "0.05"))

# ACT ON INFERRED PAIRS, or only report them.
#
# Order-based pairing recovers the structure the human actually put on. It
# cannot recover one they LEGGED INTO across separate orders: on 2026-09-03 a
# SNDK 1500 bought on 09-02 and a 1530 sold at 14:14 today are economically a
# spread and appear to the pairer as two unrelated legs.
#
# The fallback pairs leftovers by strike, which is a GUESS -- the same guess
# that made the engine's naked-leg check read a covered book as naked. It is
# usually right and it is not stated fact, so an inferred pair is REPORTED by
# default and acted on only when this is set.
#
# The distinction matters more than it sounds. An inferred entry is the sum of
# two independent fills, which can exceed the structure's own width -- SNDK
# 1500/1530 nets 35.78 against a 30-dollar width, so its BEST possible return
# is -16% and a stop would fire the moment it were managed. Booking that
# automatically, on a pairing nobody stated, is not a decision to take by
# default.
MANAGE_INFERRED = os.getenv("TRADING_ORPHAN_MANAGE_INFERRED", "false").lower() == "true"

# NEVER STOP OUT A POSITION THAT IS ALREADY PROFITABLE AT EXPIRY.
#
# The stop fires on the MARK, and a deep in-the-money spread marks far below
# what it is worth. Observed live 2026-09-03, an hour before the close:
#
#   AVGO 365/355 put, entry 7.88, spot 354.37 -- BELOW the short strike, so
#   the spread holds its full 10.00 of intrinsic. It marked 6.45, a return of
#   -18%, because the short 355 put still carried 3.27 of time premium.
#
# Tomorrow that position becomes 0DTE and the -25% stop goes live at a mark of
# 5.91. One tick and the engine books -190 on a structure worth +212 held to
# expiry. The underlying had moved IN ITS FAVOUR and the rule would have sold
# it for that reason.
#
# So: if INTRINSIC exceeds what was paid, holding to expiry pays more than the
# entry cost and a mark depressed by time value is not a reason to sell. Time
# premium on the short leg goes to zero on its own; that is arithmetic, not a
# forecast. The stop still fires when intrinsic itself has fallen through the
# entry, which is the case it was built for -- a genuine adverse move.
#
# This does NOT disable the stop. It removes the one situation where the stop
# does the opposite of its job.
STOP_RESPECTS_INTRINSIC = os.getenv(
    "TRADING_ORPHAN_STOP_RESPECTS_INTRINSIC", "true").lower() == "true"

# AND THE SAME FOR THE STALL, WHICH IS THE MORE EXPENSIVE HALF.
#
# The stop guard above was built first and stopped there, which was my error:
# the stall has the identical defect and a worse consequence. The stop only
# fires on losers; the stall fires on WINNERS, which is exactly where giving
# up intrinsic costs the most.
#
# It cost 1,250 dollars within the hour. 2026-09-03 17:20:
#
#   ORPHAN STALL_LATER: closing SNDK 1500/1540 x1 -> booked +345
#
# Entry 24.05, sold near 27.50, SNDK at 1558 -- above BOTH strikes, so the
# spread held its full 40.00 of intrinsic and pays +1,595 at expiry. The stall
# read a mark fading from time premium on the short 1540 and called it a
# profit that had stopped climbing.
#
# WITH THIS ON, THE STALL STILL WORKS -- it is measured on INTRINSIC rather
# than on the mark. A genuine reversal (SNDK actually falling back through
# 1540) shrinks intrinsic and books the trade. Time premium bleeding out of a
# short leg does not, because that is not the profit going away, it is the
# profit arriving.
STALL_RESPECTS_INTRINSIC = os.getenv(
    "TRADING_ORPHAN_STALL_RESPECTS_INTRINSIC", "true").lower() == "true"

# A RULE THAT LOCKS IN PROFIT MUST NOT REALISE A LOSS DOING IT.
#
# The stall now DECIDES on intrinsic and still EXECUTES at the mark, and on a
# deep in-the-money spread the mark sits below the entry. So a small intrinsic
# decline -- a real one, correctly detected -- triggers a sale at a price that
# books a loss on a position that was profitable at expiry.
#
# It cost about 263 dollars on 2026-09-03 across three closes:
#
#   AVGO 365/355 -113   intrinsic 9.40 vs 7.88 entry  (+193 at expiry)
#   NVDA 220/230  -15   intrinsic 9.54 vs 8.15 entry  (+139 at expiry)
#   MU   930/950 -135   intrinsic 20.00 vs 12.90 entry (+710 at expiry)
#
# Each fired legitimately by its own logic. Each sold a winner at a loss.
#
# So the stall additionally requires that the exit ACTUALLY BOOKS A GAIN. If
# the mark is below entry there is no profit to protect, and the case is the
# STOP's -- which is already intrinsic-aware and fires when intrinsic itself
# falls through the entry. The two rules then divide cleanly: the stall
# protects gains, the stop limits losses, and neither does the other's job
# badly.
STALL_MUST_BOOK_A_GAIN = os.getenv(
    "TRADING_ORPHAN_STALL_MUST_BOOK_GAIN", "true").lower() == "true"

# THE STALL FOR POSITIONS THAT EXPIRE LATER.
#
# The 0DTE stall is deliberately unarmed -- any positive peak starts it --
# because a same-day position has no tomorrow to recover into. A multi-day
# position does, so an unarmed stall would book it at 09:35 on a +2% wiggle
# and forfeit the rest of the week. That is why the stall was 0DTE-only.
#
# But refusing to watch at all gives up the other half: a spread that runs to
# a good profit intraday and rolls over gets carried back down to nothing,
# which is the case this was built for -- a NVDA 215/225 expected to reach 228
# today and not beyond.
#
# So: ARM on a real profit, then protect it with a SMALL giveback. Arming is
# what makes a tight giveback safe here; without it the two settings fight.
# The quiet period is longer than the 0DTE one for the same reason -- a
# multi-day position is allowed to pause without that meaning it is finished.
ORPHAN_LATER_STALL_ARM_PCT = float(os.getenv("TRADING_ORPHAN_LATER_STALL_ARM", "10"))
ORPHAN_LATER_STALL_MINUTES = float(os.getenv("TRADING_ORPHAN_LATER_STALL_MINUTES", "15"))
ORPHAN_LATER_STALL_GIVEBACK_PCT = float(
    os.getenv("TRADING_ORPHAN_LATER_STALL_GIVEBACK", "3.3"))

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

# NO LOSS-TAKING BEFORE THIS TIME. Empty disables the gate, which is the
# default and leaves behaviour unchanged.
#
# THE OPEN IS NOT A SIGNAL, IT IS A SPREAD. For the first stretch of the
# session the book is at its widest and the mark at its least trustworthy, so
# a structure can print a "peak" on one wide quote and a giveback on the next,
# or a -25% mark on a spread that has not moved, with nothing having happened
# to the underlying. On a Friday expiry that dips early and comes back, both
# rules fire on microstructure and book a position that had hours to work.
#
# GATED -- the loss side, and the early-profit side:
#
#   the stop     a -25% mark at 09:31 is as likely to be a wide quote as a
#                real move.
#   the stall    books a WINNER early on a judgment about a mark, which is the
#                judgment the open is worst at making.
#
# LIVE FROM THE FIRST CYCLE:
#
#   the ceiling  fires at 90% of maximum. Taking that at 09:35 is a good
#                outcome, not a premature one, and it is a decision about a
#                position that has already won.
#   force close  is about assignment, not value. 15:45 regardless.
#
# THIS TRADES TAIL PROTECTION FOR NOISE IMMUNITY, KNOWINGLY. These are DEBIT
# spreads, so loss is bounded by the premium paid rather than by the stop: the
# gate accepts up to the full debit on a genuine gap in exchange for not
# booking losses into an opening spread that reverses. On 0DTE a 2% adverse
# move frequently does not come back, and nothing here pretends otherwise.
ORPHAN_HOLD_UNTIL = os.getenv("TRADING_ORPHAN_HOLD_UNTIL", "").strip()

# THE BAND WHERE NOTHING FIRES, IN DOLLARS OF INTRINSIC GIVEN BACK.
#
# Two guards can be correct individually and silent together:
#
#   the stall  stands down while the mark is under the entry
#              (STALL_MUST_BOOK_A_GAIN -- it will not sell at a loss, which is
#              the rule that saved 845 dollars on MU the previous day)
#   the stop   stands down while intrinsic still exceeds the entry
#              (STOP_RESPECTS_INTRINSIC -- it will not sell something that
#              pays at expiry)
#
# An in-the-money spread lives in BOTH conditions at once: its mark sits below
# entry because the short leg still holds time premium, while its intrinsic
# sits above entry because it is deep in the money. Between those two lines a
# position can shed its entire edge with nothing acting.
#
# MEASURED LIVE, 2026-09-04. MU 995/1005 x5 held maximum intrinsic at 13:00
# and drained for five straight minutes:
#
#     13:00  MU 1005.10  intrinsic 10.00   worth +1,640 at expiry
#     13:05  MU 1002.39  intrinsic  7.39   worth   +385 at expiry
#
# The engine logged "holding" on every one of those cycles, correctly by its
# own rules. 1,255 dollars of EXPIRY value went, and the only reason it was
# seen at all is that a watcher was printing the band.
#
# WHAT THIS RULE IS. A stop measured on the value at EXPIRY rather than on the
# mark. It fires when intrinsic falls this many dollars from its own peak on a
# structure whose peak was genuinely above entry -- a winner that is turning.
# It deliberately does NOT require booking a gain: the whole point is that the
# mark is underwater and holding is getting worse, so it accepts a small loss
# now against a larger one later.
#
# OFF BY DEFAULT (0). It is a real exit rule, not a correctness fix, and it
# has not been through the harness. Turning it on is a trading decision.
ORPHAN_INTRINSIC_GIVEBACK = float(
    os.getenv("TRADING_ORPHAN_INTRINSIC_GIVEBACK", "0") or 0)

STATE_PATH = os.getenv("TRADING_ORPHAN_STATE", "orphan_peaks.json")


def _load() -> dict:
    """State file: {"peaks": {...}, "structures": {...}}.

    A legacy flat file is read as peaks-only, so an upgrade does not lose the
    ratchet on a position that is already open.
    """
    try:
        with open(STATE_PATH) as fh:
            raw = json.load(fh)
    except Exception:
        return {"peaks": {}, "structures": {}}
    if not isinstance(raw, dict):
        return {"peaks": {}, "structures": {}}
    if "peaks" in raw or "structures" in raw:
        return {"peaks": raw.get("peaks") or {}, "structures": raw.get("structures") or {}}
    return {"peaks": raw, "structures": {}}


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

    # OVERNIGHT HOLDS. Tradier's /orders returns the CURRENT SESSION only, so
    # a position opened yesterday has no opening fill to rebuild from today and
    # would silently manage nothing -- found on 2026-09-03 with three spreads
    # held overnight and "structures: 0". The reconstruction is therefore
    # cached the day it is made and reused for as long as the account still
    # holds both legs. Today's orders always win, so a scale-in or a partial
    # close corrects the cached copy rather than being ignored.
    cached = _load().get("structures") or {}
    for key, rec in cached.items():
        syms = tuple(key.split("|"))
        # A key already in `book` blocks the cache -- but only if it is LIVE.
        # An opened-and-closed pair leaves a qty 0 entry, which blocked the
        # cached copy of a REOPENED structure at the same strikes and then
        # dropped out itself, leaving the legs to fall through to inferred
        # pairing and be skipped for want of a price. Observed 2026-09-03 on a
        # SNDK 1500/1560 rolled twice in one session.
        if len(syms) != 2 or (syms in book and book[syms]["qty"] > 0):
            continue
        if any(s in engine_symbols for s in syms):
            continue
        book[syms] = {"symbols": syms, "qty": rec.get("qty", 0),
                      "net": rec.get("net", 0.0), "credit": rec.get("credit", False),
                      "opened": rec.get("opened")}

    # LEGS THE ORDERS COULD NOT PAIR. A position legged into across separate
    # orders looks like two unrelated singles; pair the leftovers by strike and
    # mark the result inferred. See MANAGE_INFERRED for why that flag matters.
    # QUANTITY-AWARE, not symbol-aware. A first version treated a symbol as
    # consumed if it appeared in ANY stated pair, so a SNDK 1500 held twice
    # with one contract inside a stated 1500/1540 left the OTHER contract
    # invisible -- and the 1530 short it belonged with had nothing to pair
    # against. The leftover is what the account holds MINUS what the stated
    # structures actually use.
    # COUNT ONLY PAIRS THAT SURVIVE THE HELD CHECK.
    #
    # A structure whose legs are no longer in the account is dropped below,
    # but consumed was tallied from the WHOLE book including those. On
    # 2026-09-03 a NVDA 222.5/230 had been opened and its 230 short later
    # closed; the dead pair still claimed the 222.5 long, so five contracts
    # that were free to pair with the 227.5 shorts were counted as used and
    # never became leftovers. A 2,100-dollar spread sat outside the ladder
    # with no SINGLE warning either, because nothing was left over to warn
    # about.
    def _still_held(syms):
        return not held or all(abs(held.get(x, 0)) > 0 for x in syms)

    consumed = {}
    for syms, rec in book.items():
        if not _still_held(syms):
            continue
        for sym in syms:
            consumed[sym] = consumed.get(sym, 0) + rec["qty"]
    leftover = {}
    for sym, n in held.items():
        free = abs(n) - consumed.get(sym, 0)
        if free <= 0:
            continue
        n = free if n > 0 else -free
        parsed = _parse(sym)
        if parsed is None:
            continue
        root, expiry, right, strike = parsed
        leftover.setdefault((root, expiry, right), []).append((strike, sym, n))
    fills = tradier_orders.filled_legs() if leftover else {}
    for (root, expiry, right), legs_ in leftover.items():
        longs = sorted([l for l in legs_ if l[2] > 0])
        shorts = sorted([l for l in legs_ if l[2] < 0])
        li = si = 0
        while li < len(longs) and si < len(shorts):
            (lk, lsym, lq), (sk, ssym, sq) = longs[li], shorts[si]
            n = min(lq, -sq)
            lp = (fills.get(lsym) or {}).get("price")
            sp = (fills.get(ssym) or {}).get("price")
            if lp is None or sp is None:
                logger.warning(
                    "ORPHAN inferred pair %s %.0f/%.0f has no fill price — skipped, "
                    "since a return without a true entry is not a number worth acting on.",
                    root, lk, sk)
            else:
                book[tuple(sorted((lsym, ssym)))] = {
                    "symbols": (lsym, ssym), "qty": n, "net": round(lp - sp, 4),
                    "credit": (lp - sp) < 0, "opened": None, "inferred": True,
                }
            longs[li] = (lk, lsym, lq - n)
            shorts[si] = (sk, ssym, sq + n)
            if longs[li][2] == 0:
                li += 1
            if shorts[si][2] == 0:
                si += 1

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
        # WHICH LEG IS LONG DEPENDS ON THE RIGHT, NOT JUST THE SIGN.
        #
        # This assumed calls. For a CALL debit the long is the lower strike;
        # for a PUT debit it is the HIGHER one, because a put gains as price
        # falls. Getting it backwards does not merely mislabel the row -- _mark
        # computes bid(long) - ask(short), so the value comes out NEGATIVE.
        #
        # Observed 2026-09-03 on a manual AVGO 365/355 put debit spread bought
        # for 7.88: it marked -8.40, or -206.6%. It was WATCH ONLY, which is
        # the only reason it was not stopped out instantly on a number that
        # cannot occur. Every put spread in the book had the same defect.
        is_put = right.upper() == "P"
        if rec["credit"]:
            # Bear call: short the lower strike. Bull put: short the higher.
            short_strike, short_sym = high if is_put else low
            long_strike, long_sym = low if is_put else high
        else:
            # Call debit: long the lower. Put debit: long the higher.
            long_strike, long_sym = high if is_put else low
            short_strike, short_sym = low if is_put else high
        out.append({
            "root": root, "expiry": expiry, "right": right,
            "long": long_sym, "short": short_sym,
            "long_strike": long_strike, "short_strike": short_strike,
            "qty": rec["qty"], "entry": rec["net"], "credit": rec["credit"],
            "opened": rec["opened"], "key": "|".join(syms),
            "inferred": bool(rec.get("inferred")),
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


def _expires_today(st: dict) -> bool:
    """Does this structure expire in the current New York session?

    The expiry is the YYMMDD from the OCC symbol, compared against the New
    York date rather than UTC -- after 20:00 ET the two disagree, and the
    disagreement would silently change which positions are in scope.
    """
    try:
        from zoneinfo import ZoneInfo
        return st.get("expiry") == datetime.now(ZoneInfo("America/New_York")).strftime("%y%m%d")
    except Exception:
        return False


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


def _past_hold_until() -> bool:
    """Is it past the opening quiet period, in New York? See ORPHAN_HOLD_UNTIL.

    Fails OPEN on a bad value -- an unparseable time leaves the stop and the
    stall working as they do today rather than silently disabling two exit
    rules for a whole session.
    """
    if not ORPHAN_HOLD_UNTIL:
        return True
    try:
        from zoneinfo import ZoneInfo
        hh, mm = (int(x) for x in ORPHAN_HOLD_UNTIL.split(":"))
        now = datetime.now(ZoneInfo("America/New_York"))
        return (now.hour, now.minute) >= (hh, mm)
    except Exception:
        return True


def _quotes_tradeable(q: dict, st: dict, reason: str = "") -> bool:
    """Is the short leg quoted well enough to act on, for THIS reason?

    The guard exists so the manager does not close a position on a garbage
    quote -- a leg quoted 1.00/2.50 is not a market, and acting on its mid
    books an imaginary price. Two things were wrong with how it did that.

    IT JUDGED BOTH LEGS. On any 0DTE position in its last hour the far leg is
    bid 0.00 because it is worthless, which is the correct quote rather than a
    broken one. Observed 2026-09-03 on a QQQ 719/722 credit: the 722 quoted
    0.00/0.01, the guard refused, and the FORCE_CLOSE decided at 15:45 logged
    "[observation only]" every cycle to the bell. So the test is on the SHORT
    leg -- the one that must be bought back, and the one carrying assignment
    risk. A long bid at zero is information, not an unusable quote, and the
    mark already values it at zero, which is right.

    IT JUDGED WIDTH ONLY IN PROPORTION. That same short quoted 0.02/0.03 is a
    ONE CENT market, as tight as an option gets, and 40% of its own mid. A
    percentage threshold rejects every cheap option, and by the close every
    option in a 0DTE book is cheap. So a quote passes if it is tight in cents
    OR tight in proportion, whichever is kinder.

    AND THE STANDARD DEPENDS ON WHAT IS BEING DECIDED. A width test protects
    a judgment about VALUE -- ceiling, stall, stop -- all of which read the
    mark and would act on a false one. FORCE_CLOSE is not a judgment about
    value: it is there because these spreads are physically settled and an
    in-the-money short gets assigned. A poor fill is worse than a good fill
    and far better than assignment, so the force close needs only a real
    market to trade into, not a tight one.
    """
    row = q.get(st["short"]) or {}
    try:
        bid, ask = float(row.get("bid") or 0.0), float(row.get("ask") or 0.0)
    except (TypeError, ValueError):
        return False
    # The ask, not the bid: closing this structure BUYS the short back. A zero
    # bid on a nearly-worthless short is normal and does not stop anything; a
    # zero ask means there is no offer to buy at, which does.
    if ask <= 0 or ask < bid:
        return False
    if reason == "FORCE_CLOSE":
        return True
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return False
    if (ask - bid) > ORPHAN_MAX_LEG_SPREAD_ABS and (ask - bid) / mid > ORPHAN_MAX_LEG_SPREAD:
        logger.warning(
            "ORPHAN quote unusable: %s bid %.2f ask %.2f is %.0f%% of mid — "
            "reporting but not acting.",
            st["short"], bid, ask, (ask - bid) / mid * 100.0,
        )
        return False
    return True


def _decompose(st: dict, value: float) -> "tuple | None":
    """(intrinsic, extrinsic) for a spread, or None without a spot.

    WHY THIS EXISTS. A deep in-the-money spread marks far below what it is
    worth, and the reason is invisible in the return alone. On 2026-09-03 a
    SNDK 1500/1540 with spot at 1557.60 held its FULL 40-dollar width in
    intrinsic value and marked at 27.80, reading +15.6% against a possible
    +66.3%. Nothing was wrong with it: the short 1530 sits nearer the money
    than the long 1500 and so carries more time premium, and that premium is
    subtracted from the spread.

    Extrinsic goes to zero at expiry. So a NEGATIVE extrinsic on an ITM spread
    is not a loss, it is the part that comes back -- and telling that apart
    from a real adverse move is exactly what a bare percentage cannot do.

    Intrinsic is capped at the width: a spread cannot be worth more than the
    distance between its strikes however far past them the underlying runs.
    """
    try:
        spot = tradier_orders.quotes([st["root"]]).get(st["root"], {})
        px = float(spot.get("last") or spot.get("bid") or 0.0)
        if px <= 0:
            return None
        lo, hi = sorted((st["long_strike"], st["short_strike"]))
        width = hi - lo
        if st["right"].upper() == "P":
            intrinsic = max(0.0, min(hi - px, width))
        else:
            intrinsic = max(0.0, min(px - lo, width))
        # NO INVERSION FOR CREDIT. open_structures assigns the SHORT leg to the
        # low strike on a call credit and the high strike on a put credit, so
        # `lo`/`hi` above already resolve to the short strike and the formula
        # already yields the cost to buy the structure back. Subtracting it
        # from the width flipped max profit into max loss.
        #
        # Caught live 2026-09-03 on a QQQ 719/722 call credit, ten lots, QQQ at
        # 718.74 -- below the short, so both legs expire worthless and the cost
        # to close is ZERO. It reported 3.00, the maximum loss, which made the
        # stall read roughly -711% and SUPPRESSED the stop, because intrinsic
        # 3.00 exceeded the 0.37 entry and looked profitable at expiry. Both
        # rules were pointed the wrong way on a 0DTE position.
        return round(intrinsic, 2), round(value - intrinsic, 2)
    except Exception:
        return None


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


_DEAD = {"rejected", "canceled", "cancelled", "expired", "error"}


def _fill_value(order_id) -> "tuple | None":
    """(value per spread, contracts filled) for the close, or None if unknown.

    THE QUANTITY MATTERS AS MUCH AS THE PRICE. A close can fill in parts, and
    _book used to record st["qty"] -- the size of the whole structure --
    whatever actually filled. On 2026-09-03 a QQQ 714/716 credit spread of ten
    contracts closed as two orders of five, and each wrote a row claiming ten:
    170 dollars booked twice against a real 170 total. Both rows were genuine
    closes; only the quantity was wrong.

    submit_vertical answering {'status': 'ok'} means accepted, not filled --
    the distinction that cost a false TradeHistory row on 2026-08-27 and, in
    the other direction, understated the first live orphan close by 175
    dollars on 2026-09-03: CRWV booked at its 4.20 mark having filled at 4.55.

    The mark is the NATURAL, deliberately pessimistic because it is what an
    exit pays in the worst case. The fill is what happened. Booking the mark
    makes every orphan result wrong in the same direction, and that number
    feeds the daily loss limit and the consecutive-loss breaker.
    """
    if not order_id:
        return None
    for _ in range(6):
        try:
            o = tradier_orders.order_status(order_id)
            status = (o.get("status") or "").lower()
            if status == "filled":
                legs = o.get("leg")
                if not isinstance(legs, list):
                    return None
                total, qty = 0.0, None
                for lg in legs:
                    px = float(lg.get("avg_fill_price") or 0.0)
                    total += -px if (lg.get("side") or "").lower().startswith("buy") else px
                    n = int(float(lg.get("exec_quantity") or 0))
                    qty = n if qty is None else min(qty, n)
                if not qty:
                    return None
                return abs(round(total, 4)), qty
            if status in _DEAD:
                logger.error("ORPHAN close %s came back %s — not booking.", order_id, status)
                return None
        except Exception:
            logger.exception("Could not read orphan close %s.", order_id)
            return None
        time.sleep(2)
    logger.warning("ORPHAN close %s still working — booking deferred.", order_id)
    return None


def _close(st: dict, reason: str, limit_price: float) -> "tuple | None":
    """Send the closing order; return (filled value, contracts), or None.

    None means it did not fill -- rejected, or still working. The caller must
    not book a result for it: the position is either still open or never left,
    and the next pass will see it again.
    """
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
        return _fill_value((res or {}).get("id"))
    except Exception:
        logger.exception("ORPHAN close failed for %s — position left open.", st["key"])
        return None


def _book(st: dict, value: float, ret_pct: float, reason: str,
          qty: "int | None" = None) -> None:
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
        # The contracts that actually FILLED, not the structure's size.
        n = qty or st["qty"]
        pnl = ((entry - value) if st["credit"] else (value - entry)) * n * 100
        db = SessionLocal()
        try:
            db.add(TradeHistory(
                strategy=("CALL_CREDIT_SPREAD" if st["credit"] else "BULL_CALL_SPREAD"),
                underlying=st["root"], quantity=n,
                long_strike=st["long_strike"], short_strike=st["short_strike"],
                entry_net_debit=entry, exit_net_value=value,
                realized_pnl_dollars=round(pnl, 2), realized_pnl_pct=round(ret_pct, 2),
                close_reason=reason, playbook="MANUAL",
            ))
            db.commit()
            logger.info("ORPHAN booked to history: %s %.0f/%.0f x%d %+.2f (%s)",
                        st["root"], st["long_strike"], st["short_strike"],
                        n, pnl, reason)
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
        peaks = state["peaks"]
        now = datetime.now(timezone.utc)
        seen, reported = set(), []
        # Cache today's reconstruction so tomorrow can still price a position
        # held overnight. See the note in open_structures.
        # MERGE, DO NOT REPLACE.
        #
        # This used to rebuild the cache from whatever open_structures returned
        # this pass, which silently erased any entry it could not reproduce --
        # including a hand-seeded one, within a minute. On 2026-09-03 a SNDK
        # 1500/1560 could not be priced (the 1500 was bought the previous
        # session, and /orders is session-only, so today's fills net its
        # quantity to zero) and every attempt to seed its entry was wiped by
        # the next cycle.
        #
        # The cache is a RECORD, not a mirror of the current pass. Entries are
        # dropped when a structure closes -- handled at the close site -- not
        # because one pass failed to rebuild it.
        for st in structures:
            state["structures"][st["key"]] = {
                "qty": st["qty"], "net": st["entry"],
                "credit": st["credit"], "opened": st["opened"],
            }

        for st in structures:
            key = st["key"]
            seen.add(key)
            mark = _mark(st)
            if mark is None:
                continue
            value, ret_pct = mark

            # WHICH RETURN THE STALL WATCHES.
            #
            # On an in-the-money spread the mark is the wrong series: it fades
            # as time premium bleeds out of the short leg, which is the profit
            # ARRIVING, not leaving. Track the peak on intrinsic-vs-entry when
            # that is available, so a stall fires on a real reversal and not on
            # decay. Falls back to the mark when intrinsic cannot be computed.
            parts_iv = _decompose(st, value)
            entry_abs = abs(st["entry"])
            stall_pct = ret_pct
            if STALL_RESPECTS_INTRINSIC and parts_iv and entry_abs:
                stall_pct = ((entry_abs - parts_iv[0]) if st["credit"]
                             else (parts_iv[0] - entry_abs)) / entry_abs * 100.0

            # THE PEAK IS STORED IN DOLLARS OF INTRINSIC, NOT AS A RETURN
            # PERCENT, BECAUSE THE PERCENT'S BASELINE MOVES.
            #
            # stall_pct is measured against the ENTRY, and the entry changes
            # whenever contracts are added to a structure that is already
            # open. Observed live 2026-09-04: SNDK 1700/1720 was scaled up and
            # its average entry went 11.60 -> 13.20, so the SAME untouched
            # 20.00 of intrinsic re-read as 72.4% and then 51.5%. Against a
            # stored peak of 72.4 that is a 20.9-point giveback, and the stall
            # would have booked a position that had not moved at all.
            #
            # Intrinsic in dollars has no baseline to shift. It changes when
            # the underlying changes, which is the only thing the stall is
            # trying to detect. The percent is still derived for the log line
            # and for the giveback thresholds, but it is derived FROM the
            # dollar peak against the CURRENT entry, so both sides of the
            # comparison always use the same basis.
            iv_now = parts_iv[0] if parts_iv else None
            rec = peaks.get(key) or {}
            prev_iv = rec.get("peak_iv")
            if prev_iv is None and rec.get("peak") is not None and entry_abs:
                # Migrating a record written before this change: recover the
                # dollar peak from the stored percent using today's entry. It
                # is only as good as the entry that produced it, which is the
                # bug -- so a structure whose entry has since moved gets
                # reseeded from where it is now rather than trusted.
                prev_iv = None
            better = (iv_now is not None
                      and (prev_iv is None
                           or (iv_now < prev_iv if st["credit"] else iv_now > prev_iv)))
            if better:
                rec = {"peak_iv": iv_now, "peak_at": now.isoformat()}
            elif not rec:
                rec = {"peak_iv": iv_now, "peak_at": now.isoformat()}
            rec.setdefault("peak_at", now.isoformat())
            peak_iv = rec.get("peak_iv")
            # Derived, never stored: the percent this peak represents against
            # the entry as it stands right now.
            if peak_iv is not None and entry_abs:
                rec["peak"] = (((entry_abs - peak_iv) if st["credit"]
                                else (peak_iv - entry_abs)) / entry_abs * 100.0)
            else:
                rec["peak"] = rec.get("peak", stall_pct)
            peaks[key] = rec
            quiet = (now - datetime.fromisoformat(rec["peak_at"])).total_seconds() / 60.0
            stop_pct = ORPHAN_CREDIT_STOP_PCT if st["credit"] else ORPHAN_STOP_PCT

            max_ret = _max_return_pct(st)
            ceiling = (ORPHAN_CEILING_FRACTION * max_ret
                       if (max_ret and ORPHAN_CEILING_FRACTION > 0) else None)

            # WHICH RULES ARE ACTUALLY ABOUT THE EXPIRY, AND WHICH ARE NOT.
            #
            # The stop, the force close and the stall all assume the position
            # has hours rather than days: a multi-day drawdown can recover, a
            # Friday spread flattened on Wednesday is closed for no reason, and
            # a multi-day thesis should not die on one quiet five minutes. They
            # are 0DTE rules and stay scoped to the expiry day.
            #
            # THE CEILING IS NOT. A structure worth 90% of its maximum has the
            # same tiny upside left whenever it expires -- and holding the full
            # width of risk for the last few cents is WORSE over two days than
            # over two hours, not better. So the ceiling applies at any expiry.
            zero_dte = (not ORPHAN_TODAY_ONLY) or _expires_today(st)

            # Intrinsic is computed once here: the stop guard reads it, and so
            # does the log line below.
            intrinsic_ok = False
            if parts_iv and STOP_RESPECTS_INTRINSIC:
                # THE COMPARISON REVERSES FOR A CREDIT STRUCTURE.
                #
                # For a debit, intrinsic is the VALUE at expiry: above what was
                # paid means profitable, so a mark-based stop should not fire.
                # For a credit, intrinsic is the COST TO CLOSE: above the credit
                # collected means a LOSS.
                #
                # Written once for debits, this suppressed the stop on exactly
                # the credit spreads that most needed it -- a structure at
                # maximum loss reads intrinsic far above its entry and would
                # have looked profitable at expiry. Found alongside the
                # inversion in _decompose on 2026-09-03; same bug class, two
                # places, and both only reachable on credit structures.
                #
                # Strictly compared: at exactly the entry there is nothing to
                # protect either way.
                intrinsic_ok = (parts_iv[0] < abs(st["entry"]) if st["credit"]
                                else parts_iv[0] > abs(st["entry"]))

            # Would selling right now, at the mark, actually realise a profit?
            # See STALL_MUST_BOOK_A_GAIN.
            books_a_gain = (not STALL_MUST_BOOK_A_GAIN) or (
                (abs(st["entry"]) - value) > 0 if st["credit"]
                else (value - abs(st["entry"])) > 0)

            # Computed once per structure so the log line below can say the
            # rules are waiting rather than just omitting them.
            past_hold = _past_hold_until()

            reason = None
            if zero_dte and ret_pct <= stop_pct and not past_hold:
                # AHEAD OF THE INTRINSIC GUARD, because this holds for a
                # different reason. That guard declines to stop a position
                # that still pays at expiry; this one declines to trust a
                # -25% mark printed into the opening spread at all.
                logger.info(
                    "ORPHAN %s %.0f/%.0f is %+.1f%% on the mark but it is before "
                    "%s — holding through the opening spread rather than booking "
                    "a loss on it.",
                    st["root"], st["long_strike"], st["short_strike"], ret_pct,
                    ORPHAN_HOLD_UNTIL,
                )
            elif zero_dte and ret_pct <= stop_pct and intrinsic_ok:
                logger.info(
                    "ORPHAN %s %.0f/%.0f is %+.1f%% on the mark but holds %.2f of "
                    "intrinsic against a %.2f entry — NOT stopping out a position "
                    "that pays at expiry. The gap is time premium on the short leg.",
                    st["root"], st["long_strike"], st["short_strike"], ret_pct,
                    parts_iv[0], abs(st["entry"]),
                )
            elif zero_dte and ret_pct <= stop_pct:
                reason = "STOP_LOSS"
            elif zero_dte and _past_force_close():
                # Time beats everything. These settle in shares, not cash.
                reason = "FORCE_CLOSE"
            elif ceiling is not None and ret_pct >= ceiling:
                reason = "CEILING"
            elif (zero_dte and past_hold and ORPHAN_INTRINSIC_GIVEBACK > 0
                  and peak_iv is not None and iv_now is not None
                  and (peak_iv > entry_abs if not st["credit"] else peak_iv < entry_abs)
                  and ((peak_iv - iv_now) if not st["credit"]
                       else (iv_now - peak_iv)) >= ORPHAN_INTRINSIC_GIVEBACK):
                # See ORPHAN_INTRINSIC_GIVEBACK. No books_a_gain test here on
                # purpose -- this rule exists precisely for the case where the
                # mark is under water and the expiry value is walking away.
                reason = "GIVEBACK"
            elif (zero_dte and past_hold and STALL_MINUTES > 0 and rec["peak"] > 0
                  and quiet >= STALL_MINUTES and books_a_gain
                  and stall_pct <= rec["peak"] - STALL_GIVEBACK_PCT):
                # The take-profit ARMS this rather than firing it, exactly as
                # the engine's own credit window now does: a structure that
                # keeps making new highs is not finished.
                reason = "STALL"
            elif ((not zero_dte) and past_hold and ORPHAN_LATER_STALL_MINUTES > 0
                  and rec["peak"] >= ORPHAN_LATER_STALL_ARM_PCT
                  and quiet >= ORPHAN_LATER_STALL_MINUTES and books_a_gain
                  and stall_pct <= rec["peak"] - ORPHAN_LATER_STALL_GIVEBACK_PCT):
                # Armed by a real profit, booked on a small giveback. See the
                # knobs above for why arming is what makes the tight giveback
                # safe on a position that has days left.
                reason = "STALL_LATER"
            # The expiry check has moved INTO the reason logic above, so the
            # ceiling can act on a later expiry while the 0DTE rules cannot.
            manageable = (MANAGE_ORPHANS
                          and (not st.get("inferred") or MANAGE_INFERRED)
                          and (not MANAGE_UNDERLYING or st["root"] in MANAGE_UNDERLYING)
                          and _quotes_tradeable(
                              tradier_orders.quotes([st["long"], st["short"]]),
                              st, reason))
            # SAY ONLY WHAT APPLIES TO THIS POSITION.
            #
            # The verdict used to print "stop -25%" on every line including
            # positions expiring later, where zero_dte gates the stop off and
            # it can never fire. The behaviour was right and the log lied about
            # it -- which is worse than a cosmetic problem here, because the
            # whole point of these lines is to let a human check that the rules
            # in force are the rules intended.
            if reason:
                verdict, verdict_scope = reason, ""
            else:
                parts = []
                if ceiling is not None:
                    parts.append("ceiling %+.0f%%" % ceiling)
                if zero_dte:
                    parts.append("stop %+.0f%%%s" % (
                        stop_pct, "" if past_hold else " from %s" % ORPHAN_HOLD_UNTIL))
                    if STALL_MINUTES > 0:
                            parts.append("stall %.1fpts/%.0fmin%s" % (
                            STALL_GIVEBACK_PCT, STALL_MINUTES,
                            "" if past_hold else " from %s" % ORPHAN_HOLD_UNTIL))
                    if ORPHAN_FORCE_CLOSE:
                        parts.append("flatten %s" % ORPHAN_FORCE_CLOSE)
                elif ORPHAN_LATER_STALL_MINUTES > 0:
                    parts.append("stall %.1fpts/%.0fmin %s" % (
                        ORPHAN_LATER_STALL_GIVEBACK_PCT, ORPHAN_LATER_STALL_MINUTES,
                        "ARMED" if rec["peak"] >= ORPHAN_LATER_STALL_ARM_PCT
                        else "arms +%.0f%%" % ORPHAN_LATER_STALL_ARM_PCT))
                verdict = "holding (%s)" % ", ".join(parts) if parts else "holding"
                verdict_scope = "" if zero_dte else "  [expires %s]" % st.get("expiry")
            iv_note = ""
            if parts_iv:
                intr, extr = parts_iv
                iv_note = "  [intrinsic %.2f, extrinsic %+.2f]" % (intr, extr)
            logger.info(
                "ORPHAN %s %s %.0f/%.0f x%d %s: entry %.2f value %.2f %+.1f%% "
                "(peak %+.1f%%, %.0f min ago) — %s%s",
                st["root"], st["right"], st["long_strike"], st["short_strike"],
                st["qty"], "credit" if st["credit"] else "debit",
                abs(st["entry"]), value, ret_pct, rec["peak"], quiet,
                verdict + verdict_scope + iv_note,
                ("  [INFERRED pairing — observation only]"
                 if st.get("inferred") and not MANAGE_INFERRED
                 else ("" if manageable else "  [observation only]")),
            )
            if reason and manageable:
                got = _close(st, reason, value)
                if got is not None:
                    filled, filled_qty = got
                    # Book what it FILLED at, not the mark that triggered it.
                    entry = abs(st["entry"])
                    real_pct = ((((entry - filled) if st["credit"] else (filled - entry))
                                 / entry * 100.0) if entry else ret_pct)
                    if abs(filled - value) > 0.005:
                        logger.info(
                            "ORPHAN fill %.2f against a %.2f mark (%+.1f%% not %+.1f%%) "
                            "— booking the fill.", filled, value, real_pct, ret_pct)
                    _book(st, filled, real_pct, reason, qty=filled_qty)
                    if filled_qty >= st["qty"]:
                        peaks.pop(key, None)
                        state["structures"].pop(key, None)
                    else:
                        # A PARTIAL close leaves a smaller structure open. Keep
                        # the peak, shrink the cache, and let the next pass see
                        # what is left rather than treating it as finished.
                        logger.warning(
                            "ORPHAN partial close: %d of %d %s %.0f/%.0f filled — "
                            "%d contracts remain.", filled_qty, st["qty"], st["root"],
                            st["long_strike"], st["short_strike"], st["qty"] - filled_qty)
                        rec_s = state["structures"].get(key)
                        if rec_s:
                            rec_s["qty"] = st["qty"] - filled_qty
            reported.append(st)

        # A structure that has vanished since the last pass was closed by
        # someone -- the human, an assignment, an expiry. Book what we can and
        # stop tracking its peak.
        for gone in [k for k in peaks if k not in seen]:
            peaks.pop(gone, None)
        _save(state)
        return reported
    except Exception:
        logger.exception("Orphan review failed — continuing; this must not break a cycle.")
        return []
