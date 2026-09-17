"""Replay a session's positions against different exit settings.

    python scripts/exit_backtest.py                 # today
    python scripts/exit_backtest.py 2026-09-15      # a past session
    python scripts/exit_backtest.py 2026-09-15 QQQ  # one underlying
    python scripts/exit_backtest.py --all           # every session in the logs

WHAT IS REAL: the marks. Every row comes from the engine's own ORPHAN log
lines -- the value it actually saw, at the time it saw it, priced at what it
could have transacted at. No reconstruction, no model, no assumption about
what a spread was "worth".

WHAT IS NOT, and both matter when reading the output:

  TRUNCATION BIASES AGAINST PATIENT RULES. A rule that would have held LONGER
  than the real exit has no data past that point, so those runs stop at the
  last observed mark and are scored there. They show as HELD.

  IT REPLAYS EXITS, NOT ENTRIES. Nothing here can say whether a different tier
  or window would have been profitable, because a trade that was never taken
  left no marks.

IT NOW READS INTRINSIC, which is what lets it test a rule against the
UNDERLYING rather than against the spread mark. 2026-09-16 is the case that
demanded it: a QQQ 710/714 stopped out for -716 while QQQ moved 0.3% and the
mark moved 130%, then reversed six minutes later to a value worth -228. A
percentage stop on the mark is a stop on gamma-amplified noise.

  intrinsic == 0 on a debit spread means EXACTLY "the underlying is at or
  beyond the long strike" -- out of the money, nothing but premium left. That
  is the thesis of the position failing, stated in one number the engine
  already computes every cycle. No extra quotes, no new feed.

  The exact spot is only recoverable while a spread is PARTIALLY in the money
  (S = long + intrinsic for a call). Fully in or fully out gives a bound, not
  a price -- which is all this rule needs.

READS ROTATED LOGS TOO. logrotate compresses the engine log several times a
day on this box, and the first version of this read only the live file --
which on a quiet evening held about forty lines and none of the session.
"""

from __future__ import annotations

import glob
import gzip
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime

LOG_GLOB = "/var/log/qqq-trading.log*"
CLOSES = "/opt/fastapi/.expiry_closes.json"


def _closes() -> dict:
    """{symbol: {date: close}} from scripts/expiry_closes.py, or {}."""
    try:
        with open(CLOSES, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def settle(sym, day, right, long_k, short_k, entry, closes):
    """What a HELD 0DTE debit spread was ACTUALLY worth, or None.

    THE TRUNCATION FIX. A HELD run has no marks past the real exit, so it used
    to be scored where the marks stopped -- which for an expiring contract is
    usually near its worst, and which punished exactly the patient rules the
    sweep exists to test. But a 0DTE position expires the same session: what
    it was worth if held is not a guess, it is the intrinsic at the close.

    Returns None when the close is not cached, and the caller then falls back
    to the last mark. A missing close must leave a run scored as it was
    before, never scored wrongly -- a fallback that quietly invents a number
    is how _mark_value hid its own bug for a day.
    """
    row = closes.get(sym) or {}
    px = row.get(day + "@flatten")
    if px is None:
        px = row.get(day)
    if px is None:
        return None
    width = abs(long_k - short_k)
    if width <= 0 or entry <= 0:
        return None
    iv = (max(0.0, min(px - long_k, width)) if right == "C"
          else max(0.0, min(long_k - px, width)))
    return (iv - entry) / entry * 100.0

# entry / value / return / intrinsic, as the engine logged them.
RX = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO ORPHAN ([A-Z]+) ([CP]) "
    r"([\d.]+)/([\d.]+) x(\d+) (debit|credit): entry ([\d.]+) value ([\d.]+) "
    r"([-+][\d.]+)%(?P<exp>.*?\[expires (\d{6})\])?.*?\[intrinsic ([\d.]+),"
)

# (label, target, stop, confirm, stall, giveback, otm_floor, otm_minutes)
# otm_minutes < 0 disables the underlying stop.
CONFIGS = [
    ("tgt30 stop-10 cf0  stall5/20            ", 30, -10, 0, 5, 20, 0.0, -1),
    ("LIVE  tgt30 stop-35 cf5  stall5/20      ", 30, -35, 5, 5, 20, 0.0, -1),
    ("      tgt30 stop-35 cf2  stall5/20      ", 30, -35, 2, 5, 20, 0.0, -1),
    ("      tgt30 stop-60 cf0  stall5/20      ", 30, -60, 0, 5, 20, 0.0, -1),
    ("UND   tgt30 stop-35 cf5 + OTM 0min      ", 30, -35, 5, 5, 20, 0.0, 0),
    ("*LIVE NOW  +OTM0 +gain8 +drag15         ", 30, -35, 5, 5, 20, 0.0, 0, False, 0.0, 8.0, 0.15),
    ("      LIVE NOW but gain floor 0         ", 30, -35, 5, 5, 20, 0.0, 0, False, 0.0, 0.0, 0.15),
    ("      LIVE NOW but no drag guard        ", 30, -35, 5, 5, 20, 0.0, 0, False, 0.0, 8.0, 0.0),
    ("      LIVE NOW but gain floor 15        ", 30, -35, 5, 5, 20, 0.0, 0, False, 0.0, 15.0, 0.15),
    ("      LIVE NOW but drag guard 5%        ", 30, -35, 5, 5, 20, 0.0, 0, False, 0.0, 8.0, 0.05),
    ("      LIVE NOW, no OTM stop             ", 30, -35, 5, 5, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("ITM   OTM 0min, only if it WAS itm       ", 30, -35, 5, 5, 20, 0.0, 0, True),
    ("ITM   OTM 2min, only if it WAS itm       ", 30, -35, 5, 5, 20, 0.0, 2, True),
    ("ITM   OTM 5min, only if it WAS itm       ", 30, -35, 5, 5, 20, 0.0, 5, True),
    ("UND   tgt30 stop-35 cf5 + OTM 2min      ", 30, -35, 5, 5, 20, 0.0, 2),
    ("UND   tgt30 stop-35 cf5 + OTM 5min      ", 30, -35, 5, 5, 20, 0.0, 5),
    ("UND   tgt30 stop-35 cf5 + OTM 10min     ", 30, -35, 5, 5, 20, 0.0, 10),
    ("UND   tgt30 stop-35 cf5 + OTM 20min     ", 30, -35, 5, 5, 20, 0.0, 20),
    ("UND   tgt30 NO mark stop + OTM 5min     ", 30, -999, 0, 5, 20, 0.0, 5),
    ("GB    LIVE + intrinsic giveback  5%     ", 30, -35, 5, 5, 20, 0.0, 0, False, 5.0),
    ("GB    LIVE + intrinsic giveback 10%     ", 30, -35, 5, 5, 20, 0.0, 0, False, 10.0),
    ("GB    LIVE + intrinsic giveback 15%     ", 30, -35, 5, 5, 20, 0.0, 0, False, 15.0),
    ("GB    LIVE + intrinsic giveback 20%     ", 30, -35, 5, 5, 20, 0.0, 0, False, 20.0),
    ("GB    LIVE + intrinsic giveback 30%     ", 30, -35, 5, 5, 20, 0.0, 0, False, 30.0),
    ("SM    LIVE, stall quiet  2 min          ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("SM    LIVE, stall quiet  5 min  <-now    ", 30, -35, 5, 5, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("SM    LIVE, stall quiet 10 min          ", 30, -35, 5, 10, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("SM    LIVE, stall quiet 15 min          ", 30, -35, 5, 15, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("SM    LIVE, stall quiet 25 min          ", 30, -35, 5, 25, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("SM    LIVE, stall quiet 40 min          ", 30, -35, 5, 40, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("SM    LIVE, stall OFF                   ", 30, -35, 5, 9999, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("SS    LIVE + slow stop -10%/5min        ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 5.0),
    ("DT    soft -5%/30min  hard -10%/0min    ", 30, -10, 0, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -5.0, 30.0),
    ("DT    soft -5%/30min  hard -10%/5min    ", 30, -10, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -5.0, 30.0),
    ("DT    soft -5%/10min  hard -10%/2min    ", 30, -10, 2, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -5.0, 10.0),
    ("DT    soft -5%/5min   hard -10%/5min    ", 30, -10, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -5.0, 5.0),
    ("DT    soft -10%/30min hard -15%/5min    ", 30, -15, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 30.0),
    ("DT    soft -10%/30min hard -20%/5min    ", 30, -20, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 30.0),
    ("DT    soft -10%/30min hard -25%/5min    ", 30, -25, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 30.0),
    ("DT    soft -10%/30min hard -30%/5min    ", 30, -30, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 30.0),
    ("DT    soft -10%/30min hard -35%/5min  *  ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 30.0),
    ("FS    slow -10%/5min + fast -15%/5min   ", 30, -15, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 5.0),
    ("FS    slow -10%/5min + fast -20%/5min   ", 30, -20, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 5.0),
    ("FS    slow -10%/5min, NO fast stop      ", 30, -999, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 5.0),
    ("FS    slow -10%/5min + fast -35%/5min   ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 5.0),
    ("FS    slow -10%/30min, NO fast stop     ", 30, -999, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 30.0),
    ("FS    slow -10%/30min + fast -35%/5min  ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 30.0),
    ("FS    NO slow, fast -35%/5min only      ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("SS    LIVE + slow stop -10%/10min       ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 10.0),
    ("SS    LIVE + slow stop -10%/30min       ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 30.0),
    ("SS    LIVE + slow stop -10%/60min       ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -10.0, 60.0),
    ("SS    LIVE + slow stop -15%/30min       ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -15.0, 30.0),
    ("SS    LIVE + slow stop -20%/30min       ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -20.0, 30.0),
    ("SS    LIVE + slow stop -20%/15min       ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -20.0, 15.0),
    ("SS    LIVE + slow stop -25%/30min       ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15, -25.0, 30.0),
    ("SS    LIVE (no slow stop)               ", 30, -35, 5, 2, 20, 0.0, -1, False, 0.0, 8.0, 0.15),
    ("      no exits, ride to the last mark   ", 999, -999, 0, 999, 999, 0.0, -1),
]


def load(day: str = "", only: str = "") -> dict:
    """{(day, symbol, C/P, strikes, entry): [(t, value, ret, qty, iv)]}."""
    out = defaultdict(list)
    for fn in sorted(glob.glob(LOG_GLOB)):
        opener = gzip.open if fn.endswith(".gz") else open
        try:
            fh = opener(fn, "rt", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                m = RX.match(line)
                if not m:
                    continue
                (ts, sym, right, lo, hi, qty, kind, entry, val, ret,
                 _exptag, expiry, iv) = m.groups()
                if kind != "debit":
                    continue          # intrinsic inverts on a credit; not tested
                if day and not ts.startswith(day):
                    continue
                if only and sym != only.upper():
                    continue
                # expiry None == 0DTE: the log omits the tag when it expires
                # today, and that is the only case this can settle.
                out[(ts[:10], sym, right, f"{lo}/{hi}", float(entry),
                     expiry or "")].append(
                    (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"),
                     float(val), float(ret), int(qty), float(iv)))
    return {k: sorted(set(v)) for k, v in out.items()}


def run(marks, target, stop, confirm, stall_min, giveback, otm_floor, otm_min,
        otm_needs_itm=False, gb_pct=0.0, gb_confirm=5.0, width=0.0,
        entry=0.0, min_gain=0.0, drag_ceiling=0.0,
        slow_stop=0.0, slow_min=30.0):
    """Walk the marks once. First rule to fire wins, as the engine does."""
    peak = peak_at = stop_since = otm_since = slow_since = None
    was_itm = False
    peak_iv, gb_since = None, None
    for t, _val, ret, qty, iv in marks:
        if iv > otm_floor:
            was_itm = True
        if peak is None or ret > peak:
            peak, peak_at = ret, t
        _drag_ok_t = (drag_ceiling <= 0 or width <= 0
                      or (iv - _val) <= width * drag_ceiling)
        if ret >= target and _drag_ok_t:
            return "TARGET", ret, qty
        # THE UNDERLYING STOP, ahead of the mark stop because it is the
        # cleaner signal: the position is out of the money and the only way
        # back is a move in the underlying, not a re-quote of the spread.
        if otm_min >= 0 and (was_itm or not otm_needs_itm):
            if iv <= otm_floor:
                if otm_since is None:
                    otm_since = t
                elif (t - otm_since).total_seconds() / 60.0 >= otm_min:
                    return "OTM", ret, qty
            else:
                otm_since = None
        if peak_iv is None or iv > peak_iv:
            peak_iv = iv
        if gb_pct > 0 and width > 0 and peak_iv is not None and peak_iv > entry:
            if (peak_iv - iv) >= width * gb_pct / 100.0:
                if gb_since is None:
                    gb_since = t
                elif (t - gb_since).total_seconds() / 60.0 >= gb_confirm:
                    return "GIVEBACK", ret, qty
            else:
                gb_since = None
        else:
            gb_since = None
        if peak and peak > 0:
            quiet = (t - peak_at).total_seconds() / 60.0
            # STALL_MIN_GAIN_PCT: the close must realise something worth
            # taking, measured on the MARK where it is actually paid.
            _gain_ok = (entry <= 0) or ((_val - entry) / entry * 100.0) >= min_gain
            # ORPHAN_MAX_DRAG_WIDTH: and must not forfeit intrinsic to do it.
            _drag_ok = (drag_ceiling <= 0 or width <= 0
                        or (iv - _val) <= width * drag_ceiling)
            if (quiet >= stall_min and ret <= peak * (1 - giveback / 100.0)
                    and _gain_ok and _drag_ok):
                return "STALL", ret, qty
        if slow_stop < 0:
            if ret <= slow_stop:
                if slow_since is None:
                    slow_since = t
                elif (t - slow_since).total_seconds() / 60.0 >= slow_min:
                    return "SLOWSTOP", ret, qty
            else:
                slow_since = None
        if ret <= stop:
            if stop_since is None:
                stop_since = t
            elif (t - stop_since).total_seconds() / 60.0 >= confirm:
                return "STOP", ret, qty
        else:
            stop_since = None
    return "HELD", marks[-1][2], marks[-1][3]


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--all"]
    every = "--all" in sys.argv
    day = "" if every else (args[0] if args else date.today().isoformat())
    only = args[1] if len(args) > 1 else ""
    series = load(day, only)
    closes = _closes()
    if not series:
        print(f"No ORPHAN debit marks found for {day or 'any session'}"
              f"{' / ' + only.upper() if only else ''}.")
        print("The engine only logs these while a position is open.")
        return

    days = sorted({k[0] for k in series})
    print(f"\n{len(series)} position(s) with logged marks across "
          f"{len(days)} session(s): {days[0]} to {days[-1]}"
          f"{'  -- ' + only.upper() if only else ''}\n")

    print(f"{'config':<42}{'total $':>10} {'fires':>26}   held")
    print("-" * 92)
    for cfg in CONFIGS:
        name, tgt, stop, cf, sm, gb, of, om = cfg[:8]
        needs = cfg[8] if len(cfg) > 8 else False
        gbp = cfg[9] if len(cfg) > 9 else 0.0
        mg = cfg[10] if len(cfg) > 10 else 0.0
        dc = cfg[11] if len(cfg) > 11 else 0.0
        ss = cfg[12] if len(cfg) > 12 else 0.0
        sm2 = cfg[13] if len(cfg) > 13 else 30.0
        total, held, why_n = 0.0, 0, defaultdict(int)
        for (_d, _s, _r, _k, entry, _exp), marks in sorted(series.items()):
            try:
                _long, _short = (float(x) for x in _k.split("/"))
                w = abs(_short - _long)
            except ValueError:
                _long = _short = 0.0
                w = 0.0
            why, ret, qty = run(marks, tgt, stop, cf, sm, gb, of, om, needs,
                                gbp, 5.0, w, entry, mg, dc, ss, sm2)
            if why == "HELD":
                # ONLY 0DTE CAN BE SETTLED. A later expiry does not end with
                # this session, so neither its close nor its 15:45 price says
                # what the position was worth -- it carries on into tomorrow.
                # Settling one anyway priced a SNDK weekly off nine different
                # sessions' closes and let a 19-point last-quarter-hour move on
                # a 45-wide spread swing the whole sweep by tens of thousands.
                # Those stay HELD at the last mark, and the count is printed so
                # the remaining bias is visible rather than assumed away.
                settled = (None if _exp else
                           settle(_s, _d, _r, _long, _short, entry, closes))
                if settled is not None:
                    ret, why_n["SETTLED"] = settled, why_n["SETTLED"] + 1
                else:
                    why_n["HELD"] += 1
                    held += 1
            else:
                why_n[why] += 1
            total += entry * (ret / 100.0) * qty * 100
        tag = {"TARGET": "T", "OTM": "O", "STALL": "L",
               "STOP": "P", "HELD": "H", "GIVEBACK": "G",
               "SETTLED": "X", "SLOWSTOP": "W"}
        mix = " ".join(f"{tag.get(k, k[0])}{v}"
                       for k, v in sorted(why_n.items()))
        print(f"{name:<42}{total:>+10.0f} {mix:>26}   {held}")

    print("\nT=target  O=underlying-OTM  L=stall  P=stop  H=held to last mark.")
    print("UND rows add the underlying stop: close when intrinsic sits at or "
          "below the floor for N minutes -- the position is out of the money "
          "and only the UNDERLYING can bring it back.")
    print("HELD is scored at the last mark seen, which understates patient "
          "rules. DEBIT spreads only: intrinsic inverts on a credit.")


if __name__ == "__main__":
    main()
