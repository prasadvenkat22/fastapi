"""Operator-tunable trading settings, changed without touching .env.production.

WHY A SECOND FILE. Every knob in this engine is an environment variable read
once at import (orphans.py, nodes.py, scripts/dte0_trade.py). Changing one used
to mean editing .env.production on the droplet and recreating the `app`
container -- a minute of missed cron cycles, and a hand edit of a file that
also holds the database password and API keys.

cron starts a FRESH Python process every minute (scripts/install_cron.sh), and
every one of those imports this package before it reads a single knob. So a
small file of overrides, loaded into os.environ from trading_engine/__init__,
is picked up by the next cycle with no restart. The `app` bind-mounts the repo
at /app, so the file the API writes is the file cron reads.

PRECEDENCE: trading_overrides.env  >  .env.production  >  the code default.
Delete a line (or `settings.py unset KEY`) and the .env.production value is
back in force on the next cycle.

WHITELIST ONLY. Only keys in REGISTRY can be written, each validated against
its type and bounds. The file is loaded into the environment of processes
that hold broker credentials; it must never become a way to set arbitrary
variables (TRADIER_*, DATABASE_URL, ...). read_file() enforces the same whitelist,
so a hand-edited line for any other key is ignored and reported.

WHAT DOES NOT PICK IT UP LIVE: the uvicorn process read its constants at
startup, so the rule columns on /trading/positions show the values from the
last `app` recreate. The trading decisions themselves are cron's, and cron
sees the change on its next minute.

The on/off switches that decide whether the engine trades at all
(TRADING_DTE0_LIVE, TRADING_MANAGE_ORPHANS) are deliberately NOT tunable here.
The kill switch is the runtime off; going live stays a .env.production edit.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Literal, Optional

Kind = Literal["float", "int", "bool", "time"]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVERRIDES_PATH = os.getenv("TRADING_OVERRIDES_PATH",
                           os.path.join(_REPO_ROOT, "trading_overrides.env"))
AUDIT_PATH = os.getenv("TRADING_OVERRIDES_AUDIT",
                       os.path.join(_REPO_ROOT, "trading_overrides.log"))

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    kind: Kind
    default: str               # the code default when neither file sets it
    help: str
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    allow_blank: bool = False  # "" is valid: "disabled" on a time, "follow the other knob" on a number
    # SECTION 261: the settings page's book cards. book is qqq / s0 / w3 / w7 /
    # global; order sorts the card (multiples of 10 are the core rows every
    # book card shares, in the same order; 41-49 are the QQQ band details).
    # 0 = an Advanced row.
    book: str = ""
    order: int = 0


G_ENGINE = "QQQ engine exits (the engine's own trades)"
G_0DTE = "0DTE exits"
G_WEEKLY = "Weekly shared defaults (3-day and 7-day)"
G_W3 = "3-day spreads (bought 2-4 days before expiry)"
G_W7 = "7-day spreads (bought 5+ days before expiry)"
G_ACCOUNT = "Account"
G_ENTRY = "Entries & budgets"
G_BUCKETS = "Trade buckets (new entries on/off)"
G_STRUCT = "Entry structure gates (all buckets)"
G_PICK0 = "0DTE stocks: strike selection"

REGISTRY: tuple[Setting, ...] = (
    # --- Buckets: new entries only; open positions are always managed --------
    # OFF BY DEFAULT (operator, 2026-09-24): nothing opens a new trade until a
    # bucket is switched on in /desk/settings (or `tset set ...=true`).
    Setting("TRADING_BUCKET_QQQ_0DTE", "QQQ 0DTE (engine)", G_BUCKETS, "bool", "false",
            "The engine's QQQ same-day entries: 1-minute Bollinger band touches. Off: no new "
            "entries; an open position is still managed."),
    Setting("TRADING_POSITION_BUDGET", "QQQ 0DTE budget", G_BUCKETS, "float", "1000",
            "Capital the engine sizes a QQQ same-day entry against (realised equity, daily-loss "
            "limit and position risk are all scaled from it).", "$", 0, 1_000_000),
    Setting("TRADING_MIN_ONE_CONTRACT", "QQQ engine: at least 1 contract", G_BUCKETS, "bool", "false",
            "The engine's caps are fractions of its budget (entry 20%, tail 15%, a 6% daily-loss limit), so on a "
            "small budget they round every entry to 0. On: buy 1 contract when one fits the budget and buying "
            "power. One stop-out can exceed the daily-loss limit and halt the day."),
    Setting("TRADING_MAX_ENTRIES_QQQ_0DTE", "QQQ 0DTE: entries per day", G_BUCKETS, "int", "0",
            "Most entries the engine makes in one session. 0 = no cap.", "", 0, 50),
    Setting("TRADING_BAND_TOUCH_START", "Band touch: from", G_BUCKETS, "time", "09:45", "First entry time (ET)."),
    Setting("TRADING_BAND_TOUCH_END", "Band touch: until", G_BUCKETS, "time", "15:00", "Last entry time (ET). Entries after 14:00 are allowed only here."),
    Setting("TRADING_BAND_TOUCH_WIDTH", "Band touch: spread width", G_BUCKETS, "float", "4",
            "Strike width of the in-the-money debit spread.", "$", 1, 10),
    Setting("TRADING_BAND_TOUCH_SD", "Band touch: band width (SD)", G_BUCKETS, "float", "2",
            "Standard deviations from the 20-SMA.", "sd", 1, 4),
    Setting("TRADING_BAND_TOUCH_PERIOD", "Band touch: SMA period", G_BUCKETS, "int", "20",
            "Number of 1-minute bars in the moving average.", "bars", 5, 100),
    Setting("TRADING_BUCKET_STOCK_0DTE", "Single-stock 0DTE (rotation)", G_BUCKETS, "bool", "false",
            "dte0_trade.py same-day entries on single names. Off: screens and logs only."),
    Setting("TRADING_DTE0_MAX_BUDGET", "Single-stock 0DTE budget", G_BUCKETS, "float", "1500",
            "Total the same-day rotation commits per run, split across its max trades.",
            "$", 0, 1_000_000),
    Setting("TRADING_MAX_TRADES_STOCK_0DTE", "Trades per run", G_BUCKETS, "int", "",
            "Most new spreads one 5-minute run may open. Blank = the cron's --max-trades (4).",
            "", 1, 20, allow_blank=True),
    Setting("TRADING_MAX_TRADES_STOCK_W3", "Trades per run", G_BUCKETS, "int", "",
            "Most new spreads one 3-day run (09:50 / 13:50) may open. Blank = the cron's --max-trades (3).",
            "", 1, 20, allow_blank=True),
    Setting("TRADING_MAX_TRADES_STOCK_W7", "Trades per run", G_BUCKETS, "int", "",
            "Most new spreads one 7-day run (09:50 / 13:50) may open. Blank = the cron's --max-trades (3).",
            "", 1, 20, allow_blank=True),
    Setting("TRADING_MAX_ENTRIES_STOCK_0DTE", "Single-stock 0DTE: entries per day", G_BUCKETS, "int", "0",
            "Most new spreads this bucket opens in one session (manual spreads on its names count). "
            "0 = no cap; --max-trades still limits each run.", "", 0, 50),
    # Section 245: the weekly book is two buckets. A run's type is the expiry it
    # buys (5+ days out = 7-day); a position keeps the type it was bought as.
    Setting("TRADING_BUCKET_STOCK_W3", "Single-stock 3-day (rotation)", G_BUCKETS, "bool", "false",
            "Weekly-book runs buying an expiry 2-4 days out (the Mon-Wed schedule). Off: screens and logs only."),
    Setting("TRADING_W3_MAX_BUDGET", "Single-stock 3-day budget", G_BUCKETS, "float", "",
            "Cap on the 3-day bucket (minus its open spreads, never above buying power). "
            "Blank = the old shared weekly budget.", "$", 0, 1_000_000, allow_blank=True),
    Setting("TRADING_MAX_ENTRIES_STOCK_W3", "Single-stock 3-day: entries per day", G_BUCKETS, "int", "0",
            "Most new spreads this bucket opens in one session (manual spreads on its names count). "
            "0 = no cap; --max-trades still limits each run.", "", 0, 50),
    Setting("TRADING_BUCKET_STOCK_W7", "Single-stock 7-day (rotation)", G_BUCKETS, "bool", "false",
            "Weekly-book runs buying an expiry 5+ days out (the Thu-Fri schedule). Off: screens and logs only."),
    Setting("TRADING_W7_MAX_BUDGET", "Single-stock 7-day budget", G_BUCKETS, "float", "",
            "Cap on the 7-day bucket (minus its open spreads, never above buying power). "
            "Blank = the old shared weekly budget.", "$", 0, 1_000_000, allow_blank=True),
    Setting("TRADING_MAX_ENTRIES_STOCK_W7", "Single-stock 7-day: entries per day", G_BUCKETS, "int", "0",
            "Most new spreads this bucket opens in one session (manual spreads on its names count). "
            "0 = no cap; --max-trades still limits each run.", "", 0, 50),
    # --- Section 241: where in the range / which side of the midline -------
    Setting("TRADING_WEEKRANGE_GUARD", "Week-range guard", G_STRUCT, "bool", "false",
            "Single-stock books: no call spread (bullish) near the week's high, no put spread "
            "(bearish) near its low. Range = last 5 sessions. Measured: at the top 10% the next "
            "4 days were up 34% of the time vs 53% mid-range."),
    Setting("TRADING_WEEKRANGE_CALL_MAX", "No calls above (share of week range)", G_STRUCT, "float", "0.90",
            "Bullish entries refused at or above this position in the week's range.", "x range", 0.5, 1),
    Setting("TRADING_WEEKRANGE_PUT_MIN", "No puts below (share of week range)", G_STRUCT, "float", "0.10",
            "Bearish entries refused at or below this position in the week's range.", "x range", 0, 0.5),
    Setting("TRADING_PULLBACK_GATE_WEEKLY", "Weekly: wait for the hourly pullback", G_STRUCT, "bool", "false",
            "Weekly book: a call needs the last hourly close BELOW its 20-SMA, a put ABOVE it. "
            "Measured: below the midline, up 4 days later 60% vs 42%."),
    Setting("TRADING_PULLBACK_GATE_0DTE", "0DTE stocks: wait for the 5-min pullback", G_STRUCT, "bool", "false",
            "Single-stock 0DTE: a call needs price BELOW the 5-min 20-SMA, a put ABOVE it. Measured "
            "weak (47% vs 43% up by the close)."),
    Setting("TRADING_MACRO_BAD_PUTS_ONLY", "Macro BAD: put spreads only (stocks)", G_STRUCT, "bool", "false",
            "Single-stock books refuse bullish spreads while the engine's macro verdict is BAD. NOT "
            "supported by the measurement (macro gates showed no direction)."),
    # Section 261: the band picks the side for each stock book.
    Setting("TRADING_BOLLINGER_GATE_DTE0", "Single-stock 0DTE: Bollinger band", G_STRUCT, "bool", "false",
            "On: only trade a name outside its 5-minute 20-period 2-SD band -- at/below the lower band "
            "call spreads only, at/above the upper band put spreads only, inside the band no trade."),
    Setting("TRADING_BOLLINGER_GATE_W3", "3-day: Bollinger band", G_STRUCT, "bool", "false",
            "On: only trade a name outside its HOURLY 20-period 2-SD band -- at/below the lower band "
            "call spreads only, at/above the upper band put spreads only, inside the band no trade."),
    Setting("TRADING_BOLLINGER_GATE_W7", "7-day: Bollinger band", G_STRUCT, "bool", "false",
            "On: only trade a name outside its HOURLY 20-period 2-SD band -- at/below the lower band "
            "call spreads only, at/above the upper band put spreads only, inside the band no trade."),
    # --- The engine's own QQQ trades (trading_engine/nodes.py) ---------------
    # Section 228. The 0DTE group below is orphans.py, which manages positions the
    # engine did NOT open; the QQQ bucket's trades exit on these instead.
    # Stop and profit first (operator, 2026-10-04).
    Setting("TRADING_ENGINE_STOP_PCT", "QQQ engine stop loss", G_ENGINE, "float", "-20",
            "Sell the engine's QQQ spread when its return on cost falls to this.", "%", -100, 0),
    Setting("TRADING_STOP_CONFIRM_MINUTES", "QQQ engine stop confirmation", G_ENGINE, "float", "5",
            "Minutes the engine's stop level must hold before it sells. 0 = the first reading "
            "past the stop.", "min", 0, 30),
    Setting("TRADING_BAND_TOUCH_MIN_PROFIT_PCT", "20-SMA exit: minimum profit", G_ENGINE, "float", "0",
            "The QQQ engine sells when QQQ is back at the 20-SMA only if the spread is up at least "
            "this much. Below it the trade keeps running to the take profit, the stop or 15:45. "
            "0 = sell at the 20-SMA whatever the profit.", "%", 0, 200),
    Setting("TRADING_ENGINE_TAKE_PROFIT_PCT", "QQQ engine take profit", G_ENGINE, "float", "",
            "Sell the engine's QQQ spread once it is up this much (at the mid when 'Work orders "
            "from the mid' is on). Blank = sell only when QQQ is back at the 1-minute 20-SMA.",
            "%", 0, 500, allow_blank=True),
    Setting("TRADING_MID_ORDERS", "Work orders from the mid", G_ENGINE, "bool", "false",
            "Entries (QQQ engine and the stock books) rest at the spread's mid and are skipped if "
            "they do not fill. QQQ exits (take profit, 20-SMA target, and stops when 'stops at the "
            "mid too' is on) post at the mid and retry each minute. The force close always sells "
            "at the bid/ask."),
    Setting("TRADING_MID_ENTRY_MAX_STEPS", "Mid order: entry re-prices", G_ENGINE, "int", "0",
            "Entries (QQQ engine and the stock books): re-prices toward the ask after the mid. "
            "0 = the mid only; an entry that does not fill there is not taken.", "", 0, 10),
    Setting("TRADING_MID_ENTRY_WAIT_SECONDS", "Mid order: entry wait", G_ENGINE, "float", "20",
            "Seconds an entry rests at each price before it is cancelled.", "s", 2, 30),
    Setting("TRADING_MID_STEP", "Mid order: step", G_ENGINE, "float", "0.02",
            "How much each re-price moves toward the bid/ask, per spread.", "$", 0.01, 0.50),
    Setting("TRADING_MID_STEP_SECONDS", "Mid order: wait per step", G_ENGINE, "float", "6",
            "Seconds each price is left working before it is cancelled and re-priced.", "s", 2, 20),
    Setting("TRADING_MID_MAX_STEPS", "Mid order: exit re-prices", G_ENGINE, "int", "0",
            "Exit re-prices toward the bid/ask after the mid. 0 = the mid only.", "", 0, 10),
    Setting("TRADING_MID_STOPS", "Mid order: stops at the mid too", G_ENGINE, "bool", "true",
            "Stop-loss exits are posted at the mid like profit exits. An unfilled stop is re-posted "
            "at the new mid every cycle until it fills. The 15:45 force close always sells at the bid/ask."),
    Setting("TRADING_MID_EXIT_FALLBACK", "Mid order: exits may cross to the bid/ask", G_ENGINE, "bool", "false",
            "On: an exit that does not fill at the mid ends with an order at the bid/ask. Off: it "
            "waits and retries at the next cycle's mid."),
    Setting("TRADING_MID_BUDGET_SECONDS", "Mid order: time limit", G_ENGINE, "float", "25",
            "Wall-clock limit for the whole ladder. Keep well under 60: the next cycle starts a "
            "minute later.", "s", 5, 40),
    Setting("TRADING_WIN_COOLDOWN_MINUTES", "Wait after a win", G_ENGINE, "float", "0",
            "Minutes before the engine re-enters after a profitable exit, either direction. 0 = the "
            "next cycle.", "min", 0, 240),
    Setting("TRADING_REENTRY_COOLDOWN_MINUTES", "Wait after a loss", G_ENGINE, "float", "30",
            "Minutes before the engine re-enters the side that just stopped out.",
            "min", 0, 240),
    # --- 0DTE exit ladder (trading_engine/orphans.py) -----------------------
    # Stop and profit first: the rows a manual 0DTE trade is managed by.
    Setting("TRADING_ORPHAN_STOP_PCT", "Stop loss", G_0DTE, "float", "-25",
            "Close a debit spread when its return on cost falls to this.",
            "%", -100, 0),
    Setting("TRADING_ORPHAN_STOP_CONFIRM_MINUTES", "Stop confirmation", G_0DTE, "float", "2",
            "Minutes the stop level must hold before it fires. 0 = sell on the first reading past the stop.",
            "min", 0, 30),
    Setting("TRADING_ORPHAN_TARGET_RETURN_PCT", "Profit target (return on cost)", G_0DTE, "float", "0",
            "Sell once the spread is worth this % more than you paid (sold at the mid when "
            "'Profit exits sell at the mid' is on). Held back only while the drag guard (Max drag) "
            "says the sale would give away too much intrinsic on a deep in-the-money spread. "
            "0 disables; the 0DTE picker sizes entries against 30 when unset.",
            "%", 0, 500),
    Setting("TRADING_ORPHAN_STALL_ARM", "Stall starts watching at", G_0DTE, "float", "0",
            "The same-day stall only watches once the peak gain reaches this. After that every "
            "new high resets the watch to that level (a local max) and restarts the stall window. "
            "0 = watch from any gain.", "%", 0, 500),
    Setting("TRADING_ORPHAN_STALL_MINUTES", "Stall window", G_0DTE, "float", "0",
            "Close a winner that has not made a new peak for this long. 0 disables.",
            "min", 0, 240),
    Setting("TRADING_ORPHAN_STALL_GIVEBACK_PCT", "Stall give-back (points)", G_0DTE, "float", "0",
            "Give-back from peak, in return points, that also fires the stall.",
            "pts", 0, 200),
    Setting("TRADING_ORPHAN_STALL_MIN_GAIN_PCT", "Stall minimum gain", G_0DTE, "float", "8",
            "The stall only exits if the mark books at least this much. 0 = any gain.",
            "%", 0, 100),
    Setting("TRADING_ORPHAN_STOP_RESPECTS_INTRINSIC", "Stop waits while it pays at expiry", G_0DTE, "bool", "true",
            "Hold the stop off while the spread would still pay more than its cost at expiry. "
            "false = a plain stop on the price: at the stop level, it sells."),
    Setting("TRADING_ORPHAN_CREDIT_STOP_PCT", "Credit stop", G_0DTE, "float", "-600",
            "Stop for credit structures, as % of the credit collected.",
            "%", -2000, 0),
    Setting("TRADING_ORPHAN_CEILING", "Ceiling (fraction of width)", G_0DTE, "float", "0.90",
            "Close when the mark reaches this share of the spread width.",
            "x width", 0, 1),
    Setting("TRADING_ORPHAN_ASK", "Ask mode (resting sell limit)", G_0DTE, "bool", "false",
            "On a pinned 0DTE spread, rest a sell at the start fraction of width; step it "
            "down only while the underlying is under its session VWAP. Loss rules stay live."),
    Setting("TRADING_ORPHAN_ASK_START_WIDTH", "Ask starts at (x width)", G_0DTE, "float", "0.88",
            "The resting sell's first price, as a share of width.", "x width", 0.5, 1),
    Setting("TRADING_ORPHAN_ASK_FLOOR_WIDTH", "Ask floor (x width)", G_0DTE, "float", "0",
            "Lowest price the ask steps down to. 0 = the profit target level (entry x (1 + target)), "
            "which leaves NO ask when that is above the width.", "x width", 0, 1),
    Setting("TRADING_ORPHAN_ASK_STEP", "Ask step", G_0DTE, "float", "0.10",
            "How much each step lowers the ask.", "$", 0.01, 5),
    Setting("TRADING_ORPHAN_ASK_STEP_MINUTES", "Ask step interval", G_0DTE, "float", "3",
            "Minutes between steps (only while under VWAP).", "min", 1, 60),
    Setting("TRADING_ORPHAN_ASK_VWAP_FROM", "Ask steps from", G_0DTE, "time", "09:40",
            "No stepping before this time (ET)."),
    Setting("TRADING_ORPHAN_ASK_CANCEL_BY", "Ask withdrawn at", G_0DTE, "time", "15:40",
            "The ask is cancelled at this time (ET) and the flatten takes over."),
    Setting("TRADING_ORPHAN_STRIKE_GUARD", "Short-strike guard", G_0DTE, "bool", "false",
            "Close a same-day debit spread when the underlying is through the SHORT strike and "
            "under a VWAP moving against it for the guard minutes. Resets on a bounce."),
    Setting("TRADING_ORPHAN_STRIKE_GUARD_MINUTES", "Short-strike guard minutes", G_0DTE, "float", "3",
            "Continuous minutes through the strike under an adverse VWAP before it closes.",
            "min", 1, 30),
    Setting("TRADING_ORPHAN_STRIKE_GUARD_BUFFER", "Short-strike guard buffer", G_0DTE, "float", "0",
            "Points past the short strike before the clock starts (0 = at the strike).",
            "pts", 0, 50),
    Setting("TRADING_ORPHAN_PROFIT_LOCK_WIDTH", "Profit lock (x width over cost)", G_0DTE, "float", "0",
            "Once the spread's market price reaches cost + this share of width, sell if it falls "
            "back to that level (0.10 on a 10-wide @ 6.33 = 7.33). 0 disables.", "x width", 0, 1),
    Setting("TRADING_ORPHAN_UNDER_STOP", "Underlying stop (break-even line)", G_0DTE, "bool", "false",
            "Close a same-day debit when the UNDERLYING is past break-even (long strike +/- entry) "
            "plus the cushion, under an adverse VWAP, for the minutes below. Resets on a bounce."),
    Setting("TRADING_ORPHAN_UNDER_STOP_MINUTES", "Underlying stop minutes", G_0DTE, "float", "2",
            "Continuous minutes past the line before it closes.", "min", 1, 30),
    Setting("TRADING_ORPHAN_UNDER_STOP_CUSHION", "Underlying stop cushion", G_0DTE, "float", "0",
            "Points on the profitable side of break-even where the line sits (0 = at break-even).",
            "pts", 0, 50),
    Setting("TRADING_ORPHAN_UNDER_STOP_CUSHION_WIDTH", "Underlying stop cushion (x width)", G_0DTE, "float", "0",
            "Cushion as a fraction of width (0.10 = 1 point on a 10-wide spread); the larger of "
            "this and the points cushion applies.", "x width", 0, 1),
    Setting("TRADING_ORPHAN_UNDER_STOP_ARM_FIRST", "Underlying stop arms first", G_0DTE, "bool", "false",
            "Watch only once the underlying has been on the profitable side of the line since the "
            "position opened or was averaged."),
    Setting("TRADING_ORPHAN_PREARM_STOP_PCT", "Stop before armed", G_0DTE, "float", "0",
            "Mark stop used until the underlying stop arms (a fresh fill marks down by the bid-ask "
            "gap). 0 = the ordinary stop.", "%", -100, 0),
    Setting("TRADING_ORPHAN_UNDER_STOP_REQUIRE_TAPE", "Underlying stop needs adverse VWAP", G_0DTE, "bool", "true",
            "Only count minutes when the underlying is also under a VWAP moving against the spread."),
    Setting("TRADING_ORPHAN_PROFIT_EXIT_AT_MID", "Profit exits sell at the mid", G_0DTE, "bool", "false",
            "Stall, profit lock and target exits are priced at the spread mid instead of the "
            "bid/ask natural. An unfilled mid order is cancelled and re-priced next minute, so it "
            "never blocks the stop. Stops and the flatten always sell at the natural."),
    Setting("TRADING_ORPHAN_STALL_ON_MARK", "Stall watches the sale price", G_0DTE, "bool", "false",
            "true = the start level, the peak, the stall window and the give-back are all measured on "
            "what the spread would SELL for now, and the extrinsic-drag guard does not hold the exit "
            "back. It still never sells below the stall minimum gain. false = measured on intrinsic "
            "(moves with the underlying; a deep in-the-money spread can show +40% it cannot sell for)."),
    Setting("TRADING_ORPHAN_STALL_GIVEBACK_BAND", "Stall give-back (share of band)", G_0DTE, "float", "0",
            "Give-back as a fraction of the profit band (width − entry). "
            "Takes priority over the points give-back when above 0.",
            "x band", 0, 1),
    Setting("TRADING_ORPHAN_STALL_GIVEBACK_FRACTION", "Stall give-back (share of peak)", G_0DTE, "float", "0",
            "Give-back as a fraction of the peak gain. 0 disables.",
            "x peak", 0, 1),
    Setting("TRADING_ORPHAN_OTM_STOP", "OTM stop", G_0DTE, "bool", "true",
            "Close a debit spread as soon as it is out of the money (intrinsic 0)."),
    Setting("TRADING_ORPHAN_SLOW_STOP_PCT", "Slow stop", G_0DTE, "float", "0",
            "A second stop that needs the slow-stop window to confirm. 0 disables.",
            "%", -100, 0),
    Setting("TRADING_ORPHAN_SLOW_STOP_MINUTES", "Slow stop window", G_0DTE, "float", "30",
            "Minutes the slow stop must hold.", "min", 0, 240),
    Setting("TRADING_ORPHAN_HOLD_UNTIL", "No loss-taking before", G_0DTE, "time", "",
            "Exit rules wait until this time (ET). Blank = act from the open.",
            allow_blank=True),
    Setting("TRADING_ORPHAN_FORCE_CLOSE", "Force close at", G_0DTE, "time", "15:45",
            "Same-day positions are flattened at this time (ET)."),

    # --- Weekly / later-expiry ladder (orphans.py) --------------------------
    Setting("TRADING_ORPHAN_LATER_STOP_PCT", "Stop loss", G_WEEKLY, "float", "0",
            "Stop for positions not expiring today. 0 disables.", "%", -100, 0),
    Setting("TRADING_ORPHAN_LATER_STOP_MINUTES", "Stop confirmation", G_WEEKLY, "float", "15",
            "Minutes the weekly stop must hold.", "min", 0, 240),
    Setting("TRADING_ORPHAN_LATER_STOP_PCT_1D", "Stop loss, 1 day left", G_WEEKLY, "float", "-30",
            "The scaled stop on the day before expiry.", "%", -100, 0),
    Setting("TRADING_ORPHAN_LATER_STOP_MINUTES_1D", "Stop confirmation, 1 day left", G_WEEKLY, "float", "5",
            "", "min", 0, 240),
    Setting("TRADING_ORPHAN_LATER_SCALE_DAYS", "Scaling span (sessions)", G_WEEKLY, "int", "5",
            "Sessions over which the weekly stop/stall slide from the full-week to the 1-day values. "
            "2 = a step: full-week values with 2+ sessions left, 1-day values on the last day.", "", 2, 10),
    Setting("TRADING_ORPHAN_LATER_STALL_ARM", "Stall arms at", G_WEEKLY, "float", "10",
            "The weekly stall only watches once the gain reaches this.", "%", 0, 500),
    Setting("TRADING_ORPHAN_LATER_STALL_MINUTES", "Stall window", G_WEEKLY, "float", "15",
            "Minutes without a new peak before the armed stall fires.", "min", 0, 480),
    Setting("TRADING_ORPHAN_LATER_STALL_GIVEBACK", "Stall give-back (points)", G_WEEKLY, "float", "3.3",
            "Give-back from peak, in return points.", "pts", 0, 200),
    Setting("TRADING_ORPHAN_LATER_STALL_GIVEBACK_ATR", "Stall give-back (ATR)", G_WEEKLY, "float", "0",
            "Give-back in ATR of the underlying. Above 0 it replaces the points give-back.",
            "ATR", 0, 5),
    Setting("TRADING_ORPHAN_LATER_STALL_GIVEBACK_BAND", "Stall give-back (share of band)", G_WEEKLY, "float", "0",
            "Weekly band give-back. Unset = follows the 0DTE band setting.",
            "x band", 0, 1),
    Setting("TRADING_ORPHAN_LATER_TARGET_PCT", "Target (fraction of width)", G_WEEKLY, "float", "0",
            "Close a weekly when it reaches this share of width. 0 disables.",
            "x width", 0, 1),
    Setting("TRADING_ORPHAN_MAX_DRAG_WIDTH", "Max extrinsic drag", G_WEEKLY, "float", "0.15",
            "Refuse a profit exit that forfeits more than this share of width in extrinsic.",
            "x width", 0, 1),
    Setting("TRADING_ORPHAN_LATER_HOLD_UNTIL", "No loss-taking before", G_WEEKLY, "time", "",
            "Weekly opening quiet period (ET). Blank = same as the 0DTE setting.",
            allow_blank=True),

    # --- Section 246: where the long strike sits (entries) --------------------
    Setting("TRADING_PICK_LONG_MIN_ATR", "Long strike from (ATR)", G_PICK0, "float", "",
            "0DTE stocks: the long strike may sit this far out of the money at most, in the stock's daily ATR "
            "(negative = out of the money, 0 = at the money). On MU (ATR ~$45) -0.25 is ~$11 out. Blank = no limit. Mirrored for puts.",
            "ATR", -3, 3, allow_blank=True),
    Setting("TRADING_PICK_LONG_MAX_ATR", "Long strike to (ATR)", G_PICK0, "float", "",
            "0DTE stocks: the long strike may sit this far IN the money at most, in daily ATR. Blank = no limit.",
            "ATR", -3, 5, allow_blank=True),
    Setting("TRADING_PICK_MAX_EXTRINSIC", "Max time value (share of premium)", G_PICK0, "float", "25",
            "0DTE stocks: refuse a spread whose premium is more than this % time value. 25 forces deep in-the-money "
            "spreads; with the long-strike band set, 100 lets the band decide.", "%", 0, 100),
    Setting("TRADING_PICK_MAX_SHORT_ATR", "Short strike at most (ATR out)", G_PICK0, "float", "0.40",
            "0DTE stocks: the short strike may sit at most this many daily ATR from spot. A wider spread needs more "
            "(MU 1110 vs 1082 is ~0.6).", "ATR", 0, 3),
    Setting("TRADING_PICK_MAX_TARGET_ATR", "Target within (ATR)", G_PICK0, "float", "0.30",
            "0DTE stocks: the +target must be reachable within this many daily ATR of movement.", "ATR", 0, 3),
    Setting("TRADING_WEEKLY_MIN_ENTRY_WIDTH", "Weekly min entry (x width)", G_ENTRY, "float", "0.20",
            "3-day and 7-day: cheapest debit accepted, as a share of width.", "x width", 0, 1),
    Setting("TRADING_WEEKLY_MAX_ENTRY_WIDTH", "Weekly max entry (x width)", G_ENTRY, "float", "0.75",
            "3-day and 7-day: dearest debit accepted, as a share of width.", "x width", 0, 1),
    # --- Section 243: 3-day and 7-day spreads, each with its own settings ------
    Setting("TRADING_WEEKLY_LONG_MIN_DAYS", "7-day means bought this many days out", G_W7, "int", "5",
            "A weekly bought at least this many calendar days before expiry uses the 7-day settings for "
            "its whole life; fewer uses the 3-day settings. Expiry day always uses the 0DTE settings.", "days", 3, 10),
    Setting("TRADING_W3_LONG_MIN_ATR", "Long strike from (ATR)", G_W3, "float", "",
            "3-day entries: the long strike may sit this far out of the money at most, in the stock's daily ATR "
            "(negative = out of the money, 0 = at the money). -0.15..+0.25 keeps it near the money. Blank = no limit. Mirrored for puts.",
            "ATR", -3, 3, allow_blank=True),
    Setting("TRADING_W3_LONG_MAX_ATR", "Long strike to (ATR)", G_W3, "float", "",
            "3-day entries: the long strike may sit this far IN the money at most, in daily ATR. Blank = no limit.",
            "ATR", -3, 5, allow_blank=True),
    Setting("TRADING_W3_STOP_PCT", "Stop loss", G_W3, "float", "",
            "3-day spread: stop on return. Blank = the shared weekly stop (scaled by days left).", "%", -100, 0, allow_blank=True),
    Setting("TRADING_W3_STOP_MINUTES", "Stop confirmation", G_W3, "float", "",
            "Minutes the stop level must hold. Blank = shared weekly value.", "min", 0, 240, allow_blank=True),
    Setting("TRADING_W3_TARGET_RETURN_PCT", "Profit booking (return on cost)", G_W3, "float", "",
            "Sell at this return on cost. Blank = the 0DTE group's profit target, which also applies to weeklies.",
            "%", 0, 500, allow_blank=True),
    Setting("TRADING_W3_TARGET_WIDTH", "Max profit close (share of width)", G_W3, "float", "",
            "Sell once the spread is worth this share of its width. Blank = shared weekly target.",
            "x width", 0, 1, allow_blank=True),
    Setting("TRADING_W3_STALL_ARM", "Stall starts watching at", G_W3, "float", "",
            "The weekly stall watches once the gain reaches this. Blank = shared weekly value.", "%", 0, 500, allow_blank=True),
    Setting("TRADING_W3_STALL_MINUTES", "Stall window", G_W3, "float", "",
            "Minutes without a new peak before the stall fires. Blank = shared weekly value.", "min", 0, 480, allow_blank=True),
    Setting("TRADING_W3_FLATTEN", "Flatten at end of day", G_W3, "bool", "false",
            "Close 3-day spreads every day at the flatten time instead of holding them overnight."),
    Setting("TRADING_W3_FLATTEN_AT", "Flatten time", G_W3, "time", "15:45",
            "When the end-of-day flatten closes them (ET)."),
    Setting("TRADING_W7_LONG_MIN_ATR", "Long strike from (ATR)", G_W7, "float", "",
            "7-day entries: the long strike may sit this far out of the money at most, in the stock's daily ATR "
            "(negative = out of the money, 0 = at the money). -0.25..+0.25 keeps it near the money. Blank = no limit. Mirrored for puts.",
            "ATR", -3, 3, allow_blank=True),
    Setting("TRADING_W7_LONG_MAX_ATR", "Long strike to (ATR)", G_W7, "float", "",
            "7-day entries: the long strike may sit this far IN the money at most, in daily ATR. Blank = no limit.",
            "ATR", -3, 5, allow_blank=True),
    Setting("TRADING_W7_STOP_PCT", "Stop loss", G_W7, "float", "",
            "7-day spread: stop on return. Blank = the shared weekly stop (scaled by days left).", "%", -100, 0, allow_blank=True),
    Setting("TRADING_W7_STOP_MINUTES", "Stop confirmation", G_W7, "float", "",
            "Minutes the stop level must hold. Blank = shared weekly value.", "min", 0, 240, allow_blank=True),
    Setting("TRADING_W7_TARGET_RETURN_PCT", "Profit booking (return on cost)", G_W7, "float", "",
            "Sell at this return on cost. Blank = the 0DTE group's profit target, which also applies to weeklies.",
            "%", 0, 500, allow_blank=True),
    Setting("TRADING_W7_TARGET_WIDTH", "Max profit close (share of width)", G_W7, "float", "",
            "Sell once the spread is worth this share of its width. Blank = shared weekly target.",
            "x width", 0, 1, allow_blank=True),
    Setting("TRADING_W7_STALL_ARM", "Stall starts watching at", G_W7, "float", "",
            "The weekly stall watches once the gain reaches this. Blank = shared weekly value.", "%", 0, 500, allow_blank=True),
    Setting("TRADING_W7_STALL_MINUTES", "Stall window", G_W7, "float", "",
            "Minutes without a new peak before the stall fires. Blank = shared weekly value.", "min", 0, 480, allow_blank=True),
    Setting("TRADING_W7_FLATTEN", "Flatten at end of day", G_W7, "bool", "false",
            "Close 7-day spreads every day at the flatten time instead of holding them overnight."),
    Setting("TRADING_W7_FLATTEN_AT", "Flatten time", G_W7, "time", "15:45",
            "When the end-of-day flatten closes them (ET)."),

    # --- Account --------------------------------------------------------------
    Setting("TRADING_ACCOUNT_FLOOR", "Account floor", G_ACCOUNT, "float", "0",
            "Flatten EVERYTHING when equity falls to this. 0 disables.", "$", 0, 1_000_000),
    Setting("TRADING_MAX_DAILY_LOSS_PCT", "Daily loss limit", G_ACCOUNT, "float", "0.02",
            "QQQ engine: no new entries once today's realised losses reach this share of "
            "session-start equity (0.06 = 6%). Open positions are still managed.", "x equity", 0, 0.5),

    # --- Entries (scripts/dte0_trade.py) -------------------------------------
    Setting("TRADING_DTE0_MAX_ROTATIONS", "Max rotations", G_ENTRY, "int", "3",
            "New entries allowed per day after exits.", "", 0, 20),
    Setting("TRADING_DTE0_ROTATE_COOLDOWN_MIN", "Rotation cooldown", G_ENTRY, "float", "30",
            "Minutes between an exit and the next rotation entry.", "min", 0, 240),
    Setting("TRADING_DTE0_ROTATE_CUTOFF", "Rotation cutoff", G_ENTRY, "time", "13:30",
            "No new rotation entries after this time (ET)."),
    Setting("TRADING_WEEKLY_ROTATE_CUTOFF", "Weekly entry cutoff", G_ENTRY, "time", "15:30",
            "No new weekly-book entries after this time (ET). The 0DTE rotation cutoff above does "
            "not apply to weeklies."),
    Setting("TRADING_PICK_MIN_ENTRY_WIDTH", "0DTE min entry (×width)", G_ENTRY, "float", "0.30",
            "Cheapest debit accepted, as a share of width.", "x width", 0, 1),
    Setting("TRADING_PICK_MAX_ENTRY_WIDTH", "0DTE max entry (×width)", G_ENTRY, "float", "0.75",
            "Dearest debit accepted, as a share of width.", "x width", 0, 1),
    Setting("TRADING_PICK_MIN_SHORT_PAYS_PCT", "Short leg must pay (both books)", G_ENTRY, "float", "10",
            "Reject a debit spread whose short-leg bid is under this % of the long leg's ask "
            "(a worthless short makes it a long call). 0 disables.", "%", 0, 60),
    Setting("TRADING_WEEKLY_MIN_PWIN", "Weekly min Pwin", G_ENTRY, "float", "0.45",
            "Weekly picks need at least this win probability.", "", 0, 1),
)

# SECTION 261: THE BOOK CARDS. Every book shows the same core rows, in the same
# order and with the same labels (orders 10..70); QQQ adds its band details at
# 41-45 because band touch is its only entry. Everything not listed here is an
# Advanced row on the settings page. Labels here replace the REGISTRY label.
_CORE = ("On / off", "Budget", "Entries per day", "Bollinger band",
         "Stop loss", "Stop confirmation", "Take profit")
_CARDS: dict[str, tuple] = {
    "qqq": ("TRADING_BUCKET_QQQ_0DTE", "TRADING_POSITION_BUDGET", "TRADING_MAX_ENTRIES_QQQ_0DTE",
            "TRADING_BAND_TOUCH_PERIOD", "TRADING_ENGINE_STOP_PCT", "TRADING_STOP_CONFIRM_MINUTES",
            "TRADING_ENGINE_TAKE_PROFIT_PCT"),
    "s0": ("TRADING_BUCKET_STOCK_0DTE", "TRADING_DTE0_MAX_BUDGET", "TRADING_MAX_ENTRIES_STOCK_0DTE",
           "TRADING_BOLLINGER_GATE_DTE0", "TRADING_ORPHAN_STOP_PCT", "TRADING_ORPHAN_STOP_CONFIRM_MINUTES",
           "TRADING_ORPHAN_TARGET_RETURN_PCT"),
    "w3": ("TRADING_BUCKET_STOCK_W3", "TRADING_W3_MAX_BUDGET", "TRADING_MAX_ENTRIES_STOCK_W3",
           "TRADING_BOLLINGER_GATE_W3", "TRADING_W3_STOP_PCT", "TRADING_W3_STOP_MINUTES",
           "TRADING_W3_TARGET_RETURN_PCT"),
    "w7": ("TRADING_BUCKET_STOCK_W7", "TRADING_W7_MAX_BUDGET", "TRADING_MAX_ENTRIES_STOCK_W7",
           "TRADING_BOLLINGER_GATE_W7", "TRADING_W7_STOP_PCT", "TRADING_W7_STOP_MINUTES",
           "TRADING_W7_TARGET_RETURN_PCT"),
}
_EXTRA: dict[str, tuple] = {   # key -> (book, order, label)
    "TRADING_BAND_TOUCH_SD": ("qqq", 41, "Bollinger band: width (SD)"),
    "TRADING_BAND_TOUCH_START": ("qqq", 42, "Bollinger band: from"),
    "TRADING_BAND_TOUCH_END": ("qqq", 43, "Bollinger band: until"),
    "TRADING_BAND_TOUCH_WIDTH": ("qqq", 44, "Spread width"),
    "TRADING_BAND_TOUCH_MIN_PROFIT_PCT": ("qqq", 71, "20-SMA exit: minimum profit"),
    "TRADING_MAX_TRADES_STOCK_0DTE": ("s0", 31, "Trades per run"),
    "TRADING_MAX_TRADES_STOCK_W3": ("w3", 31, "Trades per run"),
    "TRADING_MAX_TRADES_STOCK_W7": ("w7", 31, "Trades per run"),
    "TRADING_MID_ORDERS": ("global", 10, "Work orders from the mid"),
    "TRADING_ACCOUNT_FLOOR": ("global", 20, "Account floor"),
    "TRADING_MAX_DAILY_LOSS_PCT": ("global", 30, "Daily loss limit"),
    "TRADING_ORPHAN_FORCE_CLOSE": ("global", 40, "Force close"),
}
_HELP: dict[str, str] = {
    "TRADING_BAND_TOUCH_PERIOD": (
        "The QQQ engine's only entry: live QQQ at/below the lower 1-minute band buys a call "
        "spread, at/above the upper band a put spread; it sells at the 20-SMA, the take profit "
        "or the stop. This is the number of 1-minute bars in the band's moving average."),
    "TRADING_ORPHAN_STOP_PCT": (
        "Sell a same-day stock spread when its return on cost falls to this. Also governs "
        "manual trades and weeklies on their expiry day."),
    "TRADING_ORPHAN_TARGET_RETURN_PCT": (
        "Sell a same-day stock spread once it is up this much. Also governs manual trades and "
        "weeklies on their expiry day. 0 = no target."),
    "TRADING_ORPHAN_FORCE_CLOSE": (
        "Every same-day position -- engine, stock books and manual -- is flattened at this "
        "time (ET), at the bid/ask."),
}


def _carded(registry: tuple) -> tuple:
    where: dict[str, tuple] = dict(_EXTRA)
    for book, keys in _CARDS.items():
        for i, k in enumerate(keys):
            where[k] = (book, (i + 1) * 10, _CORE[i])
    out = []
    for st in registry:
        if st.key in where:
            book, order, label = where[st.key]
            st = replace(st, book=book, order=order, label=label,
                         help=_HELP.get(st.key, st.help))
        out.append(st)
    return tuple(out)


REGISTRY = _carded(REGISTRY)
BY_KEY: dict[str, Setting] = {s.key: s for s in REGISTRY}

# The environment as the container started it, before overrides were applied.
# Captured on the first apply so "what would .env.production give me" stays
# answerable in a process whose os.environ has since been overwritten.
_BASE_ENV: Optional[dict[str, Optional[str]]] = None


def validate(key: str, raw: str) -> str:
    """Canonical string for `raw`, or ValueError saying why it is refused."""
    s = BY_KEY.get(key)
    if s is None:
        raise ValueError(f"{key} is not a tunable setting")
    v = str(raw).strip()
    if v == "" and s.allow_blank:
        return ""
    if s.kind == "bool":
        low = v.lower()
        if low in ("true", "1", "yes", "on"):
            return "true"
        if low in ("false", "0", "no", "off"):
            return "false"
        raise ValueError(f"{key}: expected true/false, got {raw!r}")
    if s.kind == "time":
        if not _TIME_RE.match(v):
            raise ValueError(f"{key}: expected HH:MM (24h, ET), got {raw!r}")
        return v
    try:
        num = int(v) if s.kind == "int" else float(v)
    except ValueError:
        raise ValueError(f"{key}: expected a number, got {raw!r}") from None
    if s.kind == "float" and num != num:  # NaN
        raise ValueError(f"{key}: NaN is not a setting")
    if s.min is not None and num < s.min:
        raise ValueError(f"{key}: {num} is below the minimum {s.min:g}")
    if s.max is not None and num > s.max:
        raise ValueError(f"{key}: {num} is above the maximum {s.max:g}")
    return str(num) if s.kind == "int" else f"{num:g}"


def read_file(path: Optional[str] = None) -> tuple[dict[str, str], list[str]]:
    """(valid overrides, problems). Unknown or invalid lines are skipped, never applied."""
    path = path or OVERRIDES_PATH
    out: dict[str, str] = {}
    problems: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return out, problems
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append(f"line {n}: no '=' -- ignored")
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        try:
            out[key] = validate(key, val)
        except ValueError as e:
            problems.append(f"line {n}: {e} -- ignored")
    return out, problems


def apply(path: Optional[str] = None) -> dict[str, str]:
    """Load the overrides into os.environ. Called from trading_engine/__init__."""
    global _BASE_ENV
    if _BASE_ENV is None:
        _BASE_ENV = {k: os.environ.get(k) for k in BY_KEY}
    overrides, problems = read_file(path)
    for p in problems:
        # print, not logging: this runs before any logging is configured and a
        # refused line must be visible in /var/log/qqq-trading.log.
        print(f"[trading_overrides] {p}")
    os.environ.update(overrides)
    return overrides


def base_value(key: str) -> Optional[str]:
    """What .env.production (the container env) sets, ignoring overrides."""
    if _BASE_ENV is not None:
        return _BASE_ENV.get(key)
    return os.environ.get(key)


_KEY_RE = re.compile(r"^TRADING_[A-Z0-9_]+$")


def _foreign_lines(path: str) -> list[str]:
    """TRADING_* lines this process's REGISTRY does not know, kept verbatim.

    Section 244. An API process started before a setting was added re-read the
    file through its own older whitelist and rewrote it without the new keys:
    2026-09-26 15:08 a UI save of the account floor deleted the four gate
    switches set from the CLI a minute earlier. Unknown keys are still never
    APPLIED by this process (read_file skips them); they are just not erased.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key not in BY_KEY and _KEY_RE.match(key):
            out.append(line)
    return out


def _write(overrides: dict[str, str], path: str) -> None:
    foreign = _foreign_lines(path)
    lines = [
        "# Trading overrides -- written by /trading/settings and scripts/settings.py.",
        "# Beats .env.production; picked up by the next cron cycle (no restart).",
        "# Only keys in trading_engine/settings_overrides.REGISTRY are honoured.",
    ]
    for s in REGISTRY:  # registry order keeps the file readable
        if s.key in overrides:
            lines.append(f"{s.key}={overrides[s.key]}")
    if foreign:
        lines.append("# kept: settings this process's code does not know (newer or retired)")
        lines.extend(foreign)
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".trading_overrides.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)  # atomic: a cycle never reads a half-written file


def _audit(who: str, changes: list[tuple[str, Optional[str], Optional[str]]]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            for key, old, new in changes:
                f.write(f"{stamp}\t{who}\t{key}\t{old if old is not None else '(unset)'}"
                        f" -> {new if new is not None else '(unset)'}\n")
    except OSError:
        pass  # an unwritable audit log must not block a stop-loss change


def set_many(values: dict[str, object], who: str, path: Optional[str] = None) -> dict[str, str]:
    """Validate every value first, then write once. All or nothing."""
    path = path or OVERRIDES_PATH
    clean = {k: validate(k, str(v)) for k, v in values.items()}
    current, _ = read_file(path)
    changes = [(k, current.get(k), v) for k, v in clean.items() if current.get(k) != v]
    if changes:
        current.update(clean)
        _write(current, path)
        _audit(who, changes)
    return current


def unset(keys: list[str], who: str, path: Optional[str] = None) -> dict[str, str]:
    path = path or OVERRIDES_PATH
    for k in keys:
        if k not in BY_KEY:
            raise ValueError(f"{k} is not a tunable setting")
    current, _ = read_file(path)
    changes = [(k, current.pop(k), None) for k in keys if k in current]
    if changes:
        _write(current, path)
        _audit(who, changes)
    return current


def snapshot(path: Optional[str] = None) -> dict:
    """Every tunable with its default, .env.production value, override and effective value."""
    overrides, problems = read_file(path)
    rows = []
    for s in REGISTRY:
        base = base_value(s.key)
        ov = overrides.get(s.key)
        if ov is not None:
            effective, source = ov, "override"
        elif base is not None:
            effective, source = base, "env"
        else:
            effective, source = s.default, "default"
        rows.append({**asdict(s), "env_value": base, "override": ov,
                     "effective": effective, "source": source})
    return {"path": path or OVERRIDES_PATH, "settings": rows, "problems": problems}
