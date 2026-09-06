"""Black-Scholes greeks for a two-leg vertical, and the net position greeks.

WHY THE NET MATTERS AND THE LEG GREEKS DO NOT. weekly_shadow has recorded
short_delta since it was written, which describes one contract. It does not
describe the position. Measured on the live SNDK 1600/1700 chain, 2026-09-06:

    1600C  delta 0.823  gamma 0.00156  theta -3.710/day  vega +0.569
    1700C  delta 0.613  gamma 0.00225  theta -5.630/day  vega +0.839

    NET    delta +0.211  gamma -0.0007  theta +1.920/day  vega -0.271

Both legs bleed theta and both are long vega; the SPREAD earns theta and is
short vega, and neither fact is visible from either leg alone. For a deep-ITM
debit spread that is the whole trade: the position cost 67.10 against 100.00
of intrinsic, so its extrinsic is -32.90 and its maximum profit is +32.90 --
THE SAME NUMBER. Max profit IS the negative extrinsic, and theta and vega are
the only two engines that collect it. At +1.92 a day, four sessions of decay
recover 7.68 of the 32.90; the rest needs the spread to stay above the short
strike, which is delta's job, and delta is only +0.21.

An IV crush therefore PAYS this structure: net vega -0.271 means ten points of
IV collapse is +2.71 a share. That is the one claim in the outside note that
survived checking, and it survives because the short leg sits nearer the money
and so nearer peak vega than the long one.

Sign convention: greeks are per share of a LONG position in the named leg.
Net = long leg minus short leg. Theta is per calendar day, vega per one
volatility POINT (not per 1.00 of vol).
"""

from __future__ import annotations

import math
from typing import Optional


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def leg_greeks(spot: float, strike: float, years: float, iv: float,
               call: bool = True) -> dict:
    """Per-share greeks for one option. Degenerates safely at expiry."""
    if not (spot > 0 and strike > 0) or years <= 0 or iv <= 0:
        itm = (spot > strike) if call else (spot < strike)
        return {"delta": (1.0 if itm else 0.0) * (1 if call else -1),
                "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    vt = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / vt
    delta = _cdf(d1) if call else _cdf(d1) - 1.0
    return {
        "delta": delta,
        "gamma": _pdf(d1) / (spot * vt),
        # Driftless: the rate term is a rounding error inside a week and the
        # rest of this engine already prices at zero rate (chain_pricer).
        "theta": (-spot * _pdf(d1) * iv / (2.0 * math.sqrt(years))) / 365.0,
        "vega": spot * _pdf(d1) * math.sqrt(years) / 100.0,
    }


def spread_greeks(spot: float, long_strike: Optional[float],
                  short_strike: Optional[float], years: float,
                  long_iv: Optional[float], short_iv: Optional[float],
                  call: bool = True) -> dict:
    """Net greeks for long `long_strike` / short `short_strike`.

    Returns {} when a leg or its vol is missing, so the caller writes nulls
    rather than a confidently wrong zero -- a null says "not measured", a
    zero says "measured and flat", and they are not the same claim.
    """
    if not spot or long_strike is None or short_strike is None:
        return {}
    if not long_iv or not short_iv or years is None or years <= 0:
        return {}
    lg = leg_greeks(spot, float(long_strike), years, float(long_iv), call)
    sg = leg_greeks(spot, float(short_strike), years, float(short_iv), call)
    return {
        "sig_net_delta": round(lg["delta"] - sg["delta"], 5),
        "sig_net_gamma": round(lg["gamma"] - sg["gamma"], 7),
        "sig_net_theta": round(lg["theta"] - sg["theta"], 5),
        "sig_net_vega": round(lg["vega"] - sg["vega"], 5),
    }
