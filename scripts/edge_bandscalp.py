"""Bollinger-band mean-reversion scalp on QQQ 0DTE: does it beat chance, and costs?

Hypothesis under test: close beyond the LOWER 20-SMA 2-sigma band -> buy a call
debit spread, exit at the midline / upper band / after N minutes; mirror with
puts at the upper band. Never tested in this pure form: STRICT/RELAXED need
MACD+trend agreement, FADE is upper-band-only.

    python scripts/edge_bandscalp.py underlying   # layer 1: QQQ only, 1m + 5m, vs matched null
    python scripts/edge_bandscalp.py options      # layer 2: 4/3-wide ITM debit spread, chain-priced
    python scripts/edge_bandscalp.py options1m    # layer 2 on Tradier 1m bars (20 sessions)
    python scripts/edge_bandscalp.py all

Data:
  5-minute: the sweep harness's cached 60 sessions (%TEMP%/sweep_cfg_cache.pkl,
            yfinance, 2026-07-10..10-02) -- the exact bars stallarm/bandonly use.
            Falls back to yfinance 60d if the cache is missing.
  1-minute: Tradier /markets/timesales (TRADIER_API_KEY from .env). Tradier only
            serves 1-min back to ~20 sessions; cached at %TEMP%/bandscalp_1m.pkl.

Out of sample: every row is reported on sessions half 1 and half 2 separately;
the one "pick" per layer is chosen on half 1 only and its half-2 row reported.
"""
import math
import os
import pickle
import random
import sys
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import trading_engine.broker as B          # noqa: E402
import trading_engine.chain_pricer as CP    # noqa: E402

NY = ZoneInfo("America/New_York")
TMP = os.environ.get("TEMP", "/tmp")
CACHE_5M = os.path.join(TMP, "sweep_cfg_cache.pkl")
CACHE_1M = os.path.join(TMP, "bandscalp_1m.pkl")

# Same cost model as scripts/sweep.py at its defaults (bid/ask half-spread
# from broker.fill_price, tick grid, +0.10 slippage, 0.014 commission).
SLIP = 0.10
COMM = 0.014
TICK_BREAK, TICK_LO, TICK_HI = 3.0, 0.01, 0.05


def _tick(p: float, side: str) -> float:
    if p <= 0:
        return 0.0
    t = TICK_LO if p < TICK_BREAK else TICK_HI
    n = p / t
    n = math.ceil(n - 1e-9) if side == "buy" else math.floor(n + 1e-9)
    return max(round(n * t, 4), 0.0)


def fill(v: float, side: str, mid: bool = False) -> float:
    if mid:
        return max(v, 0.0)
    return _tick(B.fill_price(v, side), side)


# ---------------------------------------------------------------- data
def load_5m():
    if os.path.exists(CACHE_5M):
        with open(CACHE_5M, "rb") as fh:
            hist, vix = pickle.load(fh)
        return hist[["Open", "High", "Low", "Close"]].copy(), dict(vix), "harness cache (yfinance 5m)"
    import yfinance as yf
    hist = yf.Ticker("QQQ").history(period="60d", interval="5m").tz_convert(NY)
    v = yf.Ticker("^VIX").history(period="60d", interval="1d")
    vix = {ts.date(): float(c) for ts, c in v["Close"].items()}
    return hist[["Open", "High", "Low", "Close"]].copy(), vix, "yfinance 5m"


def load_1m(days: list):
    if os.path.exists(CACHE_1M):
        with open(CACHE_1M, "rb") as fh:
            return pickle.load(fh), "Tradier timesales 1m (cached)"
    from dotenv import load_dotenv
    import httpx
    load_dotenv(os.path.join(ROOT, ".env"))
    key = os.getenv("TRADIER_API_KEY")
    if not key:
        return None, "none"
    env = os.getenv("TRADIER_ENV", "sandbox").lower()
    url = ("https://api.tradier.com" if env == "production"
           else "https://sandbox.tradier.com") + "/v1/markets/timesales"
    frames = []
    for d in days:
        r = httpx.get(url, params={"symbol": "QQQ", "interval": "1min",
                                   "start": f"{d} 09:30", "end": f"{d} 16:00",
                                   "session_filter": "open"},
                      headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                      timeout=30.0)
        if r.status_code != 200:
            continue
        data = ((r.json() or {}).get("series") or {})
        data = data.get("data") if isinstance(data, dict) else None
        if not data:
            continue
        if isinstance(data, dict):
            data = [data]
        df = pd.DataFrame(data)
        df.index = pd.to_datetime(df["time"]).dt.tz_localize(NY)
        frames.append(df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                         "close": "Close"})[["Open", "High", "Low", "Close"]])
    if not frames:
        return None, "none"
    out = pd.concat(frames).sort_index()
    out = out[(out.index.time >= dtime(9, 30)) & (out.index.time < dtime(16, 0))]
    with open(CACHE_1M, "wb") as fh:
        pickle.dump(out, fh)
    return out, "Tradier timesales 1m"


def with_bands(bars: pd.DataFrame) -> pd.DataFrame:
    """20-SMA, 2-sigma, sample std -- as nodes.bollinger_agent. Rolled over the
    continuous series so the first bars of a day are warmed by the prior day,
    as the live 5-day fetch is."""
    b = bars.copy()
    r = b["Close"].rolling(20)
    b["mid"] = r.mean()
    b["sd"] = r.std()
    b["up"] = b["mid"] + 2 * b["sd"]
    b["lo"] = b["mid"] - 2 * b["sd"]
    b["day"] = b.index.date
    return b


def events(b: pd.DataFrame, variant: str, step_min: int) -> list:
    """(i, dir) for band events. dir +1 = expect up (lower band), -1 = expect down.

    beyond:  first close outside the band (onset of a pierce).
    reentry: first close back inside after a close outside.
    Entry bar's CLOSE must be <= 15:30 so a 30-minute forward exists in-session.
    """
    c, lo, up, day = b["Close"].values, b["lo"].values, b["up"].values, b["day"].values
    t = b.index
    out = []
    for i in range(1, len(b)):
        if day[i] != day[i - 1] or np.isnan(lo[i - 1]):
            continue
        close_t = (t[i] + timedelta(minutes=step_min)).time()
        if not (dtime(9, 45) <= close_t <= dtime(15, 30)):
            continue
        if variant == "beyond":
            if c[i] < lo[i] and c[i - 1] >= lo[i - 1]:
                out.append((i, +1))
            elif c[i] > up[i] and c[i - 1] <= up[i - 1]:
                out.append((i, -1))
        else:
            if c[i - 1] < lo[i - 1] and c[i] >= lo[i]:
                out.append((i, +1))
            elif c[i - 1] > up[i - 1] and c[i] <= up[i]:
                out.append((i, -1))
    return out


# ---------------------------------------------------------------- layer 1
HORIZONS = (5, 10, 15, 30)


def _mins_left(ts_close) -> float:
    return B.minutes_to_expiry(ts_close.to_pydatetime())


def spread_pnl(b, i, j, d, vix, step, width=4.0, mid=False) -> float:
    """$ P&L of 1 contract ITM debit spread opened at bar i close, closed at bar j close."""
    s0, s1 = float(b["Close"].iat[i]), float(b["Close"].iat[j])
    atm = B.round_to_strike(s0)
    call = d > 0
    long_k = atm - width if call else atm + width
    m0 = _mins_left(b.index[i] + timedelta(minutes=step))
    m1 = _mins_left(b.index[j] + timedelta(minutes=step))
    vx = vix.get(b.index[i].date())
    v0 = CP.vertical_value(s0, m0, long_k, atm, call, vx)
    v1 = CP.vertical_value(s1, m1, long_k, atm, call, vx)
    per = fill(v1, "sell", mid) - fill(v0, "buy", mid) - (0 if mid else SLIP) - COMM
    return per * 100


def measure(b, i, d, step, target_sd, adverse_sd, vix):
    """Forward stats for a (bar, direction). target/adverse in units of this bar's sd."""
    c, h, l = b["Close"].values, b["High"].values, b["Low"].values
    sd = b["sd"].values[i]
    out = {}
    for hz in HORIZONS:
        j = i + hz // step
        out[f"r{hz}"] = d * (c[j] - c[i])
        out[f"z{hz}"] = d * (c[j] - c[i]) / sd
    tgt = c[i] + d * target_sd * sd
    adv = c[i] - d * adverse_sd * sd
    hit = 0
    for j in range(i + 1, i + 30 // step + 1):
        if (l[j] <= adv) if d > 0 else (h[j] >= adv):
            break
        if (h[j] >= tgt) if d > 0 else (l[j] <= tgt):
            hit = 1
            break
    out["hit"] = hit
    for hz in (5, 10):
        out[f"opt{hz}"] = spread_pnl(b, i, i + hz // step, d, vix, step)
    return out


def layer1_series(b, step, vix, label, rng):
    days = sorted(set(b["day"]))
    half_of = {d: (0 if k < len(days) // 2 else 1) for k, d in enumerate(days)}
    # Candidate null bars by clock time of the bar label (+-2 bars of 1m, +-1 bar of 5m).
    tod = np.array([t.hour * 60 + t.minute for t in b.index])
    ok = np.array([(dtime(9, 45) <= (t + timedelta(minutes=step)).time() <= dtime(15, 30))
                   and not np.isnan(s) for t, s in zip(b.index, b["sd"].values)])
    halfarr = np.array([half_of[d] for d in b["day"]])
    tol = 2 if step == 1 else 5
    rows = []
    for variant in ("beyond", "reentry"):
        ev = events(b, variant, step)
        for hf in (0, 1):
            evh = [(i, d) for i, d in ev if half_of[b["day"].iat[i]] == hf]
            if not evh:
                continue
            E, NULLS = [], []
            for i, d in evh:
                dist_sd = abs(b["mid"].iat[i] - b["Close"].iat[i]) / b["sd"].iat[i]
                E.append(measure(b, i, d, step, dist_sd, 1.0, vix))
                pool = np.where(ok & (halfarr == hf) & (np.abs(tod - tod[i]) <= tol))[0]
                pool = pool[pool != i]
                draws = rng.choice(pool, size=40, replace=True)
                NULLS.append([measure(b, int(r), d, step, dist_sd, 1.0, vix) for r in draws])
            E = pd.DataFrame(E)
            # null means: resample one matched draw per event, 2000 times
            nd = len(NULLS[0])
            null_mat = {k: np.array([[n[k] for n in nl] for nl in NULLS])
                        for k in E.columns}
            res = {"series": label, "variant": variant, "half": hf + 1, "n": len(E)}
            for k in ["r5", "r10", "r15", "r30", "z5", "z10", "z30", "hit", "opt5", "opt10"]:
                ev_mean = E[k].mean() if k[:3] != "opt" else (E[k] > 0).mean()
                mat = null_mat[k] if k[:3] != "opt" else (null_mat[k] > 0).astype(float)
                idx = rng.integers(0, nd, size=(2000, len(E)))
                boots = mat[np.arange(len(E))[None, :], idx].mean(axis=1)
                res[k] = ev_mean
                res[k + "_null"] = mat.mean()
                res[k + "_pct"] = (boots < ev_mean).mean() * 100
            rows.append(res)
    return rows


def run_layer1():
    rng = np.random.default_rng(7)
    h5, vix, src5 = load_5m()
    days5 = sorted(set(h5.index.date))
    h1, src1 = load_1m([d for d in days5 if d >= date(2026, 9, 4)])
    print(f"\nLAYER 1 -- UNDERLYING ONLY.  5m: {src5}, {len(days5)} sessions "
          f"{days5[0]}..{days5[-1]}")
    rows = layer1_series(with_bands(h5), 5, vix, "5m", rng)
    if h1 is not None:
        d1 = sorted(set(h1.index.date))
        print(f"  1m: {src1}, {len(d1)} sessions {d1[0]}..{d1[-1]}")
        rows += layer1_series(with_bands(h1), 1, vix, "1m", rng)
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 60)
    print("\n  signed forward move ($ QQQ), event vs time-matched null, and event percentile vs null")
    for _, r in df.iterrows():
        print(f"  {r.series} {r.variant:8s} h{r.half} n={r.n:4d} | "
              + "  ".join(f"r{hz} {r[f'r{hz}']:+.3f}/{r[f'r{hz}_null']:+.3f} p{r[f'r{hz}_pct']:3.0f}"
                          for hz in HORIZONS)
              + f" | z10 {r.z10:+.2f}/{r.z10_null:+.2f} z30 {r.z30:+.2f}/{r.z30_null:+.2f}"
              + f" | hit {r.hit*100:4.1f}%/{r.hit_null*100:4.1f}% p{r.hit_pct:3.0f}"
              + f" | spread>cost 5m {r.opt5*100:4.1f}%/{r.opt5_null*100:4.1f}%"
              + f" 10m {r.opt10*100:4.1f}%/{r.opt10_null*100:4.1f}% p{r.opt10_pct:3.0f}")
    # One pick on half 1: best (series, variant, horizon) by event-minus-null z, report half 2.
    best = None
    for (s, v), g in df.groupby(["series", "variant"]):
        g1 = g[g.half == 1]
        if g1.empty:
            continue
        for hz in HORIZONS:
            key = f"r{hz}"
            edge = float(g1[key].iat[0] - g1[key + "_null"].iat[0])
            if best is None or edge > best[0]:
                best = (edge, s, v, hz)
    if best:
        _, s, v, hz = best
        g2 = df[(df.series == s) & (df.variant == v) & (df.half == 2)]
        if not g2.empty:
            r = g2.iloc[0]
            print(f"\n  PICK on half 1: {s} {v} {hz}min (edge {best[0]:+.3f}).  "
                  f"Half 2 once: n={r.n} r{hz} {r[f'r{hz}']:+.3f} vs null {r[f'r{hz}_null']:+.3f} "
                  f"pct {r[f'r{hz}_pct']:.0f}")
    # Breakeven QQQ move for the 4-wide ITM spread: cost / net delta.
    b5 = with_bands(h5)
    thr, dl = [], []
    for i, d in events(b5, "beyond", 5):
        s0 = float(b5["Close"].iat[i])
        atm = B.round_to_strike(s0)
        call = d > 0
        lk = atm - 4 if call else atm + 4
        m = _mins_left(b5.index[i] + timedelta(minutes=5))
        vx = vix.get(b5.index[i].date())
        v = lambda s: CP.vertical_value(s, m, lk, atm, call, vx)
        delta = abs(v(s0 + 0.25) - v(s0 - 0.25)) / 0.5
        v0 = v(s0)
        cost = (fill(v0, "buy") - v0) + (v0 - fill(v0, "sell")) + SLIP + COMM
        dl.append(delta)
        thr.append(cost / delta if delta > 0.05 else np.nan)
    print(f"  4-wide ITM spread at band events: net delta median {np.nanmedian(dl):.2f} "
          f"(p25 {np.nanpercentile(dl, 25):.2f}, p75 {np.nanpercentile(dl, 75):.2f}); "
          f"round-trip cost ~${np.nanmedian([t * d for t, d in zip(thr, dl)]) * 100:.0f}; "
          f"QQQ breakeven move median ${np.nanmedian(thr):.2f} "
          f"(p25 {np.nanpercentile(thr, 25):.2f}, p75 {np.nanpercentile(thr, 75):.2f})")
    return df


# ---------------------------------------------------------------- layer 2
def replay(b, vix, variant, target, hold_min, width, mid, stop=-20.0, step=5):
    """1 contract, one position at a time, exits evaluated at bar close."""
    evset = dict(events(b, variant, step))
    c, sma = b["Close"].values, b["mid"].values
    up, lo, day = b["up"].values, b["lo"].values, b["day"].values
    trades = []
    i, n = 0, len(b)
    while i < n:
        if i not in evset:
            i += 1
            continue
        d = evset[i]
        call = d > 0
        s0 = float(c[i])
        atm = B.round_to_strike(s0)
        lk = atm - width if call else atm + width
        vx = vix.get(b.index[i].date())
        mins = lambda k: _mins_left(b.index[k] + timedelta(minutes=step))
        debit = fill(CP.vertical_value(s0, mins(i), lk, atm, call, vx), "buy", mid)
        if debit <= 0.05:
            i += 1
            continue
        j, reason, exit_v = i, None, None
        while True:
            j += 1
            if j >= n or day[j] != day[i]:
                j -= 1
                reason = "EOD"
                break
            v = fill(CP.vertical_value(float(c[j]), mins(j), lk, atm, call, vx), "sell", mid)
            pct = (v - debit) / debit * 100
            dev = d * (c[j] - sma[j])
            opp = d * (c[j] - (up[j] if call else lo[j]))
            if pct <= stop:
                reason = "STOP"
            elif target == "mid" and dev >= 0:
                reason = "MIDLINE"
            elif opp >= 0:
                reason = "OPP_BAND"
            elif (j - i) * step >= hold_min:
                reason = "TIME"
            elif (b.index[j] + timedelta(minutes=step)).time() >= dtime(15, 50):
                reason = "FLAT"
            if reason:
                exit_v = v
                break
        if exit_v is None:
            exit_v = fill(CP.vertical_value(float(c[j]), mins(j), lk, atm, call, vx), "sell", mid)
        per = exit_v - debit - (0 if mid else SLIP) - COMM
        trades.append({"day": day[i], "pnl": per * 100, "reason": reason})
        i = j + 1
    return trades


def _summ(trades, days):
    pd_ = {d: 0.0 for d in days}
    for t in trades:
        pd_[t["day"]] += t["pnl"]
    v = list(pd_.values())
    n = len(trades)
    return {"tr": n, "pday": sum(v) / len(v), "worst": min(v),
            "win": (sum(t["pnl"] > 0 for t in trades) / n * 100) if n else 0.0}


def run_layer2(one_min: bool = False):
    h5, vix, src5 = load_5m()
    step = 5
    if one_min:
        h1, src5 = load_1m([d for d in sorted(set(h5.index.date)) if d >= date(2026, 9, 4)])
        h5, step = h1, 1
    b = with_bands(h5)
    days = sorted(set(b["day"]))
    H1, H2 = set(days[:len(days) // 2]), set(days[len(days) // 2:])
    print(f"\nLAYER 2 -- OPTIONS, chain-calibrated pricer + session VIX, 1 contract, "
          f"{src5}, {len(days)} sessions ({min(H1)}..{max(H1)} | {min(H2)}..{max(H2)})")
    print(f"  {step}-min bars; exits (incl. the -20% stop) checked at bar close"
          + (" -- a 5-min hold is ONE bar." if step == 5 else "."))
    print(f"  {'arm':34s} {'tr':>4s} {'$/day':>8s} {'worst':>8s} {'h1 $/d':>8s} {'h2 $/d':>8s} "
          f"{'win%':>5s} | {'mid $/d':>8s} {'mid h1':>7s} {'mid h2':>7s}  exits")
    res = []
    for variant in ("beyond", "reentry"):
        for target in ("mid", "opp"):
            for width in (4.0, 3.0):
                for hold in (5, 10, 15):
                    tr = replay(b, vix, variant, target, hold, width, mid=False, step=step)
                    trm = replay(b, vix, variant, target, hold, width, mid=True, step=step)
                    a = _summ(tr, days)
                    a1 = _summ([t for t in tr if t["day"] in H1], sorted(H1))
                    a2 = _summ([t for t in tr if t["day"] in H2], sorted(H2))
                    m = _summ(trm, days)
                    m1 = _summ([t for t in trm if t["day"] in H1], sorted(H1))
                    m2 = _summ([t for t in trm if t["day"] in H2], sorted(H2))
                    rs = {}
                    for t in tr:
                        rs[t["reason"]] = rs.get(t["reason"], 0) + 1
                    label = f"{variant} tgt={target} w{width:.0f} N={hold}"
                    res.append((label, a, a1, a2, m, m1, m2))
                    print(f"  {label:34s} {a['tr']:4d} {a['pday']:+8.2f} {a['worst']:+8.2f} "
                          f"{a1['pday']:+8.2f} {a2['pday']:+8.2f} {a['win']:5.0f} | "
                          f"{m['pday']:+8.2f} {m1['pday']:+7.2f} {m2['pday']:+7.2f}  "
                          + " ".join(f"{k}:{v}" for k, v in sorted(rs.items())))
    best = max(res, key=lambda r: r[2]["pday"])
    label, a, a1, a2, m, m1, m2 = best
    print(f"\n  PICK on half 1: {label}  h1 {a1['pday']:+.2f}/day ({a1['tr']} tr) -> "
          f"half 2 once: {a2['pday']:+.2f}/day, {a2['tr']} tr, worst {a2['worst']:+.2f}, "
          f"win {a2['win']:.0f}%;  at mid: h2 {m2['pday']:+.2f}")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("underlying", "all"):
        run_layer1()
    if which in ("options", "all"):
        run_layer2()
    if which in ("options1m", "all"):
        run_layer2(one_min=True)
