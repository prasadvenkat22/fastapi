"""Daily closes for every (symbol, session) the engine ever marked, cached.

WHY THIS EXISTS. exit_backtest.py scores a HELD run -- one where the rule
would have held past the real exit -- at the LAST OBSERVED MARK, because there
is no data beyond it. That biases hard against patient rules, and the bias is
not small: across 182 positions, between 82 and 128 runs end HELD depending on
the configuration, and the rules most worth testing are precisely the ones
that hold more.

    LIVE (no guards)          -24,784    82 of 182 HELD
    LIVE NOW, no OTM stop     -27,357   128 of 182 HELD

Most of that 2,573 is the artifact, not the rules. 128 truncations against 82
is not a fair comparison and no amount of re-running fixes it.

THE FIX IS THAT A 0DTE HELD RUN HAS A KNOWN ANSWER. The position expires the
same session, so what it was "worth if held" is not a guess -- it is the
intrinsic at the underlying's CLOSE, which is a fact that can be fetched:

    call debit:  clamp(close - long_strike, 0, width)
    put  debit:  clamp(long_strike - close, 0, width)

Fetched once per (symbol, date) and cached to JSON, because the sweep re-runs
constantly and the closes never change.

RUNS ON THE HOST, with stdlib only. The engine log lives on the host and is
not visible inside the container, so the backtest cannot run there; and the
host has no httpx, no trading_engine and no venv. urllib and json are enough,
and the token is read from the same .env.production the container uses.

    python3 scripts/expiry_closes.py          # fill the cache
    python3 scripts/expiry_closes.py --show   # print what is cached
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

LOG_GLOB = "/var/log/qqq-trading.log*"
ENV_PATH = "/opt/fastapi/.env.production"
CACHE = "/opt/fastapi/.expiry_closes.json"
FLATTEN_ET = "15:45"

# Only the symbol and the date matter here; the strikes are the backtest's job.
RX = re.compile(
    r"^(\d{4}-\d\d-\d\d) \d\d:\d\d:\d\d,\d+ INFO ORPHAN ([A-Z]+) [CP] ")


def _token() -> str:
    """TRADIER_API_KEY from the deployed env file.

    Read rather than passed so this cannot drift from what the engine uses,
    and never echoed -- the Polygon key is already sitting in a log in
    plaintext because a URL got logged at INFO, and that is one too many.
    """
    try:
        with open(ENV_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("TRADIER_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return os.getenv("TRADIER_API_KEY", "")


def needed() -> dict:
    """{symbol: {dates}} for everything the ORPHAN log ever marked."""
    out = defaultdict(set)
    for fn in sorted(glob.glob(LOG_GLOB)):
        opener = gzip.open if fn.endswith(".gz") else open
        try:
            fh = opener(fn, "rt", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                m = RX.match(line)
                if m:
                    out[m.group(2)].add(m.group(1))
    return {k: sorted(v) for k, v in out.items()}


def load_cache() -> dict:
    try:
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def fetch(symbol: str, start: str, end: str, token: str) -> dict:
    """{date: close} from Tradier's daily history, or {} on any failure.

    One request per symbol for the whole range rather than one per session.
    A failure returns nothing and the caller keeps whatever is cached -- a
    missing close must leave a run scored as before, never scored wrongly.
    """
    q = urllib.parse.urlencode(
        {"symbol": symbol, "interval": "daily", "start": start, "end": end})
    req = urllib.request.Request(
        "https://api.tradier.com/v1/markets/history?" + q,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as exc:
        print(f"  {symbol}: {type(exc).__name__} -- left uncached")
        return {}
    days = ((data.get("history") or {}) or {}).get("day") or []
    if isinstance(days, dict):
        days = [days]
    out = {}
    for d in days:
        try:
            out[d["date"]] = float(d["close"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_flatten(symbol: str, day: str, token: str) -> "float | None":
    """The underlying at 15:45 ET, which is when the engine actually exits.

    WHY NOT THE CLOSE. exit_backtest settles a HELD 0DTE run at the session
    close, but the engine FORCE_CLOSEs 0DTE at 15:45 -- it never holds to
    16:00. Scoring at the close charges a held run fifteen minutes of movement
    the engine would never have taken, which biases against patient rules in
    exactly the direction the truncation fix was built to remove.

    Stored alongside the close so a session that has one and not the other
    still settles, on the close, rather than not at all.
    """
    q = urllib.parse.urlencode({
        "symbol": symbol, "interval": "1min",
        "start": f"{day} {FLATTEN_ET}", "end": f"{day} {FLATTEN_ET}",
        "session_filter": "open"})
    req = urllib.request.Request(
        "https://api.tradier.com/v1/markets/timesales?" + q,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    rows = ((data.get("series") or {}) or {}).get("data") or []
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        try:
            return float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def main() -> None:
    cache = load_cache()
    if "--show" in sys.argv:
        for sym in sorted(cache):
            for day in sorted(cache[sym]):
                print(f"  {sym:<6} {day}  {cache[sym][day]:>10.2f}")
        print(f"\n{sum(len(v) for v in cache.values())} close(s) cached "
              f"across {len(cache)} symbol(s)")
        return

    token = _token()
    if not token:
        print("No TRADIER_API_KEY found -- cannot fetch.")
        return

    want = needed()
    print(f"{len(want)} symbol(s) seen in the ORPHAN log")
    added = 0
    for sym, days in sorted(want.items()):
        have = cache.get(sym) or {}
        missing = [d for d in days if d not in have]
        got = {}
        if missing:
            got = fetch(sym, min(missing), max(missing), token)
            if got:
                cache.setdefault(sym, {}).update(got)
                added += len([d for d in missing if d in got])
        # Every session with a close but no flatten price, new or not.
        have = cache.get(sym) or {}
        need_flat = [d for d in days
                     if d in have and (d + "@flatten") not in have]
        flat = 0
        for d in need_flat:
            px = fetch_flatten(sym, d, token)
            if px is not None:
                cache[sym][d + "@flatten"] = px
                flat += 1
        if got or flat:
            print(f"  {sym:<6} {len(got)} close(s), {flat} flatten price(s)")
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=1, sort_keys=True)
    print(f"\n{added} new close(s); {sum(len(v) for v in cache.values())} "
          f"cached total -> {CACHE}")


if __name__ == "__main__":
    main()
