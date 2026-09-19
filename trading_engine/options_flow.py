"""Unusual options activity, read from the chain the rotation already fetches.

Built 2026-09-19 (section 197). On Friday 09-18 about $90 million of
short-dated call blocks printed in Sandisk, Micron and Marvell at 10:39 ET.
No feed the engine read carried it; the tape gate saw the footprint on two
of the three names. But the block itself was never hidden: it sat in the
option chain as volume many times the open interest on specific strikes,
within minutes of the print, in data the rotation pulls for every candidate
anyway. This reads that.

WHAT IT MEASURES, per underlying, over the expiries it is given (the traded
one plus the next weekly by default):

    call_vol / put_vol       which side the day's paper is on
    turnover = vol / OI      whether today's volume is NEW positioning (>1
                             means more traded today than was open) or the
                             churn of an existing book
    top contracts            the strikes doing it, with vol/OI and notional

BIAS. 'bullish' when calls carry at least CP_RATIO times the put volume, the
call side turns over at least TURNOVER of its open interest and there are at
least MIN_VOLUME call contracts; 'bearish' is the mirror; otherwise
'neutral'. Every threshold is a knob and none is measured -- section 119.

MODE. record (default): log one OPTIONS line per candidate, refuse nothing.
veto: refuse a call debit on a bearish read and a put on a bullish one; a
neutral read passes. off: do not fetch the extra expiry.

WHAT IT IS NOT. Volume is two-sided: a call block is a buyer AND a seller,
and open interest only settles overnight, so "bought" here means the paper
traded in size at all. Call volume with the underlying ABOVE the strike
being sold to open (covered calls) reads exactly like call buying. The tape
gate beside it is the check on that: paper that is being bought moves the
underlying.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

MODE = os.getenv("TRADING_DTE0_OPTIONS_FLOW", "record").strip().lower()
CP_RATIO = float(os.getenv("TRADING_OPTFLOW_CP_RATIO", "2.0"))
TURNOVER = float(os.getenv("TRADING_OPTFLOW_TURNOVER", "0.5"))
MIN_VOLUME = int(os.getenv("TRADING_OPTFLOW_MIN_VOLUME", "500"))
TOP_N = 3

_CACHE: dict = {}
_TTL_S = 120.0


def _g(row, name, default=0.0):
    v = row.get(name) if isinstance(row, dict) else getattr(row, name, None)
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def flow_from_chain(chain, spot: "float | None" = None) -> "dict | None":
    """The readings from one or more chains (dicts keyed by (type, strike)).

    `chain` may be a single chain dict or a list of them; volumes and open
    interest are summed across expiries. Pure, so it can be tested.
    """
    chains = chain if isinstance(chain, list) else [chain]
    rows = [r for c in chains if c for r in c.values()]
    if not rows:
        return None
    cvol = pvol = coi = poi = cnot = pnot = 0.0
    top = []
    for r in rows:
        typ = (r.get("option_type") if isinstance(r, dict) else getattr(r, "option_type", "")) or ""
        vol, oi = _g(r, "volume"), _g(r, "open_interest")
        mid = (_g(r, "bid") + _g(r, "ask")) / 2.0
        notional = vol * mid * 100.0
        strike = _g(r, "strike")
        if typ.lower().startswith("c"):
            cvol += vol; coi += oi; cnot += notional
        elif typ.lower().startswith("p"):
            pvol += vol; poi += oi; pnot += notional
        else:
            continue
        # The top list is for the strikes doing the work, so it skips what a
        # real chain carries: dust strikes 80% away with an open interest of
        # one (MU's Friday chain listed P190 at 11,668 volume against 1 OI).
        # The aggregates above still count everything.
        near = spot is None or abs(strike / spot - 1.0) <= 0.30
        if vol >= max(MIN_VOLUME // 5, 50) and oi >= 10 and near:
            top.append({"type": typ[0].upper(), "strike": strike, "vol": int(vol), "oi": int(oi),
                        "turnover": round(vol / oi, 2) if oi > 0 else None,
                        "notional": round(notional),
                        "moneyness_pct": (round((strike / spot - 1.0) * 100.0, 2)
                                          if spot else None)})
    top.sort(key=lambda t: (-(t["turnover"] or 0.0), -t["vol"]))
    return {
        "call_vol": int(cvol), "put_vol": int(pvol),
        "call_oi": int(coi), "put_oi": int(poi),
        "cp_ratio": round(cvol / pvol, 2) if pvol > 0 else (99.0 if cvol > 0 else 0.0),
        "call_turnover": round(cvol / coi, 2) if coi > 0 else None,
        "put_turnover": round(pvol / poi, 2) if poi > 0 else None,
        "call_notional": round(cnot), "put_notional": round(pnot),
        "top": top[:TOP_N], "spot": spot,
    }


def bias(flow: "dict | None") -> "tuple[str, str]":
    """('bullish' | 'bearish' | 'neutral', why)."""
    if not flow:
        return "neutral", "no chain read"
    cv, pv = flow["call_vol"], flow["put_vol"]
    ct, pt = flow.get("call_turnover") or 0.0, flow.get("put_turnover") or 0.0
    if cv >= MIN_VOLUME and cv >= CP_RATIO * max(pv, 1) and ct >= TURNOVER:
        return "bullish", ("calls %d vs puts %d (%.1fx), call turnover %.2f of OI"
                           % (cv, pv, cv / max(pv, 1), ct))
    if pv >= MIN_VOLUME and pv >= CP_RATIO * max(cv, 1) and pt >= TURNOVER:
        return "bearish", ("puts %d vs calls %d (%.1fx), put turnover %.2f of OI"
                           % (pv, cv, pv / max(cv, 1), pt))
    return "neutral", ("calls %d / puts %d, turnover c %.2f p %.2f" % (cv, pv, ct, pt))


def gate(direction: str, flow: "dict | None") -> "tuple[bool, str]":
    """(allowed, why) for a 'bullish' (call debit) or 'bearish' (put debit) entry."""
    b, why = bias(flow)
    if b == "neutral":
        return True, "options flow neutral: " + why
    if (direction == "bullish" and b == "bearish") or (direction == "bearish" and b == "bullish"):
        return False, "options flow reads %s: %s" % (b, why)
    return True, "options flow agrees (%s): %s" % (b, why)


def read(symbol: str, expiries: list, spot: "float | None" = None) -> "dict | None":
    """Chains for `expiries`, summed. Cached two minutes per (symbol, expiries)."""
    key = (symbol.upper(), tuple(expiries))
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL_S:
        return hit[1]
    out = None
    try:
        from .data_feed import fetch_option_chain
        chains = [fetch_option_chain(e, symbol.upper()) for e in expiries]
        out = flow_from_chain([c for c in chains if c], spot)
    except Exception:
        logger.warning("OPTIONS %s: chain unreadable.", symbol, exc_info=True)
        out = None
    _CACHE[key] = (time.time(), out)
    return out


def describe(flow: "dict | None") -> str:
    if not flow:
        return "OPTIONS n/a"
    b, _ = bias(flow)
    tops = "; ".join("%s%g vol %d/oi %d%s%s" % (
        t["type"], t["strike"], t["vol"], t["oi"],
        (" x%.1f" % t["turnover"]) if t["turnover"] is not None else "",
        (" %+.1f%%" % t["moneyness_pct"]) if t["moneyness_pct"] is not None else "")
        for t in flow["top"])
    return ("OPTIONS %s: calls %d puts %d (%.1fx), turnover c %s p %s, notional c $%dk p $%dk"
            "%s" % (b.upper(), flow["call_vol"], flow["put_vol"], flow["cp_ratio"],
                    flow["call_turnover"], flow["put_turnover"],
                    flow["call_notional"] // 1000, flow["put_notional"] // 1000,
                    (" | " + tops) if tops else ""))
