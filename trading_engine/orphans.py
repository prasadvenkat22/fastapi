"""Positions the engine did not open, watched with the engine's own rules.

Why this exists
---------------
2026-09-02: a manual QQQ 700/708 spread was opened at 09:43. The engine logged
RECONCILE at ERROR once a minute for the rest of the session and did nothing
else with it -- no stop, no stalled-peak exit, no ratchet. It then opened its
own position sized as though the account were empty. A position nobody is
watching is the one that hurts, and "it isn't ours" is not a reason for it to
go unwatched when the same account, the same underlying and the same expiry
are involved.

STAGE ONE: THIS OBSERVES. IT DOES NOT CLOSE.
--------------------------------------------
Every rule here logs and stops. Nothing in this module places an order and
nothing calls the broker's sell path. That is deliberate and it is not
timidity -- the four reasons are all live, and every one of them would have
produced a wrong action today:

  ONE POSITION. The engine's exit ladder models a single open position. On
  2026-09-02 the account held three distinct structures at once.

  PAIRING IS AMBIGUOUS. Nine long calls against nine short calls across five
  strikes admit several valid pairings. The engine's own naked-leg check got
  this wrong the same afternoon: it read a fully covered book as naked,
  because it looked only at the two strikes it expected and found one of them
  missing.

  COST BASIS LIES. Tradier averages basis across contracts bought and sold
  under one position id. The 700 leg's basis moved from 3540.00 to 4251.92
  while the quantity stayed at 5. Any return computed from that is wrong, and
  every rule below reads a return.

  INTENT IS UNKNOWN. A -25% stop applied to a spread a human meant to carry
  into expiry closes something they wanted kept. The engine can see the
  position; it cannot see the plan.

So this reports what the ladder WOULD have said. If the pairing and the
marks prove reliable over some weeks, an opt-in can let it act -- and that
decision should be made on this module's own logs, not on an argument.

WHAT IT PAIRS
-------------
Legs are grouped by underlying, expiry and right, then longs and shorts are
matched cheapest-strike-first into verticals. That is the pairing that
maximises the value of the resulting spreads, which is the conservative
reading: it never invents a naked leg that a different pairing would have
covered. Genuinely unpaired legs are reported as singles rather than folded
into a spread they do not belong to.
"""
import json
import logging
import os
from datetime import datetime, timezone

from . import tradier_orders

logger = logging.getLogger(__name__)

# Watch orphans at all. Off leaves today's behaviour exactly as it was: a
# RECONCILE line and nothing else.
WATCH_ORPHANS = os.getenv("TRADING_WATCH_ORPHANS", "false").lower() == "true"

# The thresholds the report is written against. Deliberately the same
# variables the engine manages its own positions with, so the log reads as
# "what the ladder would have said" rather than as a second opinion.
STALL_MINUTES = float(os.getenv("TRADING_STALL_MINUTES", "0"))
STALL_GIVEBACK_PCT = float(os.getenv("TRADING_STALL_GIVEBACK_PCT", "0"))
ORPHAN_STOP_PCT = float(os.getenv("TRADING_ORPHAN_STOP_PCT", "-25"))

# Peak tracking has to survive a container recreate or the stall resets to
# zero every deploy and can never fire. A file under the mounted working
# directory, not a table: this is observational, and a migration for data
# nothing reads back is a cost with no return yet.
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
        logger.exception("Could not persist orphan peaks — the stall will restart cold.")


def _parse(symbol: str) -> "tuple | None":
    """(root, expiry, right, strike) from an OCC symbol, or None.

    The tail is fixed width -- 6 digits of date, one letter, 8 digits of
    strike in thousandths -- so the root is whatever precedes it. Same rule as
    tradier_orders.occ_root, which is why that is used for the root rather
    than re-deriving it here.
    """
    try:
        root = tradier_orders.occ_root(symbol)
        tail = symbol[len(root):]
        if len(tail) != 15:
            return None
        return root, tail[:6], tail[6], int(tail[7:]) / 1000.0
    except Exception:
        return None


def _pair(legs: list) -> list:
    """Match longs against shorts into verticals, cheapest strike first.

    Conservative on purpose: pairing the lowest long with the lowest short
    maximises the intrinsic value of the resulting spreads, so this never
    reports a naked short that some other pairing would have covered. The
    opposite convention would manufacture alarms.
    """
    longs = sorted([l for l in legs if l["qty"] > 0], key=lambda l: l["strike"])
    shorts = sorted([s for s in legs if s["qty"] < 0], key=lambda s: s["strike"])
    spreads, li, si = [], 0, 0
    while li < len(longs) and si < len(shorts):
        lo, sh = longs[li], shorts[si]
        n = min(lo["qty"], -sh["qty"])
        spreads.append({"long": lo["symbol"], "short": sh["symbol"],
                        "long_strike": lo["strike"], "short_strike": sh["strike"],
                        "qty": n, "right": lo["right"], "expiry": lo["expiry"]})
        lo["qty"] -= n
        sh["qty"] += n
        if lo["qty"] == 0:
            li += 1
        if sh["qty"] == 0:
            si += 1
    singles = ([l for l in longs if l["qty"] > 0] + [s for s in shorts if s["qty"] < 0])
    return spreads, singles


def review(engine_symbols: "set | None" = None) -> list:
    """Log what the exit ladder would say about every position we did not open.

    engine_symbols are the legs belonging to the engine's own open row; they
    are excluded so this never double-reports the position the engine is
    already managing.

    Returns the spread dicts it reported, for callers that want to assert on
    them. Never raises: an observational pass must not be able to break a
    trading cycle.
    """
    if not WATCH_ORPHANS:
        return []
    try:
        engine_symbols = engine_symbols or set()
        legs = []
        for p in tradier_orders.open_positions():
            sym = p.get("symbol")
            if not sym or sym in engine_symbols:
                continue
            parsed = _parse(sym)
            if parsed is None:
                continue
            root, expiry, right, strike = parsed
            legs.append({"symbol": sym, "root": root, "expiry": expiry,
                         "right": right, "strike": strike,
                         "qty": int(float(p.get("quantity") or 0)),
                         "basis": float(p.get("cost_basis") or 0.0)})
        if not legs:
            return []

        groups = {}
        for l in legs:
            groups.setdefault((l["root"], l["expiry"], l["right"]), []).append(l)

        state = _load()
        now = datetime.now(timezone.utc)
        reported = []
        for (root, expiry, right), grp in sorted(groups.items()):
            spreads, singles = _pair(grp)
            for sp in spreads:
                key = f"{sp['long']}|{sp['short']}"
                mark = _mark(sp)
                if mark is None:
                    continue
                value, ret_pct = mark
                st = state.get(key) or {"peak": ret_pct, "peak_at": now.isoformat()}
                if ret_pct > st["peak"]:
                    st = {"peak": ret_pct, "peak_at": now.isoformat()}
                state[key] = st
                quiet = (now - datetime.fromisoformat(st["peak_at"])).total_seconds() / 60.0

                verdict = "holding"
                if ret_pct <= ORPHAN_STOP_PCT:
                    verdict = f"WOULD STOP OUT (past {ORPHAN_STOP_PCT:+.0f}%)"
                elif (STALL_MINUTES > 0 and st["peak"] > 0
                      and quiet >= STALL_MINUTES
                      and ret_pct <= st["peak"] - STALL_GIVEBACK_PCT):
                    verdict = (f"WOULD BOOK ON STALL (peaked {st['peak']:+.1f}% "
                               f"{quiet:.0f} min ago)")
                logger.info(
                    "ORPHAN %s %s %.0f/%.0f x%d: value %.2f, %+.1f%% "
                    "(peak %+.1f%%, %.0f min ago) — %s. Observation only, no order placed.",
                    root, right, sp["long_strike"], sp["short_strike"], sp["qty"],
                    value, ret_pct, st["peak"], quiet, verdict,
                )
                reported.append(sp)
            for sg in singles:
                logger.warning(
                    "ORPHAN SINGLE %s %s %.0f x%d — unpaired leg, no vertical to mark it against.",
                    root, right, sg["strike"], sg["qty"],
                )
        _save(state)
        return reported
    except Exception:
        logger.exception("Orphan review failed — continuing; this watches, it does not trade.")
        return []


def _mark(sp: dict) -> "tuple | None":
    """(value, return_pct) for one spread, from live quotes.

    Return is measured against the spread's CURRENT width-normalised value,
    not against cost basis -- see the module docstring on why Tradier's basis
    cannot be trusted here. For a debit structure that means return against
    what the spread is worth versus its maximum; it is a coarser number than
    the engine's own return_pct and it is the honest one available.
    """
    try:
        quotes = tradier_orders.quotes([sp["long"], sp["short"]])
        if not quotes:
            return None
        lq, sq = quotes.get(sp["long"]), quotes.get(sp["short"])
        if not lq or not sq:
            return None
        value = float(lq.get("bid") or 0.0) - float(sq.get("ask") or 0.0)
        width = abs(sp["long_strike"] - sp["short_strike"])
        if width <= 0:
            return None
        # Percent of the structure's maximum value. At 100% the spread is worth
        # its full width and there is nothing left to gain by holding.
        return value, value / width * 100.0
    except Exception:
        return None
