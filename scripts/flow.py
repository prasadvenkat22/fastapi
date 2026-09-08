"""Was this name bought or sold today? Net signed volume and VWAP, any symbol.

    python scripts/flow.py --symbols SNDK,NVDA,MU,QQQ
    python scripts/flow.py --symbols SNDK --bars          # per-bar detail
    python scripts/flow.py --symbols SNDK --day 2026-09-04

WHY TRADIER AND NOT yfinance. /markets/timesales returns intraday bars with a
correct volume AND a per-bar vwap, so the session figure is the volume-weighted
mean of bars rather than a reconstruction from typical prices. yfinance gives
hourly bars at best, which is too coarse to sign volume usefully.

WHAT NET SIGNED VOLUME IS. Volume on up-bars minus volume on down-bars -- the
tick rule, applied at bar resolution. It approximates buy-initiated against
sell-initiated flow, and the approximation is the point to keep in mind:

    THE PROPER CLASSIFICATION IS TRADE PRICE AGAINST THE QUOTE -- at the ask is
    a buy, at the bid is a sell (Lee-Ready). Comparing a bar's close to the
    previous close is a cruder proxy that misreads fast tape, and it cannot
    classify the first bar of a session at all because there is no previous
    close. Here the opening bar is signed by close against its own OPEN, which
    is the least bad answer, and the report says how much volume that was.

AND WHAT IT IS NOT. It does not identify institutions. Every buyer has a
seller. What it measures is which side was URGENT -- who paid up rather than
waiting -- which is the trace a large order worked against a VWAP benchmark
leaves behind, and is not a count of anyone's shares (section 128).

READ IT WITH THE VWAP LINE, NEVER ALONE. Signed volume can run positive while
price fails to hold above VWAP, which is a different and weaker picture than
both agreeing. SNDK on 2026-09-08 was exactly that: 71% up-volume with price
0.18% BELOW a flat VWAP, against 2026-09-04 when VWAP rose 2.56% and not one
bar closed beneath it.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NY = ZoneInfo("America/New_York")


def _base() -> str:
    env = os.getenv("TRADIER_ENV", "sandbox").lower()
    root = ("https://api.tradier.com/v1" if env == "production"
            else "https://sandbox.tradier.com/v1")
    return root + "/markets/timesales"


def bars(symbol: str, day: str, interval: str) -> list:
    key = os.getenv("TRADIER_API_KEY")
    if not key:
        return []
    try:
        r = httpx.get(_base(),
                      params={"symbol": symbol, "interval": interval,
                              "start": f"{day} 09:30", "end": f"{day} 16:00",
                              "session_filter": "open"},
                      headers={"Authorization": f"Bearer {key}",
                               "Accept": "application/json"},
                      timeout=20.0)
        r.raise_for_status()
        d = (r.json().get("series") or {}).get("data") or []
        return [d] if isinstance(d, dict) else d
    except Exception as exc:
        print(f"  {symbol}: timesales failed, {exc}")
        return []


def adv(symbol: str) -> "float | None":
    try:
        import yfinance as yf

        h = yf.Ticker(symbol).history(period="3mo", interval="1d")
        if len(h) < 21:
            return None
        return float(h["Volume"].astype(float).tail(20).mean())
    except Exception:
        return None


def analyse(symbol: str, day: str, interval: str, show_bars: bool) -> dict:
    rows = bars(symbol, day, interval)
    if not rows:
        return {}
    num = den = up = dn = 0.0
    above = total = 0
    first_vol = 0.0
    prev = None
    run_first = None
    detail = []
    for b in rows:
        v = float(b.get("volume") or 0)
        w = float(b.get("vwap") or 0)
        c = float(b.get("close") or 0)
        o = float(b.get("open") or 0)
        if v <= 0 or w <= 0 or c <= 0:
            continue
        num += w * v
        den += v
        run = num / den
        if run_first is None:
            run_first = run
        # First bar has no previous close, so sign it against its own open.
        ref = prev if prev is not None else (o or None)
        if prev is None:
            first_vol = v
        if ref is not None:
            if c > ref:
                up += v
            elif c < ref:
                dn += v
        total += 1
        if c > run:
            above += 1
        detail.append((b.get("time", "")[11:16], c, w, run, v,
                       (v if (ref is not None and c > ref) else
                        (-v if (ref is not None and c < ref) else 0.0))))
        prev = c
    if not den or run_first is None:
        return {}

    if show_bars:
        print(f"  {'time':6s} {'close':>9s} {'bar vwap':>9s} {'run vwap':>9s} "
              f"{'vol':>10s} {'signed':>10s}")
        for t, c, w, run, v, sg in detail:
            print(f"  {t:6s} {c:9.2f} {w:9.2f} {run:9.2f} {v:10.0f} {sg:+10.0f}")

    a = adv(symbol)
    return dict(
        vwap=num / den, last=prev, slope=(num / den / run_first - 1) * 100,
        vol=den, up=up, dn=dn, net=up - dn,
        above_pct=above / total * 100 if total else 0.0,
        bars=total, first_vol=first_vol,
        vs_adv=(den / a) if a else None,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--day", default="", help="YYYY-MM-DD, default today (ET)")
    ap.add_argument("--interval", default="5min",
                    choices=("1min", "5min", "15min"),
                    help="finer bars sign volume better and cost more calls")
    ap.add_argument("--bars", action="store_true", help="print every bar")
    args = ap.parse_args()
    day = args.day or datetime.now(NY).strftime("%Y-%m-%d")
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"INTRADAY FLOW  {day}  ({args.interval} bars, "
          f"{datetime.now(NY):%H:%M %Z})")
    print(f"{'sym':6s} {'vwap':>9s} {'last':>9s} {'vs vwap':>8s} {'slope':>7s} "
          f"{'above':>6s} {'up%':>5s} {'net signed':>12s} {'vol/ADV':>8s}")
    for s in syms:
        if args.bars:
            print(f"\n{s}")
        r = analyse(s, day, args.interval, args.bars)
        if not r:
            print(f"{s:6s} (no bars)")
            continue
        upshare = r["up"] / (r["up"] + r["dn"]) * 100 if (r["up"] + r["dn"]) else 0.0
        print(f"{s:6s} {r['vwap']:9.2f} {r['last']:9.2f} "
              f"{(r['last']/r['vwap']-1)*100:+7.2f}% {r['slope']:+6.2f}% "
              f"{r['above_pct']:5.0f}% {upshare:4.0f}% {r['net']:+12.0f} "
              f"{(r['vs_adv'] if r['vs_adv'] else float('nan')):7.2f}x")

    print("\nSIGNED VOLUME AND THE VWAP LINE HAVE TO AGREE. Positive net signed "
          "volume while price sits below a flat VWAP is buyers who are not "
          "winning -- a weaker picture than either number alone suggests. "
          "Price holding above a RISING vwap with net buying is the one that "
          "means something.")
    print("Neither identifies institutions: this measures urgency, not who "
          "(section 128). The first bar is signed against its own open "
          "because it has no previous close.")


if __name__ == "__main__":
    main()
