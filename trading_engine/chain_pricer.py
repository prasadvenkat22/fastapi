"""Black-Scholes vertical pricing, calibrated against the real option chain.

Why this exists
---------------
`broker.estimate_spread_value` is not an option pricer. It values a vertical
as `width * N(distance past the midpoint / sigma)` — a probability that the
spread finishes in the money, scaled by its width. That is a reasonable
approximation near the money and it falls apart in the tail, because the
true value of a far-OTM vertical is the difference of two small option
prices and decays far faster than a normal CDF of the midpoint does.

Measured against 1,944 marks the live engine logged from the Tradier chain
(sessions 2026-08-21 and 2026-08-24, `trading_logs.raw_log_payload ->
shadow_condor`), the error is a clean monotone function of moneyness:

    mean |delta|   market   estimate_spread_value   ratio
      0.00-0.05     0.012           0.229           18.6x
      0.05-0.10     0.052           0.595           11.4x
      0.10-0.15     0.283           1.059            3.7x
      0.15-0.20     0.678           1.266            1.9x
      0.20-0.25     0.848           1.381            1.6x
      0.30-0.35     1.139           1.383            1.2x
      0.35-0.40     1.286           1.530            1.2x

Near the money it is 20% rich; at a 0.15-delta short strike — exactly where
an iron condor or a far-OTM credit spread lives — it is off by 4x to 18x.
Every credit-side sweep in scripts/sweep.py was decided on those numbers.

What this module does instead
-----------------------------
Prices each leg with Black-Scholes and subtracts, using an implied vol read
off a fitted smile rather than a single flat number. Same 1,944 marks:

    mean absolute error   old 0.4660   new 0.0836   (82% better)
    ratio to market       old 1.2x-18.6x   new 1.0x-1.6x

Two calibration details that are easy to get wrong, and did get wrong once:

1.  TIME IS CALENDAR TIME, not trading time. Tradier quotes its greeks and
    IVs against a 365-day year. Pricing with the engine's 390-minute
    trading day against Tradier's IVs inflates every value by roughly 2.3x
    in sigma-root-t and made the first version of this module no better
    than the model it replaces. The tell was that a completely different
    pricing method landed within a few percent of the old one in every
    bucket.

2.  THE SMILE IS SHALLOW AND TILTED. Fitted quadratic in standardised
    log-moneyness x = ln(K/S) / sqrt(T), R^2 = 0.716 over 2,156 short-leg
    IV observations: puts bid up, calls bid down, which is the usual equity
    skew and is not something a flat vol can express.
"""
from __future__ import annotations

import math
import os

# Calendar minutes in a year. See note (1) above — this is the single most
# important constant here.
YEAR_MINUTES = 365.0 * 24.0 * 60.0

# Fitted smile: iv = A*x^2 + B*x + C, with x = ln(K/S)/sqrt(T).
#
# Fitted on 2,156 short-leg IV observations logged on 2026-08-24, a session
# that closed with VIX at 15.7. C is therefore the AT-THE-MONEY vol for a
# quiet day; SMILE_VIX_REFERENCE below is what scales it to other days.
SMILE_A = float(os.getenv("TRADING_SMILE_A", "0.1383"))
SMILE_B = float(os.getenv("TRADING_SMILE_B", "-0.0801"))
SMILE_C = float(os.getenv("TRADING_SMILE_C", "0.1867"))

# The VIX level the smile above was fitted at. QQQ's own at-the-money vol ran
# 0.187 against a VIX of 15.7 that session, a ratio of about 1.19 — QQQ is
# more volatile than the S&P, which is what that premium is.
#
# THIS IS THE WEAKEST LINK IN THE MODULE and it is deliberately isolated on
# one line. The smile was fitted on a SINGLE low-volatility session, so
# scaling it linearly by VIX is an assumption, not a measurement: it has one
# anchor point and no second one to draw a line through. It will be wrong in
# a way nobody can bound until the shadow log has covered a high-VIX day.
# Until then, treat the LEVEL of any credit result from this module as
# provisional and the RANKING between variants as the usable output.
SMILE_VIX_REFERENCE = float(os.getenv("TRADING_SMILE_VIX_REF", "15.7"))


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def implied_vol(strike: float, spot: float, years: float, vix: float | None = None) -> float:
    """Implied vol for one strike, off the fitted smile.

    `vix` scales the whole surface: pass the session's VIX to price a jumpy
    day differently from a quiet one, or None to price every day like the
    quiet one the smile was fitted on.
    """
    if years <= 0 or spot <= 0 or strike <= 0:
        return SMILE_C
    x = math.log(strike / spot) / math.sqrt(years)
    iv = SMILE_A * x * x + SMILE_B * x + SMILE_C
    if vix and vix > 0:
        iv *= vix / SMILE_VIX_REFERENCE
    # A floor, not a fudge: the quadratic can go negative far enough out,
    # where it is extrapolating past every strike it ever saw.
    return max(iv, 0.03)


def black_scholes(spot: float, strike: float, years: float, iv: float, call: bool = True) -> float:
    """Zero rate, zero dividend. Both are rounding errors inside one session."""
    if years <= 0 or iv <= 0:
        return max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    vt = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / vt
    d2 = d1 - vt
    value = spot * _normal_cdf(d1) - strike * _normal_cdf(d2)
    return value if call else value - spot + strike


def vertical_value(spot: float, minutes_left: float, near_strike: float,
                   far_strike: float, call: bool, vix: float | None = None) -> float:
    """Value of a vertical spread, long the near strike and short the far one.

    Sign convention matches how the engine already thinks about these: this
    is what it costs to BUY the spread outright, which is also what it costs
    to buy BACK a credit spread sold at the same two strikes.
    """
    years = max(minutes_left, 0.0) / YEAR_MINUTES
    near = black_scholes(spot, near_strike, years, implied_vol(near_strike, spot, years, vix), call)
    far = black_scholes(spot, far_strike, years, implied_vol(far_strike, spot, years, vix), call)
    return max(near - far, 0.0)


def credit_value(spot: float, minutes_left: float, short_strike: float,
                 long_strike: float, call: bool, vix: float | None = None) -> float:
    """Cost to close a credit vertical — the drop-in replacement for
    `broker.estimate_credit_value`, which is the call site the measured
    error above actually matters at."""
    return vertical_value(spot, minutes_left, short_strike, long_strike, call, vix)


def condor_value(spot: float, minutes_left: float, call_short: float, put_short: float,
                 width: float, vix: float | None = None) -> float:
    """Cost to close both sides of an iron condor."""
    return (credit_value(spot, minutes_left, call_short, call_short + width, True, vix)
            + credit_value(spot, minutes_left, put_short, put_short - width, False, vix))
