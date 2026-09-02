"""Shared entry point for running one trading cycle and persisting the
result — used by both the manual POST /trading/run-daily-cycle endpoint and
the optional background scheduler (trading_engine/scheduler.py), so the two
never drift out of sync.

Also where realized P&L gets recorded: an OpenPosition row is deleted
whenever a position fully closes, so without writing a TradeHistory row here
first, the result of that trade (profit or loss) would simply disappear."""

import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from GENAI.vector_stores import VoyageEmbeddings
from models_pgdb.trading_models import OpenPosition, TradeHistory, TradingLog

from .broker import (MockBrokerClient, MockSpreadPosition, estimate_credit_value, option_type_for,
                      estimate_spread_value, fill_price, is_credit)
from .data_feed import chain_vertical, fetch_qqq_spot, log_price_divergence, today_expiry
from .equity import current_equity
from .graph import run_trading_cycle
from .nodes import POSITION_BUDGET, TAKE_PROFIT_PCT, is_past_force_close
from .setup_vector_store import store_trade_setup
from . import orphans, tradier_orders, weekly_shadow
from .state import TradingState

# The only underlying this engine trades. Used to scope the broker
# reconciliation, because the account is shared with manual positions in
# other names and those are not the engine's business to report on.
ENGINE_UNDERLYING = os.getenv("TRADING_UNDERLYING", "QQQ").strip().upper()

# How long after an entry to let the broker's position list catch up
# before treating a missing leg as a divergence. One cron cycle is 60s;
# 90 gives the acknowledgement-to-position lag room without hiding a
# fill that genuinely never arrived.
RECONCILE_GRACE_SECONDS = float(os.getenv("TRADING_RECONCILE_GRACE", "90"))

logger = logging.getLogger(__name__)


def _classify_close_reason(exit_reason: str, return_pct: float) -> str:
    """Prefer the reason the rule engine actually recorded when it closed the
    position (nodes.execution_risk_agent). Re-deriving it from P&L here can't
    tell a RISK_OFF exit from an ordinary stop — both are just "losing" — and
    re-checking the clock could disagree with the agent across a minute
    boundary. The P&L fallback covers a close arriving with no reason set."""
    if exit_reason:
        return exit_reason
    if is_past_force_close():
        return "FORCE_CLOSE"
    if return_pct >= TAKE_PROFIT_PCT:
        return "TAKE_PROFIT"
    return "STOP_LOSS"



def _route_order(position_like, quantity: int, opening: bool, limit_price: float,
                 label: str) -> "dict | None":
    """Send the order the engine's decision implies, and log what came back.

    No longer shadow. This began as a logging-only path, with the engine's own
    row authoritative and the broker's answer recorded but never acted on --
    the same staged approach taken with chain pricing, where both numbers were
    logged for a session before either was trusted.

    Closes have since been promoted: the caller reads the returned order and
    an explicit rejection keeps the position open (see _close_rejected).
    OPENS are still shadow in exactly the old sense -- the row is written
    whatever the broker says -- which is a known gap, visible as a RECONCILE
    line rather than as a corrected position.

    Guarded completely. Every P&L figure in this repository already assumes a
    fill it never received; an exception here must not also cost a cycle.
    """
    if not tradier_orders.LIVE_ORDERS:
        return None
    try:
        result = tradier_orders.submit_vertical(
            position_like.underlying if hasattr(position_like, "underlying") else "QQQ",
            today_expiry(),
            option_type_for(position_like.strategy),
            long_strike=position_like.long_strike,
            short_strike=position_like.short_strike,
            quantity=quantity,
            opening=opening,
            limit_price=limit_price,
            is_credit=is_credit(position_like.strategy),
        )
        logger.info("Order [%s] %s: %s", label, "OPEN" if opening else "CLOSE", result)
        return result
    except Exception:
        logger.exception("Order [%s] failed — the engine's own state is unchanged.", label)
        return None


# Terminal states in which the broker has refused or abandoned an order. A
# close that reaches one of these did NOT happen, whatever the submit call
# answered at the time.
_DEAD_ORDER_STATES = {"rejected", "canceled", "cancelled", "expired", "error"}


def _open_rejected(order_result: "dict | None") -> bool:
    """Did the broker REFUSE this open?

    The mirror of _close_rejected, and it should have existed at the same
    time. Until 2026-09-02 an OPEN was pure shadow: the row was written
    whatever the broker answered, which the docstring above called "a known
    gap, visible as a RECONCILE line rather than as a corrected position".

    On 2026-09-02 that gap cost a session. The entry order was rejected --

        14:45:14.640755  OpenPosition row written: BULL_CALL_SPREAD 708/718 x3
        14:45:14.730     order created
        14:45:14.777     order REJECTED

    -- and the row survived it by ninety milliseconds and five hours. The
    engine then believed it held a spread that had never existed and spent the
    afternoon trying to close it: 189 rejected two-leg buy_to_close orders
    against a short 718 that was never opened, one every thirteen seconds.
    Worse than the noise, the real book went unmanaged the whole time, because
    the single position slot was occupied by a fiction.

    A row written for a rejected open is not a divergence to be reported. It
    is a position that does not exist, and the engine must not carry one.

    Same conservatism as the close path: only an EXPLICIT refusal counts. An
    order still working when the poll runs out is treated as standing and the
    row is kept, because an open that fills after we declined to record it is
    the worse failure -- that one leaves real contracts with nothing watching
    them, which is precisely what this whole section is about.
    """
    return _close_rejected(order_result)


def _close_rejected(order_result: "dict | None") -> bool:
    """Did the broker REFUSE this close?

    submit_vertical answering {'status': 'ok'} means "accepted for
    processing", not "filled", and the two came apart on 2026-08-27: an
    external buyback removed the short leg, the engine's multileg exit was
    rejected a second after being accepted, and the caller booked it anyway.
    A TradeHistory row was written for a close that never happened and two
    real contracts went unmanaged into expiry.

    Only an EXPLICIT refusal counts. An order still working when the poll runs
    out is left alone and treated as standing, because the opposite mistake is
    worse: a row kept open on an order that then fills would have the next
    cycle try to sell a position the account no longer holds. Between booking
    a close that is merely slow and double-closing one that already went
    through, the slow case is the recoverable one -- _reconcile compares
    against the broker every cycle and says so out loud.
    """
    if not tradier_orders.LIVE_ORDERS or not order_result:
        return False
    order_id = order_result.get("id")
    if not order_id:
        return False
    for attempt in range(6):
        try:
            status = (tradier_orders.order_status(order_id).get("status") or "").lower()
        except Exception:
            logger.exception("Could not read close order %s — treating it as standing.", order_id)
            return False
        if status in _DEAD_ORDER_STATES:
            logger.error("Close order %s came back %s.", order_id, status)
            return True
        if status == "filled":
            return False
        if attempt < 5:
            time.sleep(1.5)
    logger.warning(
        "Close order %s still %s after the poll — booking it and letting RECONCILE "
        "catch it if it never fills.", order_id, status or "unknown")
    return False


def _reconcile(open_row) -> None:
    """Does the broker agree with us about what is open?

    Our row is a belief; the broker's position list is the fact. Comparing
    them every cycle is what turns a silent divergence -- an order that never
    filled, a leg that did -- into something visible while it can still be
    acted on.
    """
    if not tradier_orders.LIVE_ORDERS:
        return
    try:
        if open_row is None:
            # Only the engine's OWN underlying. The account is shared with a
            # human who trades other names in it, and before this filter every
            # manual position raised RECONCILE at ERROR once a minute -- which
            # is worse than useless, because a real engine/broker divergence
            # would then arrive in a stream of alerts already known to be
            # noise. Observed 2026-08-28: MU, SNDK and MRVL put spreads, none
            # of them the engine's, flagged on every cycle.
            held = [
                p for p in tradier_orders.open_positions()
                if tradier_orders.occ_root(p.get("symbol")) == ENGINE_UNDERLYING
            ]
            if held:
                logger.error(
                    "RECONCILE: the engine believes it is flat, the broker holds %d %s position(s): %s",
                    len(held), ENGINE_UNDERLYING, [p.get("symbol") for p in held],
                )
            # ... and say what the exit ladder WOULD do about them. The error
            # above has been the whole of the response since it was written:
            # on 2026-09-02 it fired 34 times at once a minute while a manual
            # spread ran unmanaged all morning. Observation only -- orphans.py
            # places no orders.
            orphans.review()
            return
        # A just-submitted order is not a divergence. Tradier's position list
        # lags its own order acknowledgement by more than the second between
        # submitting and reconciling, so the first cycle after an entry
        # reported "the broker holds neither leg" every time -- an ERROR for
        # the ordinary case. Observed 2026-08-28 at 14:35:05, one second after
        # the order that did fill. Anything still missing on the NEXT cycle is
        # a real divergence and is reported as before.
        opened_at = getattr(open_row, "opened_at", None)
        if opened_at is not None:
            age = (datetime.now(timezone.utc) - opened_at).total_seconds()
            if age < RECONCILE_GRACE_SECONDS:
                return
        call_put = option_type_for(open_row.strategy)
        short_sym = tradier_orders.occ_symbol("QQQ", today_expiry(), call_put, open_row.short_strike)
        long_sym = tradier_orders.occ_symbol("QQQ", today_expiry(), call_put, open_row.long_strike)
        fill = tradier_orders.check_fill(short_sym, long_sym)
        # Everything that is NOT the engine's two legs, marked and reported.
        orphans.review(engine_symbols={short_sym, long_sym})
        if fill["naked"]:
            logger.error("RECONCILE: naked leg detected — %s", fill)
        elif not fill["complete"]:
            logger.error(
                "RECONCILE: the engine holds %s %d contracts, the broker holds neither leg.",
                open_row.strategy, open_row.quantity,
            )
    except Exception:
        logger.exception("Reconciliation failed — continuing on the engine's own state.")


async def execute_and_persist_cycle(db: Session) -> TradingState:
    open_row = db.query(OpenPosition).first()

    pre_close_current_value = None
    pre_close_return_pct = None

    if open_row is not None:
        # No live option-chain feed is wired up — reprice the open spread
        # against today's live QQQ spot with the same model that priced the
        # entry (see broker.estimate_spread_value). Repricing on intrinsic
        # alone, as this once did, valued a freshly opened long-ITM/short-ATM
        # spread at its full width immediately: every position showed a paper
        # gain on the next cycle and took profit regardless of the market.
        spot = fetch_qqq_spot()
        # Marked where the position could actually be closed. A debit spread
        # is sold, so it marks at the bid; a credit spread is bought back, so
        # it marks at the ask — the cost side is opposite because the trade is.
        if is_credit(open_row.strategy):
            current_value = fill_price(
                estimate_credit_value(open_row.strategy, open_row.short_strike, open_row.long_strike, spot),
                "buy",
            )
        else:
            current_value = fill_price(
                estimate_spread_value(open_row.strategy, open_row.long_strike, open_row.short_strike, spot),
                "sell",
            )
        # The same comparison the entry makes, repeated every cycle a position
        # is open: this is where the model's mark drives the stop, the trail
        # and the ratchet, so a mark that drifts from the market is a decision
        # made on a price that does not exist. Guarded — an observational log
        # must never interrupt managing a live position.
        market_quote = None
        try:
            buy_leg, sell_leg = (
                (open_row.short_strike, open_row.long_strike) if is_credit(open_row.strategy)
                else (open_row.long_strike, open_row.short_strike)
            )
            market_quote = log_price_divergence(
                option_type_for(open_row.strategy), buy_leg, sell_leg, current_value,
                f"open {open_row.playbook or open_row.strategy}",
            )
        except Exception:
            logger.exception("Chain mark failed — continuing with the model mark.")

        # Mark from the chain when it has these strikes, and fall back to the
        # model when it does not.
        #
        # The model is not a small calibration away from the market. Measured
        # live on 2026-08-21, it priced an open 716/720 call credit spread at
        # 0.20-0.34 all afternoon while the market quoted 0.01-0.04 -- an
        # 88-97% overstatement of what closing it cost. Every exit rule reads
        # this number, and the ratchet duly closed that position at "+27%"
        # when the market said it was worth +96% of its maximum.
        #
        # Mid, not natural: the mid is what a position is worth, the natural
        # is what a fill costs, and entries price at the natural separately.
        if market_quote is not None:
            current_value = max(market_quote["mid"], 0.0)

        position = MockSpreadPosition(
            strategy=open_row.strategy,
            underlying=open_row.underlying,
            quantity=open_row.quantity,
            long_strike=open_row.long_strike,
            short_strike=open_row.short_strike,
            entry_net_debit=open_row.entry_net_debit,
            current_net_value=current_value,
            playbook=open_row.playbook or "",
            peak_return_pct=open_row.peak_return_pct or 0.0,
            peak_at=open_row.peak_at,
            entry_tranche_qty=open_row.entry_tranche_qty or 0,
            entry_slices_remaining=open_row.entry_slices_remaining or 0,
            opened_at=open_row.opened_at,
        )
        pre_close_current_value = current_value
        pre_close_return_pct = position.return_pct
        # Ratchet before the rules run, so a peak set this cycle is visible to
        # the retracement check in the same cycle rather than one late.
        if pre_close_return_pct > position.peak_return_pct:
            position.peak_return_pct = pre_close_return_pct
            open_row.peak_return_pct = pre_close_return_pct
            # Stamp the peak's TIME too. Without it the stalled-peak exit
            # cannot tell a dip inside a climb from the end of one, which is
            # the whole difference between it and a giveback ratchet.
            now_peak = datetime.now(timezone.utc)
            position.peak_at = now_peak
            open_row.peak_at = now_peak
        spent = open_row.quantity * open_row.entry_net_debit * 100
        broker = MockBrokerClient(position=position, available_cash=max(POSITION_BUDGET - spent, 0))
    else:
        # Flat: cash available is realized equity, not the static budget, so
        # sizing follows the account rather than a number frozen in the env.
        broker = MockBrokerClient(position=None, available_cash=current_equity(POSITION_BUDGET).equity)

    final_state = await run_trading_cycle(broker=broker)

    # Sync the persisted position with whatever the broker ended up holding —
    # entered, added to, fully closed, or unchanged (HOLD) — so the next
    # cycle picks it up correctly. Updated in place rather than
    # delete+reinsert when it stays open, so opened_at isn't reset to "now"
    # on every HOLD cycle.
    new_position = broker.get_open_position()

    closed_trade = None  # set below if a position fully closed this cycle

    # A cycle can now close one position and open another (same-cycle
    # re-entry after a take-profit), so "was open, is open" no longer implies
    # nothing closed. exit_reason is what actually says a position closed —
    # keying off new_position alone would take the update-in-place branch and
    # silently lose the closed trade's realized P&L.
    # The weekly shadow, which trades nothing. Guarded and last-ish so a
    # note-taking feature can never interfere with a live position: an
    # exception here must cost a log line, not a cycle.
    try:
        weekly_shadow.observe(db)
    except Exception:
        logger.exception("Weekly shadow failed — the trading cycle is unaffected.")

    closed_this_cycle = bool(final_state.get("exit_reason"))

    if open_row is not None and closed_this_cycle:
        # Book the exit at the NATURAL, not at the mark.
        #
        # Decisions and accounting want different prices, and conflating them
        # flatters the record. The mark is the mid, which is what the position
        # is worth and the right input to a stop or a ratchet -- marking at the
        # natural would make every position look half a spread worse than it is
        # and fire exits early. But a fill is not the mid: closing a debit
        # vertical sells it at its natural BID, closing a credit one buys it
        # back at the natural ASK.
        #
        # Entries already price at the natural, so recording exits at the mid
        # credited every trade with roughly half a spread it would never have
        # received -- about $0.075 a contract on a morning vertical quoting
        # 3.88/4.03, or $37 on a five-contract trade. scripts/sweep.py charges
        # a full round trip, so the backtest was honest and only the live
        # record ran optimistic, which is the number Monday gets judged on.
        exit_value = pre_close_current_value
        if market_quote is not None:
            natural = market_quote["ask"] if is_credit(open_row.strategy) else market_quote["bid"]
            exit_value = max(natural, 0.0)
            logger.info(
                "Exit booked at the natural %.3f rather than the mid %.3f (%.3f a contract of spread).",
                exit_value, pre_close_current_value, abs(pre_close_current_value - exit_value),
            )

        # Credit positions profit as the spread gets CHEAPER, so the sign
        # flips: entry_net_debit holds the credit received, and the gain is
        # what is left after buying it back.
        per_spread = (
            (open_row.entry_net_debit - exit_value)
            if is_credit(open_row.strategy)
            else (exit_value - open_row.entry_net_debit)
        )
        # A submitted close is not a completed one -- see _close_rejected.
        # On an explicit refusal the position row STAYS, nothing is recorded,
        # and the next cycle sees a live position again and can act on it.
        order_result = _route_order(
            open_row, open_row.quantity, opening=False,
            limit_price=exit_value, label=open_row.playbook or open_row.strategy)

        if _close_rejected(order_result):
            logger.error(
                "EXIT REJECTED [%s]: %s %s/%s x%d is STILL OPEN. Nothing booked, "
                "the row is kept so the next cycle can retry.",
                open_row.playbook or open_row.strategy, open_row.strategy,
                open_row.long_strike, open_row.short_strike, open_row.quantity,
            )
        else:
            realized_dollars = per_spread * open_row.quantity * 100
            # The percentage has to describe the same trade as the dollars. The
            # rule still FIRED on the mid-based return -- that is what the stop and
            # the ratchet read, and _classify_close_reason is judging the decision,
            # not the fill -- but what gets recorded as the result is the realised
            # one.
            realized_pct = (
                round(per_spread / open_row.entry_net_debit * 100, 4)
                if open_row.entry_net_debit else 0.0
            )
            close_reason = _classify_close_reason(final_state.get("exit_reason", ""), pre_close_return_pct)
            db.add(TradeHistory(
                strategy=open_row.strategy,
                underlying=open_row.underlying,
                quantity=open_row.quantity,
                long_strike=open_row.long_strike,
                short_strike=open_row.short_strike,
                entry_net_debit=open_row.entry_net_debit,
                exit_net_value=exit_value,
                realized_pnl_dollars=round(realized_dollars, 2),
                realized_pnl_pct=realized_pct,
                close_reason=close_reason,
                playbook=open_row.playbook,
                opened_at=open_row.opened_at,
                entry_macd_signal=open_row.entry_macd_signal,
                entry_sma_trend=open_row.entry_sma_trend,
                entry_bollinger_zone=open_row.entry_bollinger_zone,
                entry_rsi_zone=open_row.entry_rsi_zone,
            ))
            closed_trade = {
                "strategy": open_row.strategy,
                "macd_signal": open_row.entry_macd_signal,
                "sma_trend": open_row.entry_sma_trend,
                "bollinger_zone": open_row.entry_bollinger_zone,
                "rsi_zone": open_row.entry_rsi_zone,
                "realized_pnl_pct": pre_close_return_pct,
                "close_reason": close_reason,
            }
            db.delete(open_row)
            db.flush()      # release the row before any replacement is inserted
            open_row = None  # anything opened below is a fresh position

    if new_position is not None:
        if open_row is None:
            # Does the account already hold something at these strikes that
            # opposes the entry? See tradier_orders.opposing_leg -- a manual
            # short on the long leg's strike is what got the 2026-09-02 entry
            # rejected, and discovering that in a broker error is the
            # expensive way to learn it.
            _clash = None
            if tradier_orders.LIVE_ORDERS:
                _cp = option_type_for(new_position.strategy)
                _long_is_bought = not is_credit(new_position.strategy)
                for _strike, _want_long in ((new_position.long_strike, _long_is_bought),
                                            (new_position.short_strike, not _long_is_bought)):
                    _clash = tradier_orders.opposing_leg(
                        new_position.underlying, today_expiry(), _cp, _strike, _want_long)
                    if _clash:
                        break
            if _clash:
                logger.error(
                    "ENTRY BLOCKED [%s]: the account already holds %s x%s, which opposes "
                    "this %s %.0f/%.0f. Not sending an order the broker would refuse.",
                    new_position.playbook or new_position.strategy,
                    _clash.get("symbol"), _clash.get("quantity"),
                    new_position.strategy, new_position.long_strike,
                    new_position.short_strike,
                )
                new_position = None
            else:
                _opened = _route_order(
                    new_position, new_position.quantity, opening=True,
                    limit_price=new_position.entry_net_debit,
                    label=new_position.playbook or new_position.strategy)
                if _open_rejected(_opened):
                    # No row. See _open_rejected for the session this cost.
                    logger.error(
                        "ENTRY REJECTED [%s]: %s %.0f/%.0f x%d was refused by the broker — "
                        "no position row written. The engine stays flat.",
                        new_position.playbook or new_position.strategy,
                        new_position.strategy, new_position.long_strike,
                        new_position.short_strike, new_position.quantity,
                    )
                    new_position = None
                else:
                    db.add(OpenPosition(
                        strategy=new_position.strategy,
                        underlying=new_position.underlying,
                        quantity=new_position.quantity,
                        long_strike=new_position.long_strike,
                        short_strike=new_position.short_strike,
                        entry_net_debit=new_position.entry_net_debit,
                        playbook=final_state.get("playbook"),
                        entry_macd_signal=final_state.get("macd_signal"),
                        entry_sma_trend=final_state.get("sma_trend"),
                        entry_bollinger_zone=final_state.get("bollinger_zone"),
                        entry_rsi_zone=final_state.get("rsi_zone"),
                        # None rather than 0 when unsliced, so the column reads as
                        # "no plan" instead of "a plan with nothing left in it".
                        entry_tranche_qty=(final_state.get("entry_tranche_qty") or None),
                        entry_slices_remaining=(final_state.get("entry_slices_remaining") or None),
                    ))
        else:
            # Still the same position — unchanged (HOLD) or added to
            # (BUY_MORE). Updated in place so opened_at and the entry signals
            # aren't reset on every cycle.
            open_row.strategy = new_position.strategy
            open_row.underlying = new_position.underlying
            open_row.quantity = new_position.quantity
            open_row.long_strike = new_position.long_strike
            open_row.short_strike = new_position.short_strike
            open_row.entry_net_debit = new_position.entry_net_debit
            open_row.peak_return_pct = max(
                new_position.peak_return_pct, open_row.peak_return_pct or 0.0
            )
            # A tranche filled this cycle changes three things at once --
            # quantity, blended entry price, and how many slices are left --
            # and the first two are already carried above. Without the third
            # the countdown never decrements, so the same tranche is bought
            # again every cycle until the position is the size of the whole
            # book.
            open_row.entry_slices_remaining = (
                getattr(new_position, "entry_slices_remaining", 0) or None
            )
            open_row.entry_tranche_qty = (
                getattr(new_position, "entry_tranche_qty", 0) or None
            )

    db.add(TradingLog(
        execution_status=final_state.get("execution_status", ""),
        macd_signal=final_state.get("macd_signal", ""),
        sma_trend=final_state.get("sma_trend", ""),
        bollinger_zone=final_state.get("bollinger_zone", ""),
        rsi_zone=final_state.get("rsi_zone", ""),
        market_sentiment=final_state.get("market_sentiment", ""),
        raw_log_payload={k: v for k, v in final_state.items() if k != "messages"},
    ))
    db.commit()

    if closed_trade is not None:
        try:
            await store_trade_setup(
                strategy=closed_trade["strategy"],
                macd_signal=closed_trade["macd_signal"],
                sma_trend=closed_trade["sma_trend"],
                bollinger_zone=closed_trade["bollinger_zone"],
                rsi_zone=closed_trade["rsi_zone"],
                realized_pnl_pct=closed_trade["realized_pnl_pct"],
                close_reason=closed_trade["close_reason"],
                embeddings=VoyageEmbeddings(),
            )
        except Exception:
            # Best-effort — a Voyage hiccup here shouldn't undo an already
            # committed, correctly-closed trade.
            logger.exception("Failed to store trade setup vector for closed trade")

    # Reconcile LAST, against settled state: entries, exits and the database
    # row are all final by here, so a disagreement with the broker is a real
    # disagreement rather than a snapshot taken mid-update.
    _reconcile(db.query(OpenPosition).first())

    return final_state
