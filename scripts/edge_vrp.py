"""Variance risk premium and credit-spread rules measured on REAL captured chains.

Reads the chain snapshots written by scripts/capture_chain.py (4/day since
2026-08-24, QQQ-only lines first, then a "symbols" list) and settles every
position at the REAL 16:00 regular-session close of the underlying on the
expiry date (yfinance daily bar "Close", not adjusted). Read-only: nothing in
production is touched.

    ssh root@159.223.127.31 "cat /opt/fastapi/data/qqq-chain-snapshots.jsonl" \
        > scratch/chains/snapshots.jsonl
    env/Scripts/python.exe scripts/edge_vrp.py [--path scratch/chains/snapshots.jsonl]

Sections printed:
  1. VRP: ATM IV, realised/implied move ratio, short ATM straddle at the bid,
     by tenor bucket and by symbol, 10:05 and 15:30 separately.
  2. Credit spreads (put / call / condor, delta 0.10/0.20/0.30, width 1 or 2
     listed strikes), natural and mid pricing, weekly (3-8 cal days) and 0DTE,
     hold-to-expiry plus a -2x credit stop marked on the later snapshots.
  3. Market direction over the window and each half.
  4. IV - RV20 filter, threshold chosen on the first half, second half once.
Limits: ~6 weeks, one regime, overlapping expiries (t-stats are clustered by
expiry week), no early assignment / pin risk, stop marks only 4x/day.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

DEFAULT_PATH = r"C:\fastapi\scratch\chains\snapshots.jsonl"
MULT = 100.0


# ---------------------------------------------------------------- loading
@dataclass
class Chain:
    exp: date
    minutes: float
    rows: list  # [strike, c|p, bid, ask, iv, delta, ...]


@dataclass
class Snap:
    d: date
    label: str          # "10:05" etc.
    ts: datetime
    sym: str
    spot: float
    chains: dict = field(default_factory=dict)  # exp -> Chain


def load(path: str) -> list[Snap]:
    out: list[Snap] = []
    with open(path) as fh:
        for line in fh:
            j = json.loads(line)
            ts = datetime.fromisoformat(j["ts"])
            label = ts.strftime("%H:%M")
            syms = j["symbols"] if "symbols" in j else [
                {"symbol": "QQQ", "spot": j["spot"], "expiries": j["expiries"]}]
            for s in syms:
                sn = Snap(ts.date(), label, ts, s["symbol"], float(s["spot"]))
                for e in s["expiries"]:
                    sn.chains[date.fromisoformat(e["exp"])] = Chain(
                        date.fromisoformat(e["exp"]), float(e["minutes"]), e["rows"])
                out.append(sn)
    return out


def closes(symbols: list[str]) -> pd.DataFrame:
    df = yf.download(symbols, start="2026-06-01", end="2026-10-04",
                     auto_adjust=False, progress=False)["Close"]
    df.index = [i.date() for i in df.index]
    return df


# ---------------------------------------------------------------- chain helpers
def leg(ch: Chain, k: float, cp: str) -> Optional[list]:
    for r in ch.rows:
        if r[1] == cp and abs(r[0] - k) < 1e-9:
            return r
    return None


def atm_iv(ch: Chain, spot: float) -> Optional[float]:
    vals = []
    for cp in "cp":
        rs = sorted((r for r in ch.rows if r[1] == cp), key=lambda r: r[0])
        lo = [r for r in rs if r[0] <= spot]
        hi = [r for r in rs if r[0] > spot]
        if not lo or not hi:
            continue
        a, b = lo[-1], hi[0]
        w = (spot - a[0]) / (b[0] - a[0])
        vals.append(a[4] * (1 - w) + b[4] * w)
    return float(np.mean(vals)) if vals else None


def straddle(ch: Chain, spot: float):
    ks = sorted({r[0] for r in ch.rows if r[1] == "c"} & {r[0] for r in ch.rows if r[1] == "p"},
                key=lambda k: abs(k - spot))
    if not ks:
        return None
    k = ks[0]
    c, p = leg(ch, k, "c"), leg(ch, k, "p")
    if c[3] <= 0 or p[3] <= 0:
        return None
    return k, c[2] + p[2], (c[2] + c[3] + p[2] + p[3]) / 2


def trading_minutes(ts: datetime, exp: date, tdays: list[date]) -> float:
    """Session minutes left from ts to 16:00 on exp (trading calendar)."""
    today_left = max(0.0, (16 * 60) - (ts.hour * 60 + ts.minute))
    between = sum(1 for t in tdays if ts.date() < t <= exp)
    return today_left + 390.0 * between


# ---------------------------------------------------------------- spreads
@dataclass
class Spread:
    side: str      # "p" or "c"
    ks: float      # short strike
    kl: float      # long strike
    nat: float     # credit at natural
    mid: float     # credit at mid

    @property
    def width(self) -> float:
        return abs(self.ks - self.kl)

    def intrinsic(self, s: float) -> float:
        if self.side == "p":
            return min(max(self.ks - s, 0.0), self.width)
        return min(max(s - self.ks, 0.0), self.width)


def build_spread(ch: Chain, side: str, target: float, nstrikes: int) -> Optional[Spread]:
    rs = sorted((r for r in ch.rows if r[1] == side and r[3] > 0), key=lambda r: r[0])
    if not rs:
        return None
    sgn = -1 if side == "p" else 1
    short = min(rs, key=lambda r: abs(r[5] - sgn * target))
    i = rs.index(short)
    j = i - nstrikes if side == "p" else i + nstrikes
    if j < 0 or j >= len(rs):
        return None
    long = rs[j]
    nat = short[2] - long[3]
    mid = (short[2] + short[3]) / 2 - (long[2] + long[3]) / 2
    if nat <= 0:
        return None
    return Spread(side, short[0], long[0], nat, mid)


def mark(ch: Optional[Chain], sp: Spread, spot: float) -> tuple[float, float]:
    """(mid value, natural cost-to-close) of the spread on a later snapshot."""
    intr = sp.intrinsic(spot)
    if ch is None:
        return intr, intr
    a, b = leg(ch, sp.ks, sp.side), leg(ch, sp.kl, sp.side)
    if a is None or b is None:   # leg left the 0.03-0.97 band: use intrinsic
        return intr, intr
    m = (a[2] + a[3]) / 2 - (b[2] + b[3]) / 2
    n = a[3] - b[2]
    return min(max(m, 0.0), sp.width), min(max(n, 0.0), sp.width)


# ---------------------------------------------------------------- stats
def cluster_t(df: pd.DataFrame, col: str = "pnl") -> float:
    w = df.groupby("week")[col].sum()
    if len(w) < 2 or w.std(ddof=1) == 0:
        return float("nan")
    return float(w.mean() / w.std(ddof=1) * math.sqrt(len(w)))


def summarise(df: pd.DataFrame, col: str = "pnl") -> dict:
    if df.empty:
        return dict(n=0)
    w = df.groupby("week")[col].sum()
    return dict(n=len(df), win=round(100 * (df[col] > 0).mean()), credit=round(df["credit"].mean() * MULT),
                avg=round(df[col].mean()), worst=round(df[col].min()), worst_wk=round(w.min()),
                total=round(df[col].sum()), t=round(cluster_t(df, col), 2))


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=DEFAULT_PATH)
    a = ap.parse_args()
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 400)

    snaps = load(a.path)
    syms = sorted({s.sym for s in snaps})
    px = closes(syms)
    tdays = [d for d in px.index if d >= date(2026, 8, 24)]
    tset = set(tdays)
    last_settle = max(tdays)

    # first snapshot per (date, label, sym), trading days only
    by: dict = {}
    for s in sorted(snaps, key=lambda s: s.ts):
        if s.d in tset:
            by.setdefault((s.d, s.label, s.sym), s)
    snaps_by_sym = defaultdict(list)
    for s in by.values():
        snaps_by_sym[s.sym].append(s)
    for v in snaps_by_sym.values():
        v.sort(key=lambda s: s.ts)

    def entry_snap(d: date, sym: str) -> Optional[Snap]:
        """10:05 snapshot; if missing that day (2026-09-09) the earliest one."""
        if (d, "10:05", sym) in by:
            return by[(d, "10:05", sym)]
        c = [s for s in snaps_by_sym[sym] if s.d == d]
        return c[0] if c else None

    logret = np.log(px / px.shift(1))
    rv20 = logret.rolling(20).std() * math.sqrt(252)

    def rv_before(sym: str, d: date) -> float:
        prior = [x for x in px.index if x < d]
        return float(rv20[sym].loc[prior[-1]])

    def bucket(n: int) -> str:
        return "0DTE" if n == 0 else "1-2d" if n <= 2 else "3-5d" if n <= 5 else "6+d"

    # ------------------------------------------------ 1. VRP
    vrows = []
    for (d, label, sym), s in by.items():
        if label not in ("10:05", "15:30"):
            continue
        for exp, ch in s.chains.items():
            if exp > last_settle or exp not in tset:
                continue
            st = straddle(ch, s.spot)
            iv = atm_iv(ch, s.spot)
            if st is None or iv is None:
                continue
            k, bid, mid = st
            sT = float(px.loc[exp, sym])
            tm = trading_minutes(s.ts, exp, tdays)
            imp_iv = 0.7979 * s.spot * iv * math.sqrt(tm / (252 * 390))
            vrows.append(dict(sym=sym, d=d, label=label, exp=exp, dte=(exp - d).days,
                              bucket=bucket((exp - d).days), iv=iv, rv20=rv_before(sym, d),
                              real=abs(sT - s.spot), imp_mid=mid, imp_iv=imp_iv,
                              pnl=(bid - abs(sT - k)) * MULT, credit=bid,
                              week=exp.isocalendar()[1]))
    V = pd.DataFrame(vrows)
    print("\n=== 1. VRP: short ATM straddle at the bid, settled at 16:00 close ===")
    print("ratio = sum|S_T - S0| / sum straddle mid ;  ratio_iv uses E|move| = 0.798*S*IV*sqrt(trading min/98280)")
    for label in ("10:05", "15:30"):
        sub = V[V.label == label]
        for keycol in ("bucket", "sym"):
            g = sub.groupby(keycol).apply(lambda x: pd.Series(dict(
                n=len(x), iv=round(x.iv.mean(), 3), iv_rv20=round((x.iv - x.rv20).mean(), 3),
                ratio=round(x.real.sum() / x.imp_mid.sum(), 2),
                ratio_iv=round(x.real.sum() / x.imp_iv.sum(), 2),
                win=round(100 * (x.pnl > 0).mean()), avg=round(x.pnl.mean()),
                worst=round(x.pnl.min()), total=round(x.pnl.sum()),
                t_wk=round(cluster_t(x), 2))), include_groups=False)
            print(f"\n-- {label} by {keycol}")
            print(g.to_string())
        print(f"ALL {label}: n={len(sub)} ratio={sub.real.sum() / sub.imp_mid.sum():.2f} "
              f"total=${sub.pnl.sum():,.0f} t_wk={cluster_t(sub):.2f}")

    # ------------------------------------------------ 2. spreads
    entry_days = sorted({d for (d, _, _) in by})
    half_cut = entry_days[len(entry_days) // 2]
    trades = []
    for sym in syms:
        taken: set = set()
        for d in entry_days:
            s = entry_snap(d, sym)
            if s is None:
                continue
            for exp, ch in s.chains.items():
                dte = (exp - d).days
                mode = "0DTE" if dte == 0 else "weekly" if 3 <= dte <= 8 else None
                if mode is None or exp > last_settle or exp not in tset:
                    continue
                if (exp, mode) in taken:
                    continue          # one entry per symbol per expiry: the first day it qualifies
                taken.add((exp, mode))
                sT = float(px.loc[exp, sym])
                later = [x for x in snaps_by_sym[sym] if x.ts > s.ts and x.d <= exp]
                iv = atm_iv(ch, s.spot)
                for delta in (0.10, 0.20, 0.30):
                    for nk in (1, 2):
                        legs = {sd: build_spread(ch, sd, delta, nk) for sd in "pc"}
                        res = {}
                        for sd, sp in legs.items():
                            if sp is None:
                                continue
                            settle = sp.intrinsic(sT)
                            stop = {}
                            for px_mode, cr in (("nat", sp.nat), ("mid", sp.mid)):
                                stop[px_mode] = cr - settle
                                for x in later:
                                    m, n = mark(x.chains.get(exp), sp, x.spot)
                                    if m >= 3 * sp.mid:           # loss of 2x credit on the mid mark
                                        stop[px_mode] = cr - (n if px_mode == "nat" else m)
                                        break
                            res[sd] = dict(nat=sp.nat - settle, mid=sp.mid - settle,
                                           cr_nat=sp.nat, cr_mid=sp.mid, width=sp.width,
                                           stop_nat=stop["nat"], stop_mid=stop["mid"])
                        for rule, parts in (("put", "p"), ("call", "c"), ("condor", "pc")):
                            if not all(p in res for p in parts):
                                continue
                            tr = dict(sym=sym, d=d, exp=exp, mode=mode, delta=delta, nk=nk, rule=rule,
                                      half=1 if d < half_cut else 2, week=exp.isocalendar()[1],
                                      iv=iv, rv20=rv_before(sym, d))
                            for k in ("nat", "mid", "cr_nat", "cr_mid", "stop_nat", "stop_mid"):
                                tr[k] = sum(res[p][k] for p in parts) * (MULT if "cr" not in k else 1)
                            trades.append(tr)
    T = pd.DataFrame(trades)

    print(f"\n=== 2. Credit spreads, $ per 1-lot; halves split at entry {half_cut} ===")
    out = []
    for (mode, rule, delta, nk), g in T.groupby(["mode", "rule", "delta", "nk"]):
        for pm in ("nat", "mid"):
            gg = g.assign(pnl=g[pm], credit=g["cr_" + pm])
            s = summarise(gg)
            out.append(dict(mode=mode, rule=rule, delta=delta, w=nk, px=pm, **s,
                            h1=round(gg[gg.half == 1].pnl.sum()), h2=round(gg[gg.half == 2].pnl.sum()),
                            stop2x=round(g["stop_" + pm].sum())))
    print(pd.DataFrame(out).to_string(index=False))

    # ------------------------------------------------ 3. market direction
    def ret(sym: str, a_: date, b_: date) -> float:
        return float(px.loc[b_, sym] / px.loc[a_, sym] - 1)
    first, mid_d, last = entry_days[0], half_cut, last_settle
    print("\n=== 3. Market direction (close to close) ===")
    for lab, (a_, b_) in (("window", (first, last)), ("half1", (first, mid_d)), ("half2", (mid_d, last))):
        stk = [x for x in syms if x != "QQQ" and not math.isnan(px.loc[a_, x])]
        ew = np.mean([ret(x, a_, b_) for x in stk])
        print(f"{lab:7s} {a_}..{b_}  QQQ {ret('QQQ', a_, b_):+.1%}   equal-weight {len(stk)} stocks {ew:+.1%}")
    print("per-symbol window return:",
          {x: f"{ret(x, max(first, date(2026, 8, 27)), last):+.0%}" for x in syms})

    # ------------------------------------------------ 4. IV - RV20 filter
    print("\n=== 4. Filter: sell only when ATM IV - RV20 > X (X picked on half 1 by $/trade, n>=15) ===")
    grid = [-0.20, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15, 0.20, 0.30]
    tests = [("weekly condor 0.20 w1 nat", T[(T["mode"] == "weekly") & (T.rule == "condor") & (T.delta == 0.20) & (T.nk == 1)].assign(pnl=lambda x: x.nat)),
             ("0DTE condor 0.20 w1 nat", T[(T["mode"] == "0DTE") & (T.rule == "condor") & (T.delta == 0.20) & (T.nk == 1)].assign(pnl=lambda x: x.nat)),
             ("straddle 10:05 3-8d", V[(V.label == "10:05") & V.dte.between(3, 8)].assign(half=lambda x: np.where(x.d < half_cut, 1, 2))),
             ("straddle 10:05 0DTE", V[(V.label == "10:05") & (V.dte == 0)].assign(half=lambda x: np.where(x.d < half_cut, 1, 2)))]
    for name, df in tests:
        df = df.assign(edge=df.iv - df.rv20)
        h1, h2 = df[df.half == 1], df[df.half == 2]
        best, bx = -1e18, None
        for X in grid:
            sel = h1[h1.edge > X]
            if len(sel) >= 15 and sel.pnl.mean() > best:
                best, bx = sel.pnl.mean(), X
        sel2 = h2[h2.edge > bx] if bx is not None else h2.iloc[:0]
        print(f"{name:28s} X={bx}  h1 $/tr {best:,.0f} | h2 filtered n={len(sel2)} $/tr {sel2.pnl.mean():,.0f} "
              f"total {sel2.pnl.sum():,.0f}  vs h2 unfiltered n={len(h2)} $/tr {h2.pnl.mean():,.0f}")


if __name__ == "__main__":
    main()
