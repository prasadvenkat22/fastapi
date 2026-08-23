"""Multi-leg option orders at Tradier — the piece that makes any of this real.

Everything else in this engine decides. This is the only module that acts, and
until it exists every P&L figure in the repository — the sweeps, the live
record, Friday's +$71.39 — describes fills that were assumed rather than
received. `MockBrokerClient.sell_all` deletes a row and returns
"mock_filled"; nothing has ever been sent anywhere.

Three defaults exist because of that, and none should be relaxed casually.

LIVE_ORDERS is false. Importing this module cannot trade. It has to be turned
on deliberately, per environment.

PREVIEW_ONLY is true. Tradier validates an order and returns its cost, margin
requirement and commission without placing it, which is the right way to
learn that a symbol is malformed or an account lacks permission — before a
Wednesday morning rather than during one.

MAX_CONTRACTS caps any single order at a size chosen for a first live session
rather than by the sizing rules. The engine currently sizes to 5 morning and 4
credit contracts on $10k; the first real order should be 1, because the first
live session tests FILLS, not the strategy. We know what the strategy does and
nothing at all about how it fills.

The failure mode this module exists to prevent is a partial fill. A vertical
that fills one leg and not the other leaves a naked short option — unlimited
risk on the call side — and the rest of the engine has no concept of such a
state. `check_fill` reports it explicitly rather than letting it pass as an
ordinary open position.
"""

import logging
import os
from datetime import date, datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://sandbox.tradier.com/v1"
PRODUCTION_BASE = "https://api.tradier.com/v1"

LIVE_ORDERS = os.getenv("TRADING_LIVE_ORDERS", "false").lower() == "true"
PREVIEW_ONLY = os.getenv("TRADING_ORDER_PREVIEW_ONLY", "true").lower() == "true"
MAX_CONTRACTS = int(os.getenv("TRADING_MAX_ORDER_CONTRACTS", "1"))
ORDER_DURATION = os.getenv("TRADING_ORDER_DURATION", "day")


class OrderError(RuntimeError):
    """Raised when Tradier refuses an order or the response is unusable."""


def _order_env() -> str:
    """Where ORDERS go, which is deliberately independent of where quotes come
    from. The useful configuration is production market data with sandbox
    execution: decisions made on real quotes, fills that cost nothing. Sandbox
    quote data is thin enough that testing against it would exercise the
    plumbing on prices the engine would never have traded on."""
    return os.getenv("TRADIER_ORDER_ENV", "sandbox").lower()


def _base() -> str:
    return PRODUCTION_BASE if _order_env() == "production" else SANDBOX_BASE


def _headers() -> dict:
    # Sandbox issues its own token; a production key returns 401 against it,
    # which is verified rather than assumed. Fall back to the production key
    # only when no sandbox key is configured, so a misconfiguration surfaces
    # as an auth error instead of quietly sending orders to the live account.
    if _order_env() == "production":
        key = os.getenv("TRADIER_API_KEY")
        missing = "TRADIER_API_KEY"
    else:
        key = os.getenv("TRADIER_SANDBOX_KEY")
        missing = "TRADIER_SANDBOX_KEY"
    if not key:
        raise OrderError(f"{missing} is not set — cannot reach the {_order_env()} order API.")
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _account() -> str:
    if _order_env() != "production":
        acct = os.getenv("TRADIER_SANDBOX_ACCOUNT_ID") or os.getenv("TRADIER_ACCOUNT_ID")
    else:
        acct = os.getenv("TRADIER_ACCOUNT_ID")
    if not acct:
        raise OrderError("No account id configured for the %s order API." % _order_env())
    return acct


def preflight() -> dict:
    """Everything that must be true before an order can be sent, checked at once.

    Written because the account turned out to be the blocker rather than the
    code: the configured id did not exist for the token, the balances endpoint
    returned 401, and the account is a CASH account, which cannot sell the
    credit spreads the afternoon window is built on.
    """
    out = {"order_env": _order_env(), "live_orders": LIVE_ORDERS,
           "preview_only": PREVIEW_ONLY, "max_contracts": MAX_CONTRACTS}
    try:
        r = httpx.get(f"{_base()}/user/profile", headers=_headers(), timeout=10.0)
        out["profile_status"] = r.status_code
        if r.status_code == 200:
            prof = (r.json() or {}).get("profile", {})
            accts = prof.get("account")
            accts = [accts] if isinstance(accts, dict) else (accts or [])
            out["accounts"] = [
                {"number": a.get("account_number"), "type": a.get("type"),
                 "status": a.get("status")} for a in accts
            ]
            out["configured_account"] = _account()
            out["account_matches"] = any(
                a.get("account_number") == _account() for a in accts)
    except OrderError as e:
        out["error"] = str(e)
        return out
    except httpx.HTTPError as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    bal = account_snapshot()
    out["balances"] = bal if "error" not in bal else {"error": bal}
    if isinstance(bal, dict) and "error" not in bal:
        out["account_type"] = bal.get("account_type")
        out["can_sell_spreads"] = bal.get("account_type") in ("margin", "pdt")
    return out


def occ_symbol(underlying: str, expiry: "date | str", call_put: str, strike: float) -> str:
    """OCC option symbol: QQQ + YYMMDD + C/P + strike x 1000, zero-padded to 8.

    Getting this wrong is the most likely way to send an order for something
    other than the intended contract, so it is a pure function with no
    network in it and is tested directly.
    """
    if isinstance(expiry, str):
        expiry = datetime.strptime(expiry, "%Y-%m-%d").date()
    cp = "C" if call_put.lower().startswith("c") else "P"
    thousandths = int(round(strike * 1000))
    return f"{underlying.upper()}{expiry:%y%m%d}{cp}{thousandths:08d}"


def _post_order(payload: dict, preview: bool) -> dict:
    if not LIVE_ORDERS:
        logger.warning("Order suppressed — TRADING_LIVE_ORDERS is false. Payload: %s", payload)
        return {"status": "suppressed", "payload": payload}

    data = dict(payload)
    if preview:
        data["preview"] = "true"

    url = f"{_base()}/accounts/{_account()}/orders"
    try:
        r = httpx.post(url, data=data, headers=_headers(), timeout=15.0)
    except httpx.HTTPError as e:
        raise OrderError(f"Order request failed: {e}") from e

    if r.status_code >= 400:
        raise OrderError(f"Tradier rejected the order ({r.status_code}): {r.text[:400]}")
    body = (r.json() or {})
    if "errors" in body:
        raise OrderError(f"Tradier returned errors: {body['errors']}")
    result = body.get("order") or body
    logger.info("Order %s: %s", "previewed" if preview else "submitted", result)
    return result


def submit_vertical(underlying: str, expiry: "date | str", call_put: str,
                    long_strike: float, short_strike: float, quantity: int,
                    opening: bool, limit_price: float, is_credit: bool,
                    preview: "bool | None" = None) -> dict:
    """One vertical spread, as a single two-leg order.

    Sent as one multileg order rather than two singles on purpose: legging in
    with separate orders is precisely how a naked short leg happens, and
    Tradier fills a multileg order as a package or not at all.

    `limit_price` is the NET price of the package, positive in both
    directions — Tradier reads the sign from the order type. Never market: a
    two-leg market order in a thin 0DTE chain is how a spread gets filled
    several dollars away from its mid.
    """
    preview = PREVIEW_ONLY if preview is None else preview
    if quantity <= 0:
        raise OrderError("Refusing an order for zero contracts.")
    if quantity > MAX_CONTRACTS:
        logger.warning(
            "Order for %d contracts exceeds the %d cap — clamping. Raise "
            "TRADING_MAX_ORDER_CONTRACTS deliberately, once fills are understood.",
            quantity, MAX_CONTRACTS,
        )
        quantity = MAX_CONTRACTS

    # long_strike is the leg BOUGHT and short_strike the leg SOLD, in both
    # structures -- which is what the engine's own position row means by those
    # names. The first draft hardcoded a credit spread's leg sides for every
    # opening order, which would have sent the morning debit spread inside
    # out: selling the in-the-money leg and buying the out-of-the-money one is
    # a different trade at a different price, and it would have been filled.
    long_sym = occ_symbol(underlying, expiry, call_put, long_strike)
    short_sym = occ_symbol(underlying, expiry, call_put, short_strike)

    if opening:
        long_side, short_side = "buy_to_open", "sell_to_open"
    else:
        long_side, short_side = "sell_to_close", "buy_to_close"

    # The NET direction of money: opening a credit spread collects, closing it
    # pays, and a debit spread is the mirror of both.
    collecting = is_credit if opening else not is_credit

    payload = {
        "class": "multileg",
        "symbol": underlying.upper(),
        "type": "credit" if collecting else "debit",
        "duration": ORDER_DURATION,
        "price": f"{abs(limit_price):.2f}",
        "option_symbol[0]": long_sym, "side[0]": long_side, "quantity[0]": str(quantity),
        "option_symbol[1]": short_sym, "side[1]": short_side, "quantity[1]": str(quantity),
    }
    return _post_order(payload, preview)


def order_status(order_id: str) -> dict:
    if not LIVE_ORDERS:
        return {"status": "suppressed"}
    url = f"{_base()}/accounts/{_account()}/orders/{order_id}"
    r = httpx.get(url, headers=_headers(), timeout=10.0)
    if r.status_code >= 400:
        raise OrderError(f"Could not read order {order_id}: {r.status_code}")
    return (r.json() or {}).get("order", {})


def open_positions() -> list:
    """What the BROKER thinks is open, which is the only authority that counts.

    The engine's own row is a belief. Reconciling the two every cycle is what
    turns a silent divergence -- an order that did not fill, a leg that did --
    into something visible.
    """
    if not LIVE_ORDERS:
        return []
    url = f"{_base()}/accounts/{_account()}/positions"
    r = httpx.get(url, headers=_headers(), timeout=10.0)
    if r.status_code >= 400:
        raise OrderError(f"Could not read positions: {r.status_code}")
    pos = ((r.json() or {}).get("positions") or {})
    if pos in ("null", None, ""):
        return []
    rows = pos.get("position")
    if isinstance(rows, dict):
        rows = [rows]
    return rows or []


def check_fill(short_symbol: str, long_symbol: str) -> dict:
    """Did BOTH legs arrive?

    A vertical holding one leg is a naked option, and no other part of this
    engine models that state. Reported loudly rather than folded into an
    ordinary "position is open".
    """
    held = {p.get("symbol"): int(p.get("quantity", 0)) for p in open_positions()}
    short_qty, long_qty = held.get(short_symbol, 0), held.get(long_symbol, 0)
    complete = short_qty != 0 and long_qty != 0 and abs(short_qty) == abs(long_qty)
    if not complete and (short_qty or long_qty):
        logger.error(
            "NAKED LEG: %s qty %s against %s qty %s. One leg of a vertical is "
            "unhedged; this is not a state the engine models.",
            short_symbol, short_qty, long_symbol, long_qty,
        )
    return {"complete": complete, "short_qty": short_qty, "long_qty": long_qty,
            "naked": bool((short_qty or long_qty) and not complete)}


def account_snapshot() -> dict:
    """Balances and permissions, for the pre-flight check."""
    url = f"{_base()}/accounts/{_account()}/balances"
    r = httpx.get(url, headers=_headers(), timeout=10.0)
    if r.status_code >= 400:
        return {"error": r.status_code, "detail": r.text[:200]}
    return (r.json() or {}).get("balances", {})
