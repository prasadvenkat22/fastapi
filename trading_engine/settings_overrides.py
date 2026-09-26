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
from dataclasses import asdict, dataclass
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


G_ENGINE = "QQQ engine exits (the engine's own trades)"
G_0DTE = "0DTE exits"
G_WEEKLY = "Weekly exits (later expiry)"
G_W3 = "3-day spreads (bought 2-4 days before expiry)"
G_W7 = "7-day spreads (bought 5+ days before expiry)"
G_ACCOUNT = "Account"
G_ENTRY = "Entries & budgets"
G_BUCKETS = "Trade buckets (new entries on/off)"
G_STRUCT = "Entry structure gates (all buckets)"

REGISTRY: tuple[Setting, ...] = (
    # --- Buckets: new entries only; open positions are always managed --------
    # OFF BY DEFAULT (operator, 2026-09-24): nothing opens a new trade until a
    # bucket is switched on in /desk/settings (or `tset set ...=true`).
    Setting("TRADING_BUCKET_QQQ_0DTE", "QQQ 0DTE (engine)", G_BUCKETS, "bool", "false",
            "The engine's own QQQ same-day entries (debit, credit, condor). Off: no new "
            "entries; an open position is still managed."),
    Setting("TRADING_POSITION_BUDGET", "QQQ 0DTE budget", G_BUCKETS, "float", "1000",
            "Capital the engine sizes a QQQ same-day entry against (realised equity, daily-loss "
            "limit and position risk are all scaled from it).", "$", 0, 1_000_000),
    Setting("TRADING_BUCKET_STOCK_0DTE", "Single-stock 0DTE (rotation)", G_BUCKETS, "bool", "false",
            "dte0_trade.py same-day entries on single names. Off: screens and logs only."),
    Setting("TRADING_DTE0_MAX_BUDGET", "Single-stock 0DTE budget", G_BUCKETS, "float", "1500",
            "Total the same-day rotation commits per run, split across its max trades.",
            "$", 0, 1_000_000),
    Setting("TRADING_BUCKET_STOCK_WEEKLY", "Single-stock weekly (rotation)", G_BUCKETS, "bool", "false",
            "dte0_trade.py --book weekly entries. Off: screens and logs only."),
    Setting("TRADING_WEEKLY_MAX_BUDGET", "Single-stock weekly budget", G_BUCKETS, "float", "5000",
            "Total the weekly rotation commits per run, split across its max trades.",
            "$", 0, 1_000_000),
    # --- Section 241: where in the range / which side of the midline -------
    Setting("TRADING_WEEKRANGE_GUARD", "Week-range guard", G_STRUCT, "bool", "false",
            "All three buckets: no call spread (bullish) near the week's high, no put spread "
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
            "weak (47% vs 43% up by the close). Not applied to the QQQ engine, whose bullish setups "
            "require price above its average."),
    Setting("TRADING_MACRO_BAD_PUTS_ONLY", "Macro BAD: put spreads only (stocks)", G_STRUCT, "bool", "false",
            "Single-stock books refuse bullish spreads while the engine's macro verdict is BAD. The "
            "QQQ engine already does this. NOT supported by the measurement (macro gates showed no "
            "direction), set at the operator's direction."),
    # --- The engine's own QQQ trades (trading_engine/nodes.py) ---------------
    # Section 228. The 0DTE group below is orphans.py, which manages positions the
    # engine did NOT open; the QQQ bucket's trades exit on these instead.
    Setting("TRADING_STALL_MINUTES", "Stall window (morning debit)", G_ENGINE, "float", "0",
            "Close the engine's QQQ debit spread once it has gone this long without a new peak "
            "AND sits the give-back below it. 0 disables. Also the orphan stall window when "
            "TRADING_ORPHAN_STALL_MINUTES is unset.", "min", 0, 240),
    Setting("TRADING_STALL_GIVEBACK_PCT", "Stall give-back (morning debit)", G_ENGINE, "float", "0",
            "Return points below the peak that the stall needs. 0 disables. Also the orphan "
            "give-back when TRADING_ORPHAN_STALL_GIVEBACK_PCT is unset.", "pts", 0, 200),
    Setting("TRADING_STALL_ON_CREDIT", "Stall on the credit trade", G_ENGINE, "bool", "false",
            "The afternoon credit spread exits on the stall instead of booking at its take-profit."),
    Setting("TRADING_CREDIT_STALL_ARM", "Credit stall waits for the target", G_ENGINE, "bool", "true",
            "true = the credit stall only watches once the take-profit is reached. "
            "false = it watches from entry, like the morning stall."),
    Setting("TRADING_WIN_COOLDOWN_MINUTES", "Wait after a win", G_ENGINE, "float", "0",
            "Minutes before the engine re-enters after a profitable exit, either direction. 0 = the "
            "next cycle (the state every sweep in sections 27-51 was measured under).", "min", 0, 240),
    Setting("TRADING_REENTRY_COOLDOWN_MINUTES", "Wait after a loss", G_ENGINE, "float", "30",
            "Minutes before re-entering the side that just stopped out. Measured (60 sessions): no "
            "cooldown +51.17/day, 30 min +58.13, 90 min +60.15, and no cooldown widens the worst day.",
            "min", 0, 240),
    Setting("TRADING_CREDIT_STALL_MINUTES", "Credit stall window", G_ENGINE, "float", "",
            "Its own window for the credit stall. Blank = same as the morning window.",
            "min", 0, 240, allow_blank=True),
    Setting("TRADING_CREDIT_STALL_GIVEBACK_PCT", "Credit stall give-back", G_ENGINE, "float", "",
            "Its own give-back for the credit stall. Blank = same as the morning give-back.",
            "pts", 0, 200, allow_blank=True),
    # --- 0DTE exit ladder (trading_engine/orphans.py) -----------------------
    Setting("TRADING_ORPHAN_STOP_PCT", "Stop loss", G_0DTE, "float", "-25",
            "Close a debit spread when its return on cost falls to this.",
            "%", -100, 0),
    Setting("TRADING_ORPHAN_STOP_CONFIRM_MINUTES", "Stop confirmation", G_0DTE, "float", "2",
            "Minutes the stop level must hold before it fires. 0 = immediate.",
            "min", 0, 30),
    Setting("TRADING_ORPHAN_STOP_RESPECTS_INTRINSIC", "Stop waits while it pays at expiry", G_0DTE, "bool", "true",
            "Hold the stop off while the spread would still pay more than its cost at expiry. "
            "false = a plain stop on the price: at the stop level, it sells."),
    Setting("TRADING_ORPHAN_CREDIT_STOP_PCT", "Credit stop", G_0DTE, "float", "-600",
            "Stop for credit structures, as % of the credit collected.",
            "%", -2000, 0),
    Setting("TRADING_ORPHAN_TARGET_RETURN_PCT", "Profit target (return on cost)", G_0DTE, "float", "0",
            "Take profit at this return on cost. 0 disables in the exit ladder; "
            "the 0DTE picker sizes entries against 30 when unset.",
            "%", 0, 500),
    Setting("TRADING_ORPHAN_TAKE_PROFIT", "Take profit (legacy)", G_0DTE, "float", "50",
            "Older flat take-profit on return.", "%", 0, 500),
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
    Setting("TRADING_ORPHAN_STALL_MINUTES", "Stall window", G_0DTE, "float", "0",
            "Close a winner that has not made a new peak for this long. 0 disables.",
            "min", 0, 240),
    Setting("TRADING_ORPHAN_STALL_ARM", "Stall starts watching at", G_0DTE, "float", "0",
            "The same-day stall only watches once the peak gain reaches this. After that every "
            "new high resets the watch to that level (a local max) and restarts the stall window. "
            "0 = watch from any gain.", "%", 0, 500),
    Setting("TRADING_ORPHAN_PROFIT_EXIT_AT_MID", "Profit exits sell at the mid", G_0DTE, "bool", "false",
            "Stall, profit lock and target exits are priced at the spread mid instead of the "
            "bid/ask natural. An unfilled mid order is cancelled and re-priced next minute, so it "
            "never blocks the stop. Stops and the flatten always sell at the natural."),
    Setting("TRADING_ORPHAN_STALL_ON_MARK", "Stall watches the sale price", G_0DTE, "bool", "false",
            "true = the start level, the peak, the stall window and the give-back are all measured on "
            "what the spread would SELL for now, and the extrinsic-drag guard does not hold the exit "
            "back. It still never sells below the stall minimum gain. false = measured on intrinsic "
            "(moves with the underlying; a deep in-the-money spread can show +40% it cannot sell for)."),
    Setting("TRADING_ORPHAN_STALL_GIVEBACK_PCT", "Stall give-back (points)", G_0DTE, "float", "0",
            "Give-back from peak, in return points, that also fires the stall.",
            "pts", 0, 200),
    Setting("TRADING_ORPHAN_STALL_GIVEBACK_BAND", "Stall give-back (share of band)", G_0DTE, "float", "0",
            "Give-back as a fraction of the profit band (width − entry). "
            "Takes priority over the points give-back when above 0.",
            "x band", 0, 1),
    Setting("TRADING_ORPHAN_STALL_GIVEBACK_FRACTION", "Stall give-back (share of peak)", G_0DTE, "float", "0",
            "Give-back as a fraction of the peak gain. 0 disables.",
            "x peak", 0, 1),
    Setting("TRADING_ORPHAN_STALL_MIN_GAIN_PCT", "Stall minimum gain", G_0DTE, "float", "8",
            "The stall only exits if the mark books at least this much. 0 = any gain.",
            "%", 0, 100),
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

    # --- Section 243: 3-day and 7-day spreads, each with its own settings ------
    Setting("TRADING_WEEKLY_LONG_MIN_DAYS", "7-day means bought this many days out", G_W7, "int", "5",
            "A weekly bought at least this many calendar days before expiry uses the 7-day settings for "
            "its whole life; fewer uses the 3-day settings. Expiry day always uses the 0DTE settings.", "days", 3, 10),
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


def _write(overrides: dict[str, str], path: str) -> None:
    lines = [
        "# Trading overrides -- written by /trading/settings and scripts/settings.py.",
        "# Beats .env.production; picked up by the next cron cycle (no restart).",
        "# Only keys in trading_engine/settings_overrides.REGISTRY are honoured.",
    ]
    for s in REGISTRY:  # registry order keeps the file readable
        if s.key in overrides:
            lines.append(f"{s.key}={overrides[s.key]}")
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
