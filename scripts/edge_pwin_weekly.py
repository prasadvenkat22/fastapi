"""Does the weekly screener's Pwin / edge mean anything against real outcomes?

Walk-forward, leak-free measurement of scripts/weekly_pick.py's probability
columns for the weekly debit book (3-day: Mon-Wed entries, this Friday;
7-day: Thu-Fri entries, first Friday >= 5 calendar days out).

WHAT THE SCREENER ACTUALLY COMPUTES (read from evaluate()):
  * Pwin  = mean(demeaned payoff > 0) = P_hist(finish beyond lo+cost / hi-cost).
            It is NOT a blend: Pimp and Pmc are printed beside it and never
            enter Pwin, need, edge or ev_dem.
  * Phist = P(spot * exp(lr - mean(lr)) beyond level), lr = 3y of overlapping
            N-session log returns, N = trading_days_to(exp).
  * Pmc   = monte_carlo_terminal(spot, ATR14, N): driftless, sigma_d = ATR/1.596/spot.
  * Pimp  = |delta| of the short leg (N(d1)) at T = N/252.
  * need  = cost / width;  edge = Pwin - need;  ev_dem = mean demeaned payoff.
  * N     = sessions AFTER today through expiry. The live run enters at 09:50
            ET, so today's remaining ~0.95 session is NOT in the horizon.

ENTRY PROXY: the close of the first hourly bar (10:30 ET) on the entry day.
History = daily bars through the previous close plus a synthetic partial bar
for today (O = day open, H/L = first-hour range, C = spot), which is what
yfinance hands the screener intra-session. Outcome = daily close on expiry.

Pimp needs historical IV, which we do not have: IV := RV20 (stated proxy).
Structures are priced with Black-Scholes at IV = RV20 * k, k in {0.9,1.0,1.2},
T = (N + 0.85)/252 (the session time that really remains), plus 5% of the
debit at entry and 5% of the debit at exit when the spread has value.
That edge is PARTLY CIRCULAR (model price vs a model-ish probability); the
level calibration is the uncontaminated part.

Week-VWAP gate proxy: anchored VWAP from HOURLY bars (typical price x volume)
from Monday's open through 10:30 today, side vs +/-0.25 ATR, slope = running
VWAP now vs one bar earlier (Monday: vs the bar's open).

    python scripts/edge_pwin_weekly.py            # uses/creates scratch/edge/pwin_cache
    python scripts/edge_pwin_weekly.py --refresh  # re-download prices
"""

from __future__ import annotations

import argparse
import math
import os
import pickle
import re
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
from scipy.stats import norm, spearmanr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from weekly_pick import atr14, rv20, monte_carlo_terminal  # noqa: E402  the real helpers

SYMS = ("AMZN,AVGO,CRWV,DELL,GOOGL,META,MRVL,MSFT,MU,NVDA,PANW,SNDK,STX,WDC,"
        "INTC,TSLA,AAPL,AMD").split(",")
CACHE = os.path.join(ROOT, "scratch", "edge", "pwin_cache")
KS = np.round(np.arange(-1.0, 2.0001, 0.25), 2)       # level grid, ATR from spot
IVK = (0.9, 1.0, 1.2)
COST_SIDE = 0.05
TOL_ATR = 0.25
HIST_DAYS = 3 * 365
REMAIN_TODAY = 0.85      # sessions left after a 10:30 entry (5.5 of 6.5 h)


# ----------------------------------------------------------------- data
def load(sym: str, refresh: bool):
    os.makedirs(CACHE, exist_ok=True)
    fn = os.path.join(CACHE, f"{sym}.pkl")
    if os.path.exists(fn) and not refresh:
        with open(fn, "rb") as f:
            return pickle.load(f)
    import yfinance as yf
    tk = yf.Ticker(sym)
    d = tk.history(start="2020-06-01", interval="1d").dropna(subset=["Close", "High", "Low"])
    h = tk.history(period="730d", interval="1h").dropna(subset=["Close"])
    d.index = pd.DatetimeIndex([x.date() for x in d.index])
    out = (d[["Open", "High", "Low", "Close", "Volume"]], h[["Open", "High", "Low", "Close", "Volume"]])
    with open(fn, "wb") as f:
        pickle.dump(out, f)
    return out


def expiry_for(t: date, book: str) -> date:
    if book == "w3":
        return t + timedelta(days=4 - t.weekday())
    d = t
    while d.weekday() != 4 or (d - t).days < 5:
        d += timedelta(days=1)
    return d


# ------------------------------------------------------------ helpers
def bs_call(s, k, t, iv):
    k = np.asarray(k, float)
    vt = iv * math.sqrt(t)
    d1 = (np.log(s / k) + 0.5 * vt * vt) / vt
    return s * norm.cdf(d1) - k * norm.cdf(d1 - vt)


def bs_put(s, k, t, iv):
    return bs_call(s, k, t, iv) - s + np.asarray(k, float)


def tail_stats(sorted_p, cums, x):
    """P(P >= x), E[(P - x)+], P(P <= x), E[(x - P)+] over an empirical sample."""
    n = len(sorted_p)
    i = np.searchsorted(sorted_p, x, side="left")          # count < x
    j = np.searchsorted(sorted_p, x, side="right")         # count <= x
    above_sum = cums[-1] - np.where(i > 0, cums[np.maximum(i - 1, 0)], 0.0)
    p_ge = (n - i) / n
    call = (above_sum - x * (n - i)) / n
    below_sum = np.where(j > 0, cums[np.maximum(j - 1, 0)], 0.0)
    p_le = j / n
    put = (x * j - below_sum) / n
    return p_ge, call, p_le, put


def gate_read(hb: pd.DataFrame, t: date, atr: float, spot: float):
    """('LONG'|'SHORT'|'MIXED'|None) from hourly bars Monday..10:30 today."""
    mon = t - timedelta(days=t.weekday())
    dts = hb.index.date
    sel = hb[(dts >= mon) & (dts <= t)]
    sel = sel[~((sel.index.date == t) & (sel.index.hour > 9))]   # today's first bar only
    if len(sel) == 0 or sel.index.date[-1] != t:
        return None
    tp = (sel["High"] + sel["Low"] + sel["Close"]) / 3.0
    v = sel["Volume"].astype(float).values
    cpv, cv = np.cumsum(tp.values * v), np.cumsum(v)
    run = np.where(cv > 0, cpv / np.where(cv > 0, cv, 1), sel["Close"].values)
    vw = run[-1]
    ref = run[-2] if len(run) > 1 else float(sel["Open"].iloc[-1])
    tol = TOL_ATR * atr
    side = "ABOVE" if spot > vw + tol else ("BELOW" if spot < vw - tol else "AT")
    slope = vw / ref - 1.0
    if side == "ABOVE" and slope > 0:
        return "LONG"
    if side == "BELOW" and slope < 0:
        return "SHORT"
    return "MIXED"


# --------------------------------------------------------------- main loop
def run(refresh: bool):
    lev_rows, st_rows = [], []
    for sym in SYMS:
        try:
            d, hb = load(sym, refresh)
        except Exception as e:
            print(f"{sym}: load failed {e}")
            continue
        sessions = list(d.index.date)
        sidx = {s: i for i, s in enumerate(sessions)}
        first = hb[hb.index.hour == 9]
        first_by_day = {ix.date(): r for ix, r in first.iterrows()}
        for t in sorted(first_by_day):
            if t not in sidx or t.weekday() > 4:
                continue
            book = "w3" if t.weekday() <= 2 else "w7"
            exp = expiry_for(t, book)
            if exp not in sidx:                      # holiday Friday or future
                continue
            i_t, i_e = sidx[t], sidx[exp]
            N = i_e - i_t                            # sessions after today, inclusive
            fb = first_by_day[t]
            spot = float(fb["Close"])
            past = d.iloc[:i_t]
            past = past[past.index >= pd.Timestamp(t - timedelta(days=HIST_DAYS))]
            if len(past) < 120:
                continue
            today = pd.DataFrame({"Open": [float(d["Open"].iloc[i_t])], "High": [float(fb["High"])],
                                  "Low": [float(fb["Low"])], "Close": [spot], "Volume": [0.0]},
                                 index=[pd.Timestamp(t)])
            h = pd.concat([past, today])
            a14, rv = atr14(h), rv20(h)
            if not (a14 > 0 and rv > 0):
                continue
            c = h["Close"].values
            outc = float(d["Close"].iloc[i_e])
            gate = gate_read(hb, t, a14, spot)
            mc = monte_carlo_terminal(spot, a14, N)
            mcs = np.sort(mc)
            dists = {}
            for tag, n_ in (("N", N), ("N1", N + 1)):
                lr = np.log(c[n_:] / c[:-n_])
                p = np.sort(spot * np.exp(lr - lr.mean()))
                dists[tag] = (p, np.cumsum(p))
            # ---- level calibration
            for dirn in ("bull", "bear"):
                lv = spot + KS * a14 if dirn == "bull" else spot - KS * a14
                Th = N / 252.0
                vt = rv * math.sqrt(Th)
                d1 = (np.log(spot / lv) + 0.5 * vt * vt) / vt
                pimp = norm.cdf(d1) if dirn == "bull" else norm.cdf(-d1)
                rec = dict(sym=sym, t=t, book=book, N=N, dirn=dirn)
                ph = {}
                for tag in ("N", "N1"):
                    p_ge, _, p_le, _ = tail_stats(*dists[tag], lv)
                    ph[tag] = p_ge if dirn == "bull" else p_le
                pm_ge, _, pm_le, _ = tail_stats(mcs, np.cumsum(mcs), lv)
                pmc = pm_ge if dirn == "bull" else pm_le
                hit = (outc >= lv) if dirn == "bull" else (outc <= lv)
                for j, k in enumerate(KS):
                    lev_rows.append((sym, t, book, N, dirn, k, ph["N"][j], ph["N1"][j],
                                     pmc[j], pimp[j], int(hit[j])))
            # ---- structures
            p, cs = dists["N"]
            Tp = (N + REMAIN_TODAY) / 252.0
            ia, ib = np.triu_indices(len(KS), 1)
            for dirn in ("bull", "bear"):
                if dirn == "bull":
                    lo = spot + KS[ia] * a14
                    hi = spot + KS[ib] * a14
                else:
                    hi = spot - KS[ia] * a14
                    lo = spot - KS[ib] * a14
                w = hi - lo
                ok = (w >= 0.02 * spot) & (w <= 0.12 * spot) & (lo > 0)
                lo, hi, w, ka, kb = lo[ok], hi[ok], w[ok], KS[ia][ok], KS[ib][ok]
                _, c_lo, _, p_lo = tail_stats(p, cs, lo)
                _, c_hi, _, p_hi = tail_stats(p, cs, hi)
                ev_val = (c_lo - c_hi) if dirn == "bull" else (p_hi - p_lo)
                real_val = (np.clip(outc - lo, 0, w) if dirn == "bull"
                            else np.clip(hi - outc, 0, w))
                agree = None if gate is None else (
                    (gate == "LONG") if dirn == "bull" else (gate == "SHORT"))
                against = None if gate is None else (
                    (gate == "SHORT") if dirn == "bull" else (gate == "LONG"))
                for k in IVK:
                    iv = rv * k
                    if dirn == "bull":
                        mid = bs_call(spot, lo, Tp, iv) - bs_call(spot, hi, Tp, iv)
                    else:
                        mid = bs_put(spot, hi, Tp, iv) - bs_put(spot, lo, Tp, iv)
                    cost = mid * (1 + COST_SIDE)
                    good = (cost > 0.05) & (cost < w)
                    be = (lo + cost) if dirn == "bull" else (hi - cost)
                    pg, _, pl, _ = tail_stats(p, cs, be)
                    pwin = pg if dirn == "bull" else pl
                    need = cost / w
                    ev_dem = (ev_val - cost) * 100
                    exitc = np.where(real_val > 0, COST_SIDE * mid, 0.0)
                    ret = (real_val - exitc - cost) / cost
                    rr = (w - cost) / cost
                    for m in np.nonzero(good)[0]:
                        st_rows.append((sym, t, book, N, dirn, k, ka[m], kb[m], cost[m], w[m],
                                        pwin[m], need[m], ev_dem[m], ev_dem[m] / (cost[m] * 100),
                                        rr[m], ret[m], gate, agree, against))
        print(f"{sym}: done ({len(lev_rows)} level rows, {len(st_rows)} structure rows)", flush=True)
    L = pd.DataFrame(lev_rows, columns=["sym", "t", "book", "N", "dirn", "k", "phist", "phist_n1",
                                        "pmc", "pimp", "hit"])
    S = pd.DataFrame(st_rows, columns=["sym", "t", "book", "N", "dirn", "ivk", "ka", "kb", "cost", "w",
                                       "pwin", "need", "ev_dem", "ev_pct", "rr", "ret", "gate",
                                       "agree", "against"])
    S["edge"] = S["pwin"] - S["need"]
    return L, S


# ----------------------------------------------------------------- report
def half_of(df):
    cut = pd.Series(sorted(df["t"].unique())).iloc[len(df["t"].unique()) // 2]
    return np.where(df["t"] < cut, "H1", "H2"), cut


def reliability(L, col):
    bins = np.linspace(0, 1, 11)
    out = []
    for hv in ("H1", "H2"):
        x = L[L.half == hv]
        b = np.clip(np.digitize(x[col], bins) - 1, 0, 9)
        g = x.groupby(b).agg(n=("hit", "size"), pred=(col, "mean"), real=("hit", "mean"))
        out.append(g.add_prefix(hv + "_"))
    return pd.concat(out, axis=1)


def brier(L, col):
    res = {}
    for hv in ("H1", "H2"):
        x = L[L.half == hv]
        bs = float(((x[col] - x["hit"]) ** 2).mean())
        clim = x.groupby(["book", "dirn", "k"])["hit"].transform("mean")
        bc = float(((clim - x["hit"]) ** 2).mean())
        res[hv] = (bs, bc, 1 - bs / bc)
    return res


def weekly_ic(x, col):
    """Mean per-entry-date Spearman(col, ret); t-stat over expiry-week clusters."""
    ics = []
    for (t, exp_wk), g in x.groupby(["t", "wk"]):
        if len(g) >= 10 and g[col].nunique() > 2:
            ics.append((exp_wk, spearmanr(g[col], g["ret"]).statistic))
    if not ics:
        return float("nan"), float("nan")
    s = pd.DataFrame(ics, columns=["wk", "ic"]).groupby("wk")["ic"].mean()
    return float(s.mean()), float(s.mean() / (s.std(ddof=1) / math.sqrt(len(s))))


def report(L, S, out):
    L["half"], cut = half_of(L)
    S["half"] = np.where(S["t"] < cut, "H1", "H2")
    S["wk"] = [pd.Timestamp(x).to_period("W-FRI") for x in S["t"]]
    S["wk"] = S["wk"].astype(str) + S["book"]
    L["pblend"] = (L.phist + L.pmc + L.pimp) / 3
    pr = lambda *a: print(*a, file=out)
    pr(f"entries {L.groupby(['sym','t']).ngroups} symbol-days, {L.t.min()}..{L.t.max()}, split at {cut}")
    pr(f"level rows {len(L)}, structure rows {len(S)}")
    pr("\nmean horizon N (sessions after entry day) by book/weekday:")
    pr(L.drop_duplicates(["sym", "t"]).assign(wd=lambda x: [pd.Timestamp(v).day_name()[:3] for v in x.t])
       .groupby(["book", "wd"])["N"].agg(["mean", "size"]).to_string())
    for col in ("phist", "phist_n1", "pmc", "pimp", "pblend"):
        b = brier(L, col)
        pr(f"\n== {col}: Brier H1 {b['H1'][0]:.4f} (clim {b['H1'][1]:.4f}, skill {b['H1'][2]:+.3f})"
           f"   H2 {b['H2'][0]:.4f} (clim {b['H2'][1]:.4f}, skill {b['H2'][2]:+.3f})")
        pr(reliability(L, col).round(3).to_string())
    pr("\nphist by level (k ATR beyond spot), pooled both halves: pred vs realised")
    pr(L.groupby(["book", "k"]).agg(pred=("phist", "mean"), pred_n1=("phist_n1", "mean"),
                                    pmc=("pmc", "mean"), pimp=("pimp", "mean"),
                                    real=("hit", "mean"), n=("hit", "size")).round(3).to_string())

    for k in IVK:
        x = S[S.ivk == k]
        pr(f"\n######## BS IV = RV20 x {k}")
        pr(f"mean ret all {x.ret.mean():+.3f}; mean edge {x.edge.mean():+.3f}; "
           f"Pwin calib of structures: mean pwin {x.pwin.mean():.3f} vs realised win "
           f"{(x.ret > 0).mean():.3f}")
        for sub, y in (("ALL GRID", x),
                       ("BOOK-ELIGIBLE", x[(x.need.between(0.20, 0.75)) & (x.rr.between(1, 3)) &
                                          (x.pwin >= 0.45) & (x.edge > 0) & (x.ev_dem > 0)])):
            pr(f"-- {sub}: n {len(y)}")
            for col in ("edge", "ev_pct"):
                for hv in ("H1", "H2"):
                    z = y[y.half == hv]
                    if len(z) < 50:
                        continue
                    dec = pd.qcut(z[col].rank(method="first"), 10, labels=False)
                    g = z.groupby(dec)["ret"].mean()
                    rho = spearmanr(z[col], z["ret"]).statistic
                    ic, tt = weekly_ic(z, col)
                    pr(f"   {col:6s} {hv} rho {rho:+.3f}  per-date IC {ic:+.3f} (t {tt:+.1f})  "
                       f"decile mean ret: " + " ".join(f"{v:+.2f}" for v in g.values))
            top = y[y.edge >= y.groupby("half")["edge"].transform(lambda s: s.quantile(0.9))]
            for hv in ("H1", "H2"):
                z = top[top.half == hv]
                a, o, m = z[z.agree == True], z[z.against == True], z[(z.agree == False) & (z.against == False)]
                pr(f"   top-edge decile {hv}: gate-agree n {len(a)} ret {a.ret.mean():+.3f} | "
                   f"gate-opposite n {len(o)} ret {o.ret.mean():+.3f} | mixed n {len(m)} ret {m.ret.mean():+.3f}")
        # the book's own pick: per date, best eligible per name by ev_dem, gate ok, top 3
        e = x[(x.need.between(0.20, 0.75)) & (x.rr.between(1, 3)) & (x.pwin >= 0.45) &
              (x.edge > 0) & (x.ev_dem > 0) & (x.agree == True)]
        if len(e):
            b = e.sort_values("ev_dem", ascending=False).groupby(["t", "sym"]).head(1)
            b = b.sort_values("ev_dem", ascending=False).groupby("t").head(3)
            for hv in ("H1", "H2"):
                z = b[b.half == hv]
                pr(f"   simulated book picks {hv}: n {len(z)} mean ret {z.ret.mean():+.3f} "
                   f"win {(z.ret > 0).mean():.2f} mean Pwin {z.pwin.mean():.3f} need {z.need.mean():.3f}")


def live_check(out):
    pr = lambda *a: print(*a, file=out)
    fn = os.path.join(ROOT, "scratch", "bp", "data", "weekly-trade.log")
    if not os.path.exists(fn):
        pr("\nno local weekly-trade.log")
        return
    import yfinance as yf
    pat = re.compile(r"^(\S+) [\d:,]+ (\w+)\s+(CALL|PUT)\s+([\d.]+)/([\d.]+) w([\d.]+) x\d+ @ ([\d.]+).*"
                     r"Pwin ([\d.]+)% need ([\d.]+)%")
    exp_now = None
    pr("\nLIVE weekly plan lines (local copy scratch/bp/data/weekly-trade.log):")
    for line in open(fn, encoding="utf-8", errors="ignore"):
        m0 = re.search(r"WEEKLY BOOK: expiry (\S+)", line)
        if m0:
            exp_now = m0.group(1)
        m = pat.match(line)
        if not m:
            continue
        day, sym, cp, lk, sk, w, cost, pw, nd = m.groups()
        lk, sk, w, cost = float(lk), float(sk), float(w), float(cost)
        h = yf.Ticker(sym).history(start=exp_now, end=str(date.fromisoformat(exp_now) + timedelta(days=1)))
        px = float(h["Close"].iloc[-1])
        val = min(max(px - lk, 0), w) if cp == "CALL" else min(max(lk - px, 0), w)
        pr(f"  {day} {sym} {cp} {lk:g}/{sk:g} @ {cost} exp {exp_now} close {px:.2f}: value {val:.2f} "
           f"-> {'WIN' if val > cost else 'LOSS'} {(val - cost) / cost:+.0%} (Pwin {pw}%, need {nd}%)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    L, S = run(args.refresh)
    L.to_pickle(os.path.join(CACHE, "levels.pkl"))
    S.to_pickle(os.path.join(CACHE, "structs.pkl"))
    outp = os.path.join(ROOT, "scratch", "edge", "edge_pwin_weekly.txt")
    with open(outp, "w") as f:
        report(L, S, f)
        live_check(f)
    print(open(outp).read())
