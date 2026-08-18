"""Named, time-windowed entry strategies.

The engine used to run one structure — long ITM / short ATM — at every hour
of the session. That has a specific cost: a debit spread can never be worth
more than its width, so where the strikes sit relative to spot decides how
much of a move you actually capture. Measured on a $3-wide spread with QQQ
up $9 by expiry:

    deep ITM   +5%      ITM   +58%      ATM   +173%      OTM   +556%

A $9 day and a $3 day pay an ITM spread the same 58%. Widening the spread
does not help — a $9-wide ITM spread costs proportionally more and returns
18%. Capturing a bigger move means moving the strikes toward the money, not
making them wider. The tradeoff is real: ITM wins often and small, ATM wins
less often and large.

So structure is chosen by time of day, on the reasoning that the market's
behaviour differs by hour: the morning produces directional moves worth
paying for leverage on, and the midday lull produces chop where positive
theta and a high hit rate matter more.

Every window carries a NAME, recorded on the position and carried through to
TradeHistory, so performance can be attributed per strategy rather than
lumped together. Pruning is deliberately trivial: delete a window from
WINDOWS and the engine stops trading it.
"""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# How the long leg sits relative to the ATM short strike.
ITM = "ITM"    # long leg in the money   — high win rate, capped upside
ATM = "ATM"    # long leg at the money   — lower win rate, much larger upside


@dataclass(frozen=True)
class PlaybookWindow:
    name: str
    start: time
    end: time
    placement: str      # ITM or ATM
    width: float        # distance between the strikes, in dollars
    take_profit_pct: float  # per-strategy, because the structures have very
                            # different ceilings — see the note below
    note: str


# Ordered, non-overlapping. Times are ET.
#
# Deleting an entry here removes that strategy entirely — that is the intended
# way to retire one that is not earning its place.
WINDOWS = (
    PlaybookWindow(
        name="ATM_MOMENTUM",
        start=time(9, 45), end=time(10, 15), placement=ATM, width=3.0,
        take_profit_pct=60.0,
        note="Opening range resolved. Pay for leverage while a real directional "
             "leg is most likely; this is the window that can capture a big day. "
             "Takes profit at 60%, not 30%: an ATM spread entered near $1.14 "
             "maxes out around +163%, so a 30% target would book the small win "
             "and throw away the entire reason for choosing this structure.",
    ),
    PlaybookWindow(
        name="MORNING_DRIFT",
        start=time(10, 15), end=time(11, 30), placement=ITM, width=3.0,
        take_profit_pct=30.0,
        note="No clear regime — the momentum leg is spent and the midday range "
             "has not formed. Conservative structure; a candidate for removal "
             "if it does not earn its place.",
    ),
    PlaybookWindow(
        name="ITM_GRINDER",
        start=time(11, 30), end=time(13, 30), placement=ITM, width=3.0,
        take_profit_pct=30.0,
        note="Midday lull. Volume dries up and QQQ tends to consolidate, so a "
             "positive-theta structure that also pays when price sits still.",
    ),
    PlaybookWindow(
        name="AFTERNOON_ITM",
        start=time(13, 30), end=time(14, 0), placement=ITM, width=3.0,
        take_profit_pct=30.0,
        note="Theta is accelerating but a debit spread wants time to work. The "
             "playbook calls for an OTM credit spread here instead; until that "
             "exists this is the conservative stand-in.",
    ),
)


def window_for(now: Optional[datetime] = None) -> Optional[PlaybookWindow]:
    """The window covering `now`, or None outside all of them.

    None means no entry — it covers the opening warmup, anything past the
    final window's end, and any gap left by removing a window.
    """
    now = now or datetime.now(NY)
    t = now.time()
    for w in WINDOWS:
        if w.start <= t < w.end:
            return w
    return None


def strikes_for(window: PlaybookWindow, atm_strike: float, bullish: bool) -> tuple[float, float]:
    """(long_strike, short_strike) for this window's placement.

    A bull call spread is long the lower strike and short the higher one; a
    bear put spread is the mirror. ITM placement puts the long leg in the
    money and the short leg at the money. ATM placement puts the long leg at
    the money and pushes the short leg out — cheaper, and it needs the move.
    """
    w = window.width
    if window.placement == ATM:
        return (atm_strike, atm_strike + w) if bullish else (atm_strike, atm_strike - w)
    return (atm_strike - w, atm_strike) if bullish else (atm_strike + w, atm_strike)


def take_profit_for(playbook_name: str, default: float) -> float:
    """The take-profit target for the strategy that OPENED a position.

    Looked up by name rather than by the current clock: a position opened in
    ATM_MOMENTUM is still an ATM structure at 13:00, and judging it against
    the ITM target that happens to be in force then would book it early for
    no reason. `default` covers positions opened before per-strategy targets
    existed, and any whose window has since been retired.
    """
    # Names carry a tier suffix ("ITM_GRINDER:RELAXED") so the scoreboard can
    # split them; the exit target belongs to the window, not the tier.
    base = (playbook_name or "").split(":", 1)[0]
    for w in WINDOWS:
        if w.name == base:
            return w.take_profit_pct
    return default
