"""Edge test for the single-stock 0DTE book (scripts/dte0_trade.py). Read-only.

Inputs (pulled from the droplet read-only into scratch/edge/):
  dte0_plan.txt      plan lines (" xN @ ... Pwin ...") + ORDER SENT lines from /var/log/dte0-trade.log
  edge_orders.jsonl  broker GET /orders/{id} for every ORDER SENT id (exact legs, status, fill)
  shadow.csv         dte0_shadow (paper book, both sides, chain-priced, settled from close)
Prices: yfinance daily closes (3y, as weekly_pick uses) and 5-min bars (spot at entry).

    python scripts/edge_stock0dte.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

D = Path(__file__).resolve().parent.parent / "scratch" / "edge"
RNG = np.random.default_rng(7)
SYMS = ["NVDA", "TSLA", "AAPL", "AMZN", "MSFT", "META", "GOOGL", "AVGO", "MU", "INTC"]

_daily: dict = {}
_intra: dict = {}


def daily(sym: str) -> pd.Series:
    if sym not in _daily:
        h = yf.Ticker(sym).history(period="3y", interval="1d")["Close"]
        h.index = h.index.tz_localize(None).normalize()
        _daily[sym] = h
    return _daily[sym]


def intra(sym: str) -> pd.Series:
    if sym not in _intra:
        h = yf.Ticker(sym).history(period="60d", interval="5m")["Close"]
        h.index = h.index.tz_convert("UTC")
        _intra[sym] = h
    return _intra[sym]


def spot_at(sym: str, ts: datetime) -> float:
    s = intra(sym)
    s = s[s.index <= ts]
    return float(s.iloc[-1])


def close_on(sym: str, day: str) -> float:
    return float(daily(sym).loc[pd.Timestamp(day)])


# ---------------------------------------------------------------- picks
def load_picks() -> pd.DataFrame:
    lines = (D / "dte0_plan.txt").read_text(encoding="utf-8", errors="replace").splitlines()
    orders = [json.loads(l) for l in (D / "edge_orders.jsonl").read_text().splitlines()]
    plan_re = re.compile(r"^(\S+ \S+) (\w+)\s+(CALL|PUT)\s+\S+ w([\d.]+) x(\d+) @ ([\d.]+) .*"
                         r"Pwin ([\d.]+)% need ([\d.]+)% EV \$([+-]?\d+)")
    rows, pending = [], None
    for l in lines:
        m = plan_re.match(l)
        if m:
            pending = m
            continue
        if "ORDER SENT" in l and pending:
            oid = int(re.search(r"'id': (\d+)", l).group(1))
            o = next(x for x in orders if x["id"] == oid)
            legs = o["leg"] if isinstance(o["leg"], list) else [o["leg"]]
            k = {lg["side"]: float(re.search(r"[CP](\d{8})$", lg["option_symbol"]).group(1)) / 1000
                 for lg in legs}
            ts, sym, side, w, qty, cost, pw, need, ev = pending.groups()
            rows.append(dict(ts=datetime.strptime(ts.split(",")[0], "%Y-%m-%d %H:%M:%S")
                             .replace(tzinfo=timezone.utc), day=ts[:10], sym=sym, side=side,
                             long=k["buy_to_open"], short=k["sell_to_open"], w=float(w),
                             qty=int(qty), cost=float(cost), pwin=float(pw) / 100,
                             need=float(need) / 100, ev=float(ev), status=o["status"],
                             fill=float(o.get("avg_fill_price") or 0)))
            pending = None
    p = pd.DataFrame(rows)
    out = []
    for r in p.itertuples():
        s0, c = spot_at(r.sym, r.ts), close_on(r.sym, r.day)
        if r.side == "CALL":
            val = min(max(c - r.long, 0), r.w)
            be = r.long + r.cost
            hit = c > be
            dirn = np.sign(c - s0)
        else:
            val = min(max(r.long - c, 0), r.w)
            be = r.long - r.cost
            hit = c < be
            dirn = -np.sign(c - s0)
        out.append(dict(spot=s0, close=c, be=be, hit=bool(hit), dir_ok=dirn > 0,
                        val=val, ret=val / r.cost - 1))
    return pd.concat([p, pd.DataFrame(out)], axis=1)


# ---------------------------------------------------------------- shadow null
def load_shadow() -> pd.DataFrame:
    s = pd.read_csv(D / "shadow.csv").dropna(subset=["symbol"])
    s = s[s.symbol.isin(SYMS)]
    s["trading_day"] = s.trading_day.astype(str)
    s["ret_nat"] = s.expiry_value / s.entry_natural - 1
    s["ret_mid"] = s.expiry_value / s.entry_mid - 1
    return s


def null_percentile(picks: pd.DataFrame, shadow: pd.DataFrame, n: int = 2000):
    deb = shadow[(shadow.structure == "DEBIT") & (shadow.entry_natural > 0.05)]
    pools = {d: g.ret_nat.values for d, g in deb.groupby("trading_day")}
    pk = picks[picks.day.isin(pools)]
    actual = pk.ret.mean()
    sims = np.array([np.mean([RNG.choice(pools[d]) for d in pk.day]) for _ in range(n)])
    return actual, sims, len(pk)


# ---------------------------------------------------------------- model calibration at scale
def pwin_model_test():
    """The ranker uses fwd_days = max(n,1) = 1 for a same-day expiry, i.e. the
    FULL close-to-close move distribution, whatever the time of entry. Test
    P_model(close beyond level) vs realised on every name, every 5-min-covered
    session, at 10:00/11:30/13:00 ET, for levels 0.25 and 0.5 daily-sigma
    beyond spot either way (where the book's break-evens sit)."""
    recs = []
    for sym in SYMS:
        dly, ib = daily(sym), intra(sym)
        days = sorted({t.date() for t in ib.index})
        for d in days:
            d_ts = pd.Timestamp(d)
            hist = dly[dly.index < d_ts]
            if len(hist) < 300 or d_ts not in dly.index:
                continue
            lr = np.log(hist.values[1:] / hist.values[:-1])
            dem = np.exp(lr - lr.mean())
            sd = lr.std()
            c = float(dly.loc[d_ts])
            for hhmm in ("14:00", "15:30", "17:00"):     # UTC = 10:00, 11:30, 13:00 ET (EDT)
                t = pd.Timestamp(f"{d} {hhmm}", tz="UTC")
                seg = ib[(ib.index <= t) & (ib.index.date == d)]
                if seg.empty:
                    continue
                s0 = float(seg.iloc[-1])
                for k in (0.25, 0.5):
                    for sgn in (1, -1):
                        lvl = s0 * (1 + sgn * k * sd)
                        pm = float(((s0 * dem > lvl) if sgn > 0 else (s0 * dem < lvl)).mean())
                        hit = (c > lvl) if sgn > 0 else (c < lvl)
                        recs.append(dict(sym=sym, day=str(d), t=hhmm, k=k, p=pm, hit=hit))
    return pd.DataFrame(recs)


def main():
    pd.set_option("display.width", 200)
    picks = load_picks()
    print("=== engine orders (all ORDER SENT) ===")
    print(picks[["day", "sym", "side", "long", "short", "cost", "pwin", "need", "ev", "status",
                 "fill", "spot", "close", "hit", "ret"]].round(3).to_string())
    u = picks.drop_duplicates(["day", "sym", "side", "long", "short"]).copy()
    print(f"\n{len(picks)} orders, {len(u)} unique spreads, {u.day.nunique()} sessions, "
          f"{(picks.status == 'filled').sum()} filled")
    for name, df in (("all orders", picks), ("unique", u), ("filled", picks[picks.status == "filled"])):
        print(f"{name:10s} n={len(df):2d} mean hold-to-expiry ret {df.ret.mean():+.1%}  "
              f"BE-hit {df.hit.mean():.0%}  mean Pwin {df.pwin.mean():.0%}  mean need {df.need.mean():.0%}  "
              f"direction right {df.dir_ok.mean():.0%}")
    print("\nby day (unique):")
    print(u.groupby("day").agg(n=("ret", "size"), ret=("ret", "mean"), hit=("hit", "mean"),
                               pwin=("pwin", "mean")).round(3))
    print("\ncorr(EV, realised ret) unique: pearson %.2f spearman %.2f" %
          (u.ev.corr(u.ret), u.ev.corr(u.ret, method="spearman")))
    print("corr(Pwin-need, ret) unique: spearman %.2f" %
          ((u.pwin - u.need).corr(u.ret, method="spearman")))

    shadow = load_shadow()
    a, sims, n = null_percentile(u, shadow)
    print(f"\n=== null: random same-day shadow DEBIT (natural entry, settled) n={n} ===")
    print(f"picks mean {a:+.1%}  null mean {sims.mean():+.1%}  null 5-95% "
          f"[{np.percentile(sims, 5):+.1%}, {np.percentile(sims, 95):+.1%}]  "
          f"percentile of picks {(sims < a).mean():.0%}")
    a2, sims2, n2 = null_percentile(picks, shadow)
    print(f"all orders n={n2}: picks {a2:+.1%} percentile {(sims2 < a2).mean():.0%}")

    deb = shadow[shadow.structure == "DEBIT"]
    print("\nshadow DEBIT rows by day: mean ret at natural / at mid, n")
    print(deb.groupby("trading_day").agg(n=("ret_nat", "size"), nat=("ret_nat", "mean"),
                                         mid=("ret_mid", "mean")).round(3))
    print("shadow ALL debit: nat %+.1f%% mid %+.1f%% n=%d; entry nat vs mid cost %.1f%%" % (
        deb.ret_nat.mean() * 100, deb.ret_mid.mean() * 100, len(deb),
        ((deb.entry_natural / deb.entry_mid - 1).median()) * 100))

    m = pwin_model_test()
    print(f"\n=== ranker probability model vs realised, {len(m)} obs, "
          f"{m.day.nunique()} sessions, {m.sym.nunique()} names ===")
    m["half"] = np.where(m.day < sorted(m.day.unique())[len(m.day.unique()) // 2], "H1", "H2")
    print(m.groupby(["k", "t"]).agg(n=("hit", "size"), p_model=("p", "mean"),
                                    realised=("hit", "mean")).round(3))
    print(m.groupby(["half", "k"]).agg(n=("hit", "size"), p_model=("p", "mean"),
                                       realised=("hit", "mean")).round(3))
    m["b"] = pd.cut(m.p, [0, .2, .3, .35, .4, .45, .5, 1])
    print(m.groupby("b", observed=True).agg(n=("hit", "size"), p_model=("p", "mean"),
                                            realised=("hit", "mean")).round(3))
    u["b"] = pd.cut(u.pwin, [0, .47, .49, 1])
    print("\npicks calibration (unique):")
    print(u.groupby("b", observed=True).agg(n=("hit", "size"), pwin=("pwin", "mean"),
                                            need=("need", "mean"), hit=("hit", "mean")).round(3))


if __name__ == "__main__":
    main()
