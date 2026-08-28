"""Append one option-chain snapshot per run to a JSONL file.

SCHEMA CHANGED 2026-08-26. Lines written before that date are QQQ-only and
carry `spot` and `expiries` at the top level. Lines written after carry a
`symbols` list, each entry with its own spot and expiries. A reader can tell
them apart by the presence of the "symbols" key; both are kept because the
early lines are still valid QQQ observations.

Why this exists
---------------
trading_engine/chain_pricer.py prices verticals off a fitted volatility
surface. Its smile came from 1,944 marks the live engine logged, all of them
0DTE, so it had no tenor dimension at all -- and priced a four-day condor at
0.79x the market as a result. The term-structure correction that fixes that
was fitted on ONE afternoon's chain, 1,006 quoted strikes across 8 expiries,
which is enough to be better than nothing and not enough to trust.

The ATM term structure that afternoon was not even monotone -- 0.1698 at 0.8
days, 0.2099 at 3.8, 0.1815 at 6.8 -- which is an event sitting inside the
near expiries rather than a curve. Refitting needs snapshots taken across
many days and several regimes, and there is no historical option-chain feed
to get them from retroactively. So they have to be collected forward, which
is the same reason weekly_shadow.py and the shadow condor exist.

    python scripts/capture_chain.py                # append one snapshot
    python scripts/capture_chain.py --expiries 8   # look further out

Storage
-------
JSONL, one snapshot per line, at CHAIN_SNAPSHOT_PATH. Deliberately not a
database table: this is calibration input for an offline fit, nothing reads
it during a trading cycle, and a file costs no migration and no schema
decision made before anyone knows what the fit will need.

Only QUOTED strikes are kept -- bid > 0, 0.03 <= |delta| <= 0.97, and a
sane IV band. Without those filters the deep wings dominate by count and
their mid_iv is model noise rather than a price: including them put the
first fit at R^2 0.24 with an implied at-the-money vol of 158%.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NY = ZoneInfo("America/New_York")
BASE = "https://api.tradier.com/v1"

# Default under the repo, NOT /var/log.
#
# The engine runs as `docker compose exec app`, and only /opt/fastapi is bind
# mounted (to /app). A file written to /var/log inside the container lands on
# the container's own writable layer and is destroyed by the next
# `compose up -d` -- which happens on every deploy. The first run of this
# script did exactly that: it reported 686 strikes written and left nothing
# on the host. Anchoring on __file__ puts it inside the mount, so the same
# path works in the container, on the host, and on a laptop.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_PATH = os.getenv(
    "CHAIN_SNAPSHOT_PATH", os.path.join(REPO_ROOT, "data", "qqq-chain-snapshots.jsonl")
)

# Quality filters. See the docstring -- these are the difference between a
# usable fit and one dominated by strikes nobody would trade.
# MAX_IV was 0.60, tuned when only QQQ was captured. That silently drops
# every strike on the high-IV names -- DELL quotes 1.02 at the money and
# CRWV 0.82 -- so the filter would have recorded nothing for exactly the
# symbols added to study. 2.50 keeps them and still rejects the deep-wing
# mid_iv noise the filter exists for.
MIN_IV, MAX_IV = 0.05, 2.50
MIN_ABS_DELTA, MAX_ABS_DELTA = 0.03, 0.97

# Symbols captured each run. QQQ is the engine's instrument; the rest are
# candidates for a single-name book and are here only to build a forward
# record -- nothing trades them.
#
# Split deliberately by implied vol, measured 2026-08-26 at the money:
#   high    DELL 1.022  CRWV 0.823  WDC 0.730  STX 0.698  MU 0.604
#   low     META 0.355  AMZN 0.252  GOOGL 0.241  MSFT 0.222
#   ref     QQQ  0.174
#
# STRIKE SPACING MATTERS MORE THAN IV for a credit spread's risk:reward. QQQ
# and CRWV quote $1 strikes, so a $2 wing is available; MU, STX, WDC, DELL
# and META are on $5 spacing, which forces a $5 wing and mechanically halves
# the credit-to-risk ratio whatever the premium looks like.
CAPTURE_SYMBOLS = [s.strip().upper() for s in os.getenv(
    "CHAIN_CAPTURE_SYMBOLS",
    "QQQ,SNDK,MU,CRWV,STX,WDC,DELL,META,MSFT,GOOGL,AMZN,MRVL"
).split(",") if s.strip()]


def _headers() -> dict:
    key = os.getenv("TRADIER_API_KEY")
    if not key:
        raise SystemExit("TRADIER_API_KEY is not set.")
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _get(client: httpx.Client, path: str, params: dict) -> dict:
    r = client.get(BASE + path, params=params, headers=_headers(), timeout=30.0)
    r.raise_for_status()
    return r.json()


def _quote(client: httpx.Client, symbol: str) -> dict:
    q = (_get(client, "/markets/quotes", {"symbols": symbol}).get("quotes") or {}).get("quote")
    if isinstance(q, list):
        q = q[0] if q else None
    return q or {}


def capture(max_expiries: int = 6, symbols=None) -> dict:
    """One snapshot: every symbol in CAPTURE_SYMBOLS, every near expiry."""
    symbols = symbols or CAPTURE_SYMBOLS
    with httpx.Client() as client:
        vix_raw = _quote(client, "VIX").get("last")
        vix = float(vix_raw) if vix_raw is not None else None
        now = datetime.now(NY)
        snapshot = {"ts": now.isoformat(), "vix": vix, "symbols": []}

        for sym in symbols:
            try:
                spot = float(_quote(client, sym).get("last") or 0.0)
            except Exception:
                continue
            if spot <= 0:
                continue
            exps = (_get(client, "/markets/options/expirations",
                         {"symbol": sym}).get("expirations") or {}).get("date") or []
            if isinstance(exps, str):
                exps = [exps]
            per_sym = {"symbol": sym, "spot": spot, "expiries": []}

            for exp in exps[:max_expiries]:
                d = date.fromisoformat(exp)
                minutes = (datetime(d.year, d.month, d.day, 16, 15,
                                    tzinfo=NY) - now).total_seconds() / 60.0
                if minutes <= 0:
                    continue
                try:
                    chain = (_get(client, "/markets/options/chains",
                                  {"symbol": sym, "expiration": exp, "greeks": "true"})
                             .get("options") or {}).get("option") or []
                except Exception:
                    continue
                rows = []
                for o in chain:
                    g = o.get("greeks") or {}
                    iv = g.get("mid_iv") or g.get("smv_vol")
                    delta, bid, ask = g.get("delta"), o.get("bid"), o.get("ask")
                    if iv is None or delta is None or not bid:
                        continue
                    iv, delta = float(iv), float(delta)
                    if not (MIN_IV <= iv <= MAX_IV):
                        continue
                    if not (MIN_ABS_DELTA <= abs(delta) <= MAX_ABS_DELTA):
                        continue
                    rows.append([o["strike"], o["option_type"][0], round(float(bid), 3),
                                 round(float(ask or 0.0), 3), round(iv, 5), round(delta, 5)])
                if rows:
                    per_sym["expiries"].append(
                        {"exp": exp, "minutes": round(minutes, 1), "n": len(rows),
                         # [strike, c|p, bid, ask, iv, delta]
                         "rows": rows})
            if per_sym["expiries"]:
                snapshot["symbols"].append(per_sym)
    return snapshot


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--expiries", type=int, default=6,
                    help="how many expirations forward to capture (default 6)")
    ap.add_argument("--path", default=SNAPSHOT_PATH)
    args = ap.parse_args()

    snap = capture(args.expiries)
    total = sum(e["n"] for sy in snap["symbols"] for e in sy["expiries"])
    if not total:
        # Outside market hours the chain still answers but nothing is quoted,
        # so an empty snapshot is normal and not worth a line in the file.
        print(f"{snap['ts']}  no quoted strikes — not written (market shut?)")
        return
    os.makedirs(os.path.dirname(os.path.abspath(args.path)), exist_ok=True)
    with open(args.path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap, separators=(",", ":")) + "\n")
    print(f"{snap['ts']}  vix {snap['vix']}  {len(snap['symbols'])} symbols  "
          f"{total} quoted strikes -> {args.path}")


if __name__ == "__main__":
    main()
