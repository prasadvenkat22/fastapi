"""Place the day's best 0DTE debit spreads, ranked by EV, sized to a budget.

    python scripts/dte0_trade.py                       # DRY RUN, prints only
    python scripts/dte0_trade.py --live                # places orders
    python scripts/dte0_trade.py --budget 1500 --max-trades 3

THIS ONE TRADES, which no other script in this repository does, so the guards
are listed before anything else.

    --live is required            dry run is the default and prints the same
                                  plan without sending it
    TRADING_DTE0_LIVE=true        must ALSO be set; --live alone does nothing
    TRADING_DTE0_MAX_BUDGET       hard ceiling, 1500, applied after sizing
    --max-trades                  3 by default, one per underlying
    already-held check            refuses a symbol the account already holds
                                  an option in for today's expiry, so a rerun
                                  cannot double a position
    MAX_ORDER_CONTRACTS           the same clamp every order passes through.
                                  Sizing respects it rather than discovering
                                  it: on 2026-09-11 an 18-lot exit became four
                                  fills a minute apart and cost $72 of slippage

WHAT IT IS BUYING, AND WHY ONLY DEBITS. Credit structures lock capital against
the full width and this book has no measured record selling premium; IV/RV on
2026-09-12 came back 0.48 to 0.83 across these names, which says implied is
BELOW realised and premium is cheap. Buying is what that reading supports.

RANKED BY EV FROM THE SCREENER, not from a second implementation. rank()
is what /screener/verticals and weekly_pick.py both call, so a trade placed
here and a row printed there cannot diverge.

AND FILTERED BY THE THREE CONSTRAINTS, each of which cost money to learn:

    entry 30-65% of width   above it the +30% target is unreachable, since
                            max return is (width-entry)/entry and 0.77 of
                            width IS 30%. Below it the premium is mostly time
                            value and the -10% stop sits nearer than one
                            morning of theta.
    extrinsic under 25%     the same failure in the units that cause it
    target within 0.3 ATR   the move to +30% must be an ordinary session

The screener ranks by EV and knows nothing about any of that -- it happily
returned NVDA 200/220 at +28 points of edge with 35% of its premium in time
value. EV picks the best of what is sound; the constraints decide what is
sound.

EXITS ARE NOT THIS SCRIPT'S JOB. Whatever it opens is picked up by
trading_engine/orphans.py on the next cycle and managed by the same ladder as
every manual position: -10% stop (intrinsic-guarded), +30% target, a trail at
15% of the profit band, and the 15:45 flatten. Nothing here needs to know
that, and nothing here should duplicate it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_engine import tradier_orders  # noqa: E402
from trading_engine.screener import rank  # noqa: E402

logger = logging.getLogger("dte0_trade")
NY = ZoneInfo("America/New_York")

LIVE_ENABLED = os.getenv("TRADING_DTE0_LIVE", "false").lower() == "true"
MAX_BUDGET = float(os.getenv("TRADING_DTE0_MAX_BUDGET", "1500"))
# QQQ IS DELIBERATELY ABSENT. The engine trades QQQ 0DTE itself from 09:45
# through its own playbook, and a second QQQ position placed here would be an
# independent bet on the same underlying, sized separately, with the engine
# logging a RECONCILE error every minute because it tracks its position in its
# own database rather than from the broker. Duplicating the one instrument the
# engine already covers is the opposite of diversifying into single names.
SYMBOLS = os.getenv("TRADING_DTE0_TRADE_SYMBOLS", "NVDA,MU,META")
MIN_EW = float(os.getenv("TRADING_PICK_MIN_ENTRY_WIDTH", "0.30"))
MAX_EW = float(os.getenv("TRADING_PICK_MAX_ENTRY_WIDTH", "0.65"))
MAX_EXTRINSIC = float(os.getenv("TRADING_PICK_MAX_EXTRINSIC", "25.0"))
MAX_TARGET_ATR = float(os.getenv("TRADING_PICK_MAX_TARGET_ATR", "0.30"))
TARGET_PCT = float(os.getenv("TRADING_ORPHAN_TARGET_RETURN_PCT", "30.0"))


def _passes(r: dict) -> "str | None":
    """None if the row clears all three constraints, else why it did not."""
    cost, w, spot = float(r["cost"]), float(r["w"]), float(r["spot"])
    if w <= 0 or cost <= 0:
        return "degenerate price"
    ew = cost / w
    if not (MIN_EW <= ew <= MAX_EW):
        return f"entry {ew:.0%} of width, outside {MIN_EW:.0%}-{MAX_EW:.0%}"
    # For a debit the long leg is the strike nearer the money: the LOW strike
    # on a call, the HIGH strike on a put.
    bullish = r.get("direction") != "bearish"
    long_k = float(r["lo"]) if bullish else float(r["hi"])
    intr = (min(max(spot - long_k, 0.0), w) if bullish
            else min(max(long_k - spot, 0.0), w))
    extr = cost - intr
    if extr < 0:
        return "negative extrinsic — crossed or stale quote"
    ex_pct = extr / cost * 100.0
    if ex_pct > MAX_EXTRINSIC:
        return f"extrinsic {ex_pct:.0f}% of premium, above {MAX_EXTRINSIC:.0f}%"
    tgt_mark = cost * (1 + TARGET_PCT / 100.0)
    if tgt_mark > w:
        return f"+{TARGET_PCT:.0f}% target above the {w:.1f} width"
    tgt_spot = (long_k + tgt_mark) if bullish else (long_k - tgt_mark)
    atr = float(r.get("atr") or 0)
    if atr <= 0:
        return "no ATR"
    move_atr = abs(tgt_spot - spot) / atr
    if move_atr > MAX_TARGET_ATR:
        return f"target needs {move_atr:.2f} ATR, above {MAX_TARGET_ATR:.2f}"
    r["_ew"], r["_ex_pct"], r["_move_atr"] = ew, ex_pct, move_atr
    r["_target_spot"], r["_long"] = tgt_spot, long_k
    return None


def _held_today(symbols: set, expiry: str) -> set:
    """Underlyings the account already holds an option in for `expiry`.

    A rerun must not double a position, and the broker is the only honest
    source for what is held -- the same reasoning as the orphan close guard.
    """
    held = set()
    try:
        ymd = expiry.replace("-", "")[2:]
        for p in (tradier_orders.open_positions() or []):
            sym = str(p.get("symbol", ""))
            for s in symbols:
                if sym.startswith(s) and ymd in sym:
                    held.add(s)
    except Exception:
        logger.warning("Could not read positions — refusing to place anything.",
                       exc_info=True)
        return symbols          # fail CLOSED: unknown means do not trade
    return held


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=SYMBOLS)
    ap.add_argument("--budget", type=float, default=1500.0)
    ap.add_argument("--max-trades", type=int, default=3)
    ap.add_argument("--by", default="ev", choices=("ev", "evpct", "prob", "edge"))
    ap.add_argument("--expiry", default="")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    budget = min(args.budget, MAX_BUDGET)
    if budget < args.budget:
        logger.info("Budget clamped to the $%.0f ceiling.", MAX_BUDGET)
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    exp = args.expiry or date.today().isoformat()
    now = datetime.now(NY)
    live = args.live and LIVE_ENABLED
    if args.live and not LIVE_ENABLED:
        logger.warning("--live given but TRADING_DTE0_LIVE is not true — DRY RUN.")

    # The per-trade allowance has to be known BEFORE selection, or a symbol
    # whose best-EV structure is unaffordable gets dropped entirely instead of
    # falling back to one that fits. MU on 2026-09-12 was exactly that: its
    # top row was a 50-wide at $1,936 and the name vanished from the plan.
    per_trade_cap = budget / max(args.max_trades, 1)

    # Best surviving candidate per symbol per side.
    best: dict = {}
    for side in ("call", "put"):
        try:
            res = rank(syms, side, by=args.by, top=60, structure="debit", expiry=exp)
        except Exception:
            logger.warning("rank() failed for %s — skipped.", side, exc_info=True)
            continue
        for r in res.get("rows", []):
            why = _passes(r)
            if why:
                continue
            # A NEGATIVE EDGE IS NOT A TRADE. Ranking by EV alone happily
            # returned META at Pwin 50.1% against a 54.3% break-even -- the
            # best of a bad set is still bad, and "best available" is not a
            # reason to buy something the screener prices as losing.
            if r["ev_dem"] <= 0 or r["pwin"] <= r["need"]:
                continue
            # Affordability is a selection criterion, not a post-check.
            if float(r["cost"]) * 100 > per_trade_cap:
                continue
            key = r["sym"]
            if key not in best or r["ev_dem"] > best[key]["ev_dem"]:
                r["_side"] = side
                best[key] = r

    if not best:
        logger.info("Nothing cleared the constraints on %s. That is an answer.", exp)
        return

    chosen = sorted(best.values(), key=lambda r: -r["ev_dem"])[:args.max_trades]
    # SIZE AGAINST THE SLOT, NOT AGAINST WHAT QUALIFIED. Dividing the budget
    # by the number of survivors concentrates the whole allowance into one
    # name on a day when only one clears the filters -- which is precisely the
    # day to be smaller, not larger, since the filters just told you the rest
    # of the board was untradeable. Unspent budget stays unspent.
    per = per_trade_cap
    held = _held_today({r["sym"] for r in chosen}, exp)

    logger.info("%s  budget $%.0f over %d trade(s) = $%.0f each  ranked by %s",
                now.strftime("%Y-%m-%d %H:%M %Z"), budget, len(chosen), per, args.by)
    placed = 0
    for r in chosen:
        sym, side, cost, w = r["sym"], r["_side"], float(r["cost"]), float(r["w"])
        long_k = r["_long"]
        short_k = float(r["hi"]) if long_k == float(r["lo"]) else float(r["lo"])
        qty = min(int(per // (cost * 100)), tradier_orders.MAX_CONTRACTS)
        logger.info(
            "%-5s %-4s %.0f/%.0f w%.1f x%d @ %.2f = $%.0f | Pwin %.1f%% need %.1f%% "
            "EV $%+.0f | entry %.0f%% of width, extr %.0f%%, target %s %.2f (%.2f ATR)",
            sym, side.upper(), long_k, short_k, w, qty, cost, cost * 100 * qty,
            r["pwin"] * 100, r["need"] * 100, r["ev_dem"], r["_ew"] * 100,
            r["_ex_pct"], sym, r["_target_spot"], r["_move_atr"])
        if qty < 1:
            logger.info("   costs $%.0f, above the $%.0f per-trade budget — skipped.",
                        cost * 100, per)
            continue
        if sym in held:
            logger.info("   the account already holds %s options expiring %s "
                        "— skipped rather than doubled.", sym, exp)
            continue
        if not live:
            logger.info("   DRY RUN — not sent.")
            continue
        try:
            res = tradier_orders.submit_vertical(
                sym, exp, "call" if side == "call" else "put",
                long_strike=long_k, short_strike=short_k, quantity=qty,
                opening=True, limit_price=cost, is_credit=False)
            logger.info("   ORDER SENT: %s", res)
            placed += 1
        except Exception:
            logger.exception("   order failed for %s — nothing opened.", sym)

    if live:
        logger.info("%d order(s) sent. Exits are orphans.py's job: -10%% stop, "
                    "+%.0f%% target, trail, 15:45 flatten.", placed, TARGET_PCT)
    else:
        logger.info("DRY RUN. Re-run with --live and TRADING_DTE0_LIVE=true to place.")


if __name__ == "__main__":
    main()
