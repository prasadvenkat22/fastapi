"""Replay the live QQQ band-touch engine on 1-minute bars and compare settings.

Mirrors trading_engine/nodes.py as of section 271:
  ENTRY  once a minute (the cron cycle): live price vs a 20-bar, 2-SD band on
         TODAY's closed 1-minute bars (so the first read is ~09:50); at/below the
         lower band -> call debit spread, at/above the upper -> put. Inside
         START..END. Refusals in the live order: win cooldown, entries cap, loss
         cooldown on the side that lost, 3 losses in a row, trend check, MACD check.
  EXITS  force close 15:45 -> take profit -> back at the 20-SMA (with minimum
         profit) -> profit lock -> stop with confirmation (in a row / total).

Approximations (the live engine polls every 10 s; this has 1-minute OHLC):
  - entry price = the bar's OPEN (the cycle runs a few seconds into the minute);
  - exits are checked three times a minute: at the open, at the bar's favourable
    extreme (take profit / 20-SMA / peak only -- price is assumed to have passed
    through those levels), and at the close. The adverse extreme is never used,
    so stops fire on open/close readings only;
  - spreads are priced with chain_pricer (Black-Scholes off the fitted smile,
    scaled by the day's VIX close), validated within ~1.0x on morning debit
    spreads; `validate` mode checks it against this engine's own fills;
  - fills: "mid" = model value; "fees" = mid plus Tradier's per-leg fees (live
    entries and exits fill at the mid when they fill at all); "cost" = mid
    +/- COST_HALF per side plus fees (the natural is 0.01 from the mid);
  - no daily-loss halt (it scales with account equity), 1 contract per trade,
    entries always fill (live, 2 of 6 mid entries on 10-09 did not).

    python scripts/replay_band_touch.py validate   # pricer vs the engine's real fills
    python scripts/replay_band_touch.py live       # live settings, trade list + summary
    python scripts/replay_band_touch.py oneway     # change one setting at a time
    python scripts/replay_band_touch.py grid       # factorial, chosen on half 1
    python scripts/replay_band_touch.py null       # live exits on random entries
    python scripts/replay_band_touch.py all
"""
from __future__ import annotations

import itertools
import math
import os
import pickle
import random
import sys
from dataclasses import dataclass, replace, asdict
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
CACHE_OLD = os.path.join(TMP, "bandscalp_1m.pkl")       # edge_bandscalp.py: 09-04..10-02
CACHE = os.path.join(TMP, "replay_band_touch_1m.pkl")    # everything this script has
COST_HALF = 0.01
# Tradier: $0 commission, ~$0.115 regulatory/clearing fees per contract per leg
# (account history, 10-06/10-07 QQQ fills) = 0.0023 a 2-leg spread per side.
FEES = 0.0023


# ---------------------------------------------------------------- settings
BREADTH: dict = {}   # date -> array of net_ratio per bar index (latest reading before the bar)
VWAP: dict = {}   # date -> array, session VWAP of the bars closed before each bar index
@dataclass(frozen=True)
class Cfg:
    min_profit: float = 15.0            # 20-SMA exit books only at/above this
    tp: float | None = 50.0             # engine take profit (None = off)
    lock_arm: float | None = 30.0       # None = the take profit
    lock_floor: float | None = 20.0     # None = profit lock off
    stop: float = -20.0
    confirm: float = 10.0               # stop confirmation minutes
    total: bool = False                 # count total minutes past the stop
    trend_bars: int = 20
    trend_min: float | None = 0.10      # None = trend check off
    macd: bool = False
    width: float = 2.0
    width_pm: float = 0.0               # spread width from pm_from on (0 = same as width)
    pm_from: str = "12:00"
    atm: bool = True
    sides: str = "both"                 # both / calls / puts
    start: str = "09:40"
    end: str = "15:30"
    win_cd: float = 5.0
    loss_cd: float = 5.0
    max_entries: int = 15
    max_streak: int = 3
    sd: float = 2.0
    period: int = 20
    # Confirmed-reversal entry (0 = off, enter on the touch). A touch only ARMS the
    # side; the entry waits until the extreme (lowest low / highest high since the
    # first touch) is at least rev_wait closed bars old, price has turned back off
    # it but is still on the touch side of the 20-SMA. Disarms after rev_window bars
    # without a fresh touch.
    rev_wait: int = 0
    rev_window: int = 10
    # Volatility-adaptive band (0 = off). When the last vol_look closes span more
    # than vol_pct percent of price, the band uses sd_hi standard deviations
    # instead of sd -- a wider band on fast days so a mid-move print is not a touch.
    sd_hi: float = 0.0
    vol_look: int = 30
    vol_pct: float = 0.0
    # Minimum band width in dollars (upper - lower); 0 = off. No entry when narrower.
    min_band: float = 0.0
    # Extra entry filters (0 = off). rsi_hi: block a CALL when RSI(14) > rsi_hi and a PUT when
    # RSI < 100-rsi_hi (the usual overbought/oversold rule). rsi_knife: the opposite -- block a
    # call when RSI < rsi_knife and a put when RSI > 100-rsi_knife (do not fade a runaway).
    # ema_stretch: block when price is more than this many dollars beyond the 9-bar EMA in the
    # touch direction (below it for a call, above it for a put).
    rsi_hi: float = 0.0
    rsi_knife: float = 0.0
    ema_stretch: float = 0.0
    # VWAP: 1 = block a CALL above VWAP and a PUT below it (fade only from the stretched side
    # of the day's average price); 2 = the reverse (block a call below VWAP, a put above it).
    # vwap_dist: only block when price is more than this many dollars from VWAP.
    vwap_mode: int = 0
    vwap_dist: float = 0.0
    # Breadth (net advancers/decliners over the basket, -1..+1, latest reading before the bar).
    # breadth_mode 1 = with the tape: block a CALL when breadth < -breadth_min, a PUT when
    # breadth > +breadth_min. 2 = contrarian: block a call when breadth > +min, a put when < -min.
    breadth_mode: int = 0
    breadth_min: float = 0.0

    def label(self, base: "Cfg") -> str:
        d = {k: v for k, v in asdict(self).items() if asdict(base)[k] != v}
        return ", ".join(f"{k}={v}" for k, v in d.items()) or "LIVE"


def live_cfg() -> Cfg:
    """The settings in force on the droplet 2026-11-03 (trading_overrides.env, read 16:30Z; SD_HI=3 but VOL_PCT unset = adaptive off).

    Profit lock floor is blank there (off); arm 20 is moot with no floor.
    """
    return Cfg(min_profit=35.0, tp=50.0, lock_arm=20.0, lock_floor=None, stop=-5.0,
               confirm=2.0, total=True, trend_bars=30, trend_min=0.25, start="09:30",
               end="14:45", max_entries=15, width=3.0)


# ---------------------------------------------------------------- data
def _fetch(days: list) -> pd.DataFrame | None:
    from dotenv import load_dotenv
    import httpx
    load_dotenv(os.path.join(ROOT, ".env"))
    key = os.getenv("TRADIER_API_KEY")
    frames = []
    for d in days:
        r = httpx.get("https://api.tradier.com/v1/markets/timesales",
                      params={"symbol": "QQQ", "interval": "1min", "start": f"{d} 09:30",
                              "end": f"{d} 16:00", "session_filter": "open"},
                      headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                      timeout=30.0)
        data = ((r.json() or {}).get("series") or {}) if r.status_code == 200 else {}
        data = data.get("data") if isinstance(data, dict) else None
        if not data:
            continue
        df = pd.DataFrame(data if isinstance(data, list) else [data])
        df.index = pd.to_datetime(df["time"]).dt.tz_localize(NY)
        frames.append(df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                         "close": "Close"})[["Open", "High", "Low", "Close"]])
    return pd.concat(frames) if frames else None


def load_bars() -> pd.DataFrame:
    have = None
    for p in (CACHE, CACHE_OLD):
        if os.path.exists(p):
            with open(p, "rb") as fh:
                part = pickle.load(fh)
            have = part if have is None else pd.concat([have, part])
    have = have[~have.index.duplicated()].sort_index() if have is not None else None
    got = set(have.index.date) if have is not None else set()
    from trading_engine import market_calendar
    first = min(got) if got else date.today() - timedelta(days=40)
    want = [first + timedelta(days=i) for i in range((date.today() - first).days + 1)]
    want = [d for d in want if market_calendar.is_trading_day(d) and d not in got]
    # Today only once the session is over.
    now = datetime.now(NY)
    live_today = os.environ.get("REPLAY_TODAY") == "1"     # partial session, never cached
    want = [d for d in want if d < now.date() or now.time() >= dtime(16, 5)
            or (live_today and d == now.date())]
    if want:
        new = _fetch(want)
        if new is not None:
            have = new if have is None else pd.concat([have, new])
            have = have[~have.index.duplicated()].sort_index()
    have = have[(have.index.time >= dtime(9, 30)) & (have.index.time < dtime(16, 0))]
    keep = have if not (live_today and now.time() < dtime(16, 5)) else have[have.index.date < now.date()]
    with open(CACHE, "wb") as fh:
        pickle.dump(keep, fh)
    return have


def load_vix(days) -> dict:
    import yfinance as yf
    v = yf.Ticker("^VIX").history(start=str(min(days) - timedelta(days=7)),
                                  end=str(max(days) + timedelta(days=2)), interval="1d")
    closes = {ts.date(): float(c) for ts, c in v["Close"].items()}
    out, last = {}, None
    for d in sorted(set(closes) | set(days)):
        # The PRIOR close: known at the open, so no look-ahead into the day priced.
        if d in days:
            out[d] = last
        if d in closes:
            last = closes[d]
    return out


@dataclass
class Day:
    d: date
    t: list            # bar start times (datetime, NY)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    vix: float | None
    hist: np.ndarray   # MACD histogram on closes, today only


def prep(bars: pd.DataFrame, vix: dict) -> list:
    days = []
    for d, g in bars.groupby(bars.index.date):
        if len(g) < int(os.environ.get("REPLAY_MIN_BARS", "300")):
            continue                     # half day / partial data
        c = g["Close"].astype(float)
        macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        hist = (macd - macd.ewm(span=9, adjust=False).mean()).values
        days.append(Day(d, [ts.to_pydatetime() for ts in g.index], g["Open"].values.astype(float),
                        g["High"].values.astype(float), g["Low"].values.astype(float),
                        c.values, vix.get(d), hist))
    return days


# ---------------------------------------------------------------- engine
def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def value(day: Day, spot: float, when: datetime, long_k: float, short_k: float, call: bool) -> float:
    return CP.vertical_value(spot, B.minutes_to_expiry(when), long_k, short_k, call, day.vix)


def strikes(spot: float, call: bool, cfg: Cfg) -> tuple:
    atm = B.round_to_strike(spot)
    w = cfg.width
    if cfg.atm:
        return (atm, atm + w) if call else (atm, atm - w)
    return (atm - w, atm) if call else (atm + w, atm)


def band_at(day: Day, i: int, cfg: Cfg):
    """Band from closed bars before bar i (today only), slope and MACD as _band_touch."""
    if i < cfg.period:
        return None
    last = day.c[i - cfg.period:i]
    mid, sd = float(last.mean()), float(last.std(ddof=1))
    slope = None
    n = cfg.period + cfg.trend_bars
    if cfg.trend_bars > 0 and i >= n:
        slope = mid - float(day.c[i - n:i - cfg.trend_bars].mean())
    hist = prev = None
    if i >= 35:
        hist, prev = float(day.hist[i - 1]), float(day.hist[i - 2])
    k = cfg.sd
    if cfg.sd_hi > 0 and cfg.vol_pct > 0 and i >= cfg.vol_look:
        w = day.c[i - cfg.vol_look:i]
        if (float(w.max()) - float(w.min())) / float(w.mean()) * 100 > cfg.vol_pct:
            k = cfg.sd_hi
    return mid, mid + k * sd, mid - k * sd, slope, hist, prev


def refused(call: bool, b, cfg: Cfg) -> str | None:
    _mid, _up, _lo, slope, hist, prev = b
    if cfg.trend_min is not None and slope is not None:
        if not call and slope > cfg.trend_min:
            return "trend"
        if call and slope < -cfg.trend_min:
            return "trend"
    if cfg.macd and hist is not None:
        if call and not hist > prev:
            return "macd"
        if not call and not hist < prev:
            return "macd"
    return None


def run_day(day: Day, cfg: Cfg, fills: str = "mid", rng: random.Random | None = None,
            p_random: float = 0.0) -> list:
    """Trades for one session: dicts with time, side, entry, exit, pnl, reason."""
    trades = []
    pos = None
    last = None          # (closed_at, pnl, call)
    entries = streak = 0
    rev = None           # [call, first_touch_i, last_touch_i] while a reversal entry waits
    start, end, fc = _hm(cfg.start), _hm(cfg.end), dtime(15, 45)
    half = COST_HALF if fills == "cost" else 0.0
    comm = FEES if fills in ("fees", "cost") else 0.0
    arm = cfg.lock_arm if cfg.lock_arm is not None else cfg.tp

    def close(when, val, reason):
        nonlocal pos, last, streak
        px = max(val - half, 0.0)
        pnl = (px - pos["fill"] - 2 * comm) * 100
        trades.append(dict(day=day.d, opened=pos["t"], closed=when, call=pos["call"],
                           k=f"{pos['k'][0]:g}/{pos['k'][1]:g}", entry=pos["fill"], exit=px,
                           pnl=pnl, reason=reason, peak=pos["peak"]))
        last = (when, pnl, pos["call"])
        streak = streak + 1 if pnl < 0 else 0
        pos = None

    for i, t0 in enumerate(day.t):
        b = band_at(day, i, cfg)
        # ---- manage
        if pos is not None:
            call = pos["call"]
            fav = day.h[i] if call else day.l[i]
            points = [("open", day.o[i], t0 + timedelta(seconds=6)),
                      ("fav", fav, t0 + timedelta(seconds=30)),
                      ("close", day.c[i], t0 + timedelta(seconds=59))]
            for kind, spot, when in points:
                if pos is None:
                    break
                v = value(day, spot, when, *pos["k"], call)
                ret = (v - pos["entry_v"]) / pos["entry_v"] * 100 if pos["entry_v"] > 0 else 0.0
                pos["peak"] = max(pos["peak"], ret)
                if when.time() >= fc:
                    close(when, v, "FORCE_CLOSE")
                    break
                if cfg.tp is not None and ret >= cfg.tp:
                    tp_v = pos["entry_v"] * (1 + cfg.tp / 100) if kind == "fav" else v
                    close(when, tp_v, "TAKE_PROFIT")
                    break
                if b is not None:
                    mid = b[0]
                    crossed = (spot >= mid) if call else (spot <= mid)
                    if crossed:
                        sv = value(day, mid, when, *pos["k"], call) if kind == "fav" else v
                        sret = (sv - pos["entry_v"]) / pos["entry_v"] * 100
                        if sret >= cfg.min_profit:
                            close(when, sv, "SMA")
                            break
                if kind == "fav":
                    continue
                if (cfg.lock_floor is not None and arm is not None
                        and pos["peak"] >= arm and ret <= cfg.lock_floor):
                    close(when, v, "PROFIT_LOCK")
                    break
                breach = ret <= cfg.stop
                if cfg.total:
                    if breach:
                        if pos["s_last"] is not None:
                            pos["s_held"] += min((when - pos["s_last"]).total_seconds() / 60, 2.0)
                        pos["s_last"] = when
                    else:
                        pos["s_last"] = None
                    held = pos["s_held"]
                else:
                    if breach:
                        pos["s_since"] = pos["s_since"] or when
                        held = (when - pos["s_since"]).total_seconds() / 60
                    else:
                        pos["s_since"] = None
                        held = 0.0
                if breach and (cfg.confirm <= 0 or held >= cfg.confirm - 10 / 60):
                    close(when, v, "STOP_LOSS")
                    break
            continue                     # re-entry waits for the next cycle

        # ---- enter
        now = t0 + timedelta(seconds=6)
        if not (start <= now.time() < end) or b is None:
            continue
        spot = day.o[i]
        mid, up, lo, *_ = b
        if p_random <= 0 and up - lo < cfg.min_band:
            continue
        if p_random > 0:
            if rng.random() >= p_random:
                continue
            call = rng.random() < 0.5
        elif cfg.rev_wait > 0:
            touch = True if spot <= lo else (False if spot >= up else None)
            if touch is not None:
                if rev is None or rev[0] != touch:
                    rev = [touch, i, i]          # side, first touch bar, latest touch bar
                else:
                    rev[2] = i
            if rev is None:
                continue
            if i - rev[2] > cfg.rev_window:
                rev = None
                continue
            call = rev[0]
            if call:
                seg = day.l[rev[1]:i] if i > rev[1] else day.l[rev[1]:rev[1] + 1]
                j = int(np.argmin(seg)); ext = float(seg[j])
                turned = spot > ext and spot < mid
            else:
                seg = day.h[rev[1]:i] if i > rev[1] else day.h[rev[1]:rev[1] + 1]
                j = int(np.argmax(seg)); ext = float(seg[j])
                turned = spot < ext and spot > mid
            age = (i - 1) - (rev[1] + j)         # closed bars since the extreme
            if not (turned and age >= cfg.rev_wait):
                continue
        else:
            if spot <= lo:
                call = True
            elif spot >= up:
                call = False
            else:
                continue
        if cfg.sides == "calls" and not call or cfg.sides == "puts" and call:
            continue
        if last is not None:
            age = (now - last[0]).total_seconds() / 60
            if last[1] >= 0 and age < cfg.win_cd:
                continue
            if last[1] < 0 and age < cfg.loss_cd and last[2] == call:
                continue
        if entries >= cfg.max_entries or streak >= cfg.max_streak:
            continue
        if p_random <= 0 and refused(call, b, cfg):
            continue
        if p_random <= 0 and cfg.breadth_mode and day.d in BREADTH:
            br = float(BREADTH[day.d][i])
            if not np.isnan(br):
                if cfg.breadth_mode == 1 and ((call and br < -cfg.breadth_min) or (not call and br > cfg.breadth_min)):
                    continue
                if cfg.breadth_mode == 2 and ((call and br > cfg.breadth_min) or (not call and br < -cfg.breadth_min)):
                    continue
        if p_random <= 0 and cfg.vwap_mode and day.d in VWAP and i < len(VWAP[day.d]):
            vw = float(VWAP[day.d][i])
            if vw > 0:
                above = spot - vw
                if cfg.vwap_mode == 1 and ((call and above > cfg.vwap_dist) or (not call and -above > cfg.vwap_dist)):
                    continue
                if cfg.vwap_mode == 2 and ((call and -above > cfg.vwap_dist) or (not call and above > cfg.vwap_dist)):
                    continue
        if p_random <= 0 and (cfg.rsi_hi or cfg.rsi_knife or cfg.ema_stretch):
            cs = pd.Series(day.c[:i])
            if len(cs) >= 15:
                dl = cs.diff()
                ag = dl.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
                al = (-dl.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
                rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
                if cfg.rsi_hi and ((call and rsi > cfg.rsi_hi) or (not call and rsi < 100 - cfg.rsi_hi)):
                    continue
                if cfg.rsi_knife and ((call and rsi < cfg.rsi_knife) or (not call and rsi > 100 - cfg.rsi_knife)):
                    continue
                e9 = float(cs.ewm(span=9, adjust=False).mean().iloc[-1])
                if cfg.ema_stretch and ((call and e9 - spot > cfg.ema_stretch) or (not call and spot - e9 > cfg.ema_stretch)):
                    continue
        k = strikes(spot, call, replace(cfg, width=cfg.width_pm) if cfg.width_pm and now.time() >= _hm(cfg.pm_from) else cfg)
        v = value(day, spot, now, *k, call)
        if v < 0.05:
            continue
        pos = dict(t=now, call=call, k=k, entry_v=v, fill=v + half, peak=0.0,
                   s_since=None, s_last=None, s_held=0.0)
        entries += 1
        rev = None
    if pos is not None:
        when = day.t[-1] + timedelta(seconds=59)
        close(when, value(day, day.c[-1], when, *pos["k"], pos["call"]), "FORCE_CLOSE")
    return trades


def run(days, cfg, fills="mid", **kw):
    out = []
    for d in days:
        out.extend(run_day(d, cfg, fills, **kw))
    return out


def summary(trades, days) -> dict:
    by_day = {d.d: 0.0 for d in days}
    for t in trades:
        by_day[t["day"]] += t["pnl"]
    vals = list(by_day.values())
    n = len(vals)
    h1, h2 = vals[: n // 2], vals[n // 2:]
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    loss = [t["pnl"] for t in trades if t["pnl"] <= 0]
    return dict(n=len(trades), win=len(wins) / len(trades) * 100 if trades else 0.0,
                day=sum(vals) / n, worst=min(vals), h1=sum(h1) / len(h1), h2=sum(h2) / len(h2),
                aw=np.mean(wins) if wins else 0.0, al=np.mean(loss) if loss else 0.0,
                calls=sum(t["pnl"] for t in trades if t["call"]),
                puts=sum(t["pnl"] for t in trades if not t["call"]))


HDR = (f"{'setting':44s} {'n':>4s} {'win%':>5s} {'$/day':>7s} {'worst':>7s} "
       f"{'half1':>7s} {'half2':>7s} {'avgW':>6s} {'avgL':>6s} {'calls$':>7s} {'puts$':>7s}")


def row(label, s) -> str:
    return (f"{label[:44]:44s} {s['n']:4d} {s['win']:5.0f} {s['day']:+7.2f} {s['worst']:+7.0f} "
            f"{s['h1']:+7.2f} {s['h2']:+7.2f} {s['aw']:+6.1f} {s['al']:+6.1f} "
            f"{s['calls']:+7.0f} {s['puts']:+7.0f}")


# ---------------------------------------------------------------- modes
REAL = [  # the engine's own band-touch fills (trading_history), ET
    ("2026-10-05 10:01", True, 750, 751, 0.60), ("2026-10-05 10:58", False, 756, 754, 1.23),
    ("2026-10-05 11:36", True, 751, 753, 1.44), ("2026-10-05 12:03", True, 751, 753, 1.51),
    ("2026-10-05 12:25", False, 756, 754, 1.43), ("2026-10-05 13:16", False, 757, 755, 1.54),
    ("2026-10-05 14:06", True, 755, 757, 0.50), ("2026-10-05 14:22", False, 755, 753, 0.33),
    ("2026-10-05 14:58", False, 756, 754, 0.55), ("2026-10-06 09:58", False, 761, 759, 0.69),
    ("2026-10-06 10:30", True, 760, 762, 0.81), ("2026-10-06 11:30", True, 762, 764, 0.81),
    ("2026-10-09 12:01", True, 749, 751, 0.89), ("2026-10-09 12:51", False, 751, 749, 0.54),
    ("2026-10-09 13:50", False, 751, 749, 0.55), ("2026-10-09 14:50", False, 752, 750, 0.72),
]


def mode_validate(days):
    by = {d.d: d for d in days}
    print("PRICER vs the engine's real entry fills (model at the bar open, ~6 s in)")
    print(f"{'entry':17s} {'spread':>14s} {'fill':>5s} {'model':>6s} {'ratio':>6s}")
    ratios = []
    for ts, call, lk, sk, fill in REAL:
        when = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=NY)
        d = by.get(when.date())
        if d is None:
            print(f"{ts}  (no bars)")
            continue
        i = next(j for j, t in enumerate(d.t) if t >= when)
        v = value(d, d.o[i], when + timedelta(seconds=6), lk, sk, call)
        ratios.append(v / fill)
        print(f"{ts:17s} {('C ' if call else 'P ') + f'{lk}/{sk}':>14s} {fill:5.2f} {v:6.2f} {v / fill:6.2f}")
    print(f"median model/fill {np.median(ratios):.2f}  (range {min(ratios):.2f}..{max(ratios):.2f}, n {len(ratios)})")


def mode_live(days):
    cfg = live_cfg()
    tr = run(days, cfg)
    print(f"LIVE settings, {len(days)} sessions {days[0].d}..{days[-1].d}, mid fills")
    for t in tr[-25:]:
        print(f"  {t['day']} {t['opened']:%H:%M}-{t['closed']:%H:%M} {'C' if t['call'] else 'P'} "
              f"{t['k']:>8s} {t['entry']:.2f}->{t['exit']:.2f} {t['pnl']:+7.1f} {t['reason']:12s} "
              f"peak {t['peak']:+.0f}%")
    print(HDR)
    print(row("LIVE (mid)", summary(tr, days)))
    print(row("LIVE (mid + fees)", summary(run(days, cfg, "fees"), days)))
    print(row("LIVE (mid +/- 0.01 + fees)", summary(run(days, cfg, "cost"), days)))
    reasons = {}
    for t in tr:
        r = reasons.setdefault(t["reason"], [0, 0.0])
        r[0] += 1
        r[1] += t["pnl"]
    print("exits: " + "  ".join(f"{k} {v[0]} ({v[1]:+.0f})" for k, v in sorted(reasons.items())))


ONEWAY = {
    "min_profit": [0.0, 10.0, 25.0, 35.0, 1000.0],
    "tp": [None, 30.0, 75.0],
    "lock_floor": [None, 10.0, 25.0],
    "lock_arm": [20.0, 40.0],
    "stop": [-10.0, -30.0, -50.0, -100.0],
    "confirm": [0.0, 2.0, 5.0],
    "total": [True],
    "trend_min": [None, 0.25],
    "trend_bars": [10, 30],
    "macd": [True],
    "atm": [False],
    "width": [1.0, 4.0],
    "sides": ["calls", "puts"],
    "sd": [1.5, 2.5],
    "end": ["14:00"],
    "start": ["10:00"],
}


def mode_oneway(days, fills="mid"):
    base = live_cfg()
    print(f"ONE SETTING AT A TIME from LIVE, {len(days)} sessions, fills={fills}. "
          f"half1 {days[0].d}..{days[len(days) // 2 - 1].d}, half2 ..{days[-1].d}")
    print(HDR)
    print(row("LIVE", summary(run(days, base, fills), days)))
    for k, vals in ONEWAY.items():
        for v in vals:
            c = replace(base, **{k: v})
            print(row(c.label(base), summary(run(days, c, fills), days)))


GRID = dict(
    # The settings that looked better than LIVE in BOTH halves one at a time.
    min_profit=[10.0, 15.0, 25.0],
    stop=[-10.0, -20.0],
    confirm=[5.0, 10.0],
    total=[False, True],
    sd=[1.5, 2.0],
    width=[2.0, 4.0],
    trend_bars=[20, 30],
)


def mode_grid(days, fills="mid"):
    base = live_cfg()
    n = len(days)
    h1, h2 = days[: n // 2], days[n // 2:]
    keys = list(GRID)
    res = []
    for combo in itertools.product(*GRID.values()):
        c = replace(base, **dict(zip(keys, combo)))
        s1 = summary(run(h1, c, fills), h1)
        res.append((s1["day"], c))
    res.sort(key=lambda x: -x[0])
    print(f"GRID {len(res)} configs, fills={fills}: chosen on half 1 ({h1[0].d}..{h1[-1].d}), "
          f"then run on half 2 ({h2[0].d}..{h2[-1].d}) and all")
    print(f"{'rank':>4s} {'h1 $/day':>8s} {'h2 $/day':>8s} {'all':>7s} {'worst':>6s} {'n':>4s}  setting")
    s_live = summary(run(days, base, fills), days)
    for r, (d1, c) in enumerate(res[:10] + res[-3:]):
        s2 = summary(run(h2, c, fills), h2)
        sa = summary(run(days, c, fills), days)
        print(f"{r + 1 if r < 10 else '-':>4} {d1:+8.2f} {s2['day']:+8.2f} {sa['day']:+7.2f} "
              f"{sa['worst']:+6.0f} {sa['n']:4d}  {c.label(base)}")
    h2s = [summary(run(h2, c, fills), h2)["day"] for _, c in res]
    top = h2s[: max(1, len(h2s) // 10)]
    print(f"LIVE all {s_live['day']:+.2f}/day. Half-2 $/day of the top 10% by half 1: mean "
          f"{np.mean(top):+.2f}; of ALL configs: mean {np.mean(h2s):+.2f}, "
          f"best {max(h2s):+.2f}, worst {min(h2s):+.2f}")
    rank1 = np.argsort(np.argsort([-x[0] for x in res]))
    rank2 = np.argsort(np.argsort([-x for x in h2s]))
    rho = np.corrcoef(rank1, rank2)[0, 1]
    print(f"Rank correlation half 1 vs half 2 across configs: {rho:+.2f} "
          f"(near 0 = the half-1 winner is luck)")


def mode_reversal(days, fills="fees"):
    """Confirmed-reversal entry vs the touch entry, each with the live stop and with looser stops."""
    base = live_cfg()
    n = len(days)
    print(f"REVERSAL ENTRY, {n} sessions {days[0].d}..{days[-1].d}, fills={fills}. "
          f"rev_wait = closed bars the extreme must have held; rev_window = bars to wait.")
    print(HDR)
    for stop, conf in ((-5.0, 10.0), (-20.0, 10.0), (-30.0, 10.0)):
        print(f"-- stop {stop:g}% confirm {conf:g} min")
        for wait, win in ((0, 0), (1, 5), (2, 5), (2, 10), (3, 10), (5, 10), (5, 15)):
            c = replace(base, stop=stop, confirm=conf, rev_wait=wait, rev_window=win)
            lab = "touch entry (LIVE entry)" if wait == 0 else f"reversal wait {wait} window {win}"
            print(row(lab, summary(run(days, c, fills), days)))


BANDS = (
    ("LIVE band sd 2.0 period 20", {}),
    ("sd 2.5", dict(sd=2.5)),
    ("sd 3.0", dict(sd=3.0)),
    ("period 30", dict(period=30)),
    ("period 40", dict(period=40)),
    ("sd 2.5 period 30", dict(sd=2.5, period=30)),
    ("sd 3.0 period 30", dict(sd=3.0, period=30)),
    ("adaptive: 2.0, 3.0 if 30-bar range>0.25%", dict(sd_hi=3.0, vol_pct=0.25)),
    ("adaptive: 2.0, 3.0 if 30-bar range>0.35%", dict(sd_hi=3.0, vol_pct=0.35)),
    ("adaptive: 2.0, 3.0 if 30-bar range>0.50%", dict(sd_hi=3.0, vol_pct=0.50)),
    ("adaptive: 2.0, 2.5 if 30-bar range>0.35%", dict(sd_hi=2.5, vol_pct=0.35)),
    ("adaptive: 2.0, 2.5 if 30-bar range>0.50%", dict(sd_hi=2.5, vol_pct=0.50)),
)


def mode_bands(days, fills="fees"):
    """Band construction at the live exits: width in SDs, lookback, and a volatility-adaptive width."""
    base = live_cfg()
    full = [d for d in days if len(d.t) >= 300]
    part = [d for d in days if len(d.t) < 300 and d.d == date.today()]
    print(f"BAND ENTRY POINT, {len(full)} full sessions {full[0].d}..{full[-1].d}, fills={fills}, "
          f"live exits (stop {base.stop:g}% / {base.confirm:g} min)")
    print(HDR)
    for lab, kw in BANDS:
        print(row(lab, summary(run(full, replace(base, **kw), fills), full)))
    if part:
        print(f"\nPARTIAL session {part[0].d} ({len(part[0].t)} bars so far) -- P&L $ and trades")
        for lab, kw in BANDS:
            tr = run(part, replace(base, **kw), fills)
            print(f"  {lab:44s} n {len(tr):2d}  {sum(t['pnl'] for t in tr):+7.1f}  "
                  + " ".join(f"{t['opened']:%H:%M}{'C' if t['call'] else 'P'}{t['pnl']:+.0f}" for t in tr))


def mode_stopband(days, fills="fees"):
    """Stop level x confirm minutes, and a minimum band width, at the 11-03 live settings."""
    base = live_cfg()
    full = [d for d in days if len(d.t) >= 300]
    print(f"STOP / BAND WIDTH, {len(full)} full sessions {full[0].d}..{full[-1].d}, fills={fills}")
    print(HDR)
    for stop in (-5.0, -10.0, -15.0, -20.0, -30.0):
        for conf in (2.0, 5.0):
            c = replace(base, stop=stop, confirm=conf)
            print(row(f"stop {stop:g}% confirm {conf:g}m" + ("  (LIVE)" if (stop, conf) == (-5.0, 2.0) else ""),
                      summary(run(full, c, fills), full)))
    print()
    for mb in (0.0, 1.0, 1.5, 2.0, 2.5, 3.0):
        c = replace(base, min_band=mb)
        print(row(f"min band ${mb:g}" + ("  (LIVE: none)" if mb == 0 else ""),
                  summary(run(full, c, fills), full)))
    print()
    for mb in (1.5, 2.0, 2.5):
        for stop in (-10.0, -15.0):
            c = replace(base, min_band=mb, stop=stop)
            print(row(f"min band ${mb:g} + stop {stop:g}%", summary(run(full, c, fills), full)))


def mode_null(days, fills="mid", seeds=20):
    base = live_cfg()
    real = run(days, base, fills)
    n_real = len(real)
    cycles = sum(1 for d in days for t in d.t
                 if _hm(base.start) <= (t + timedelta(seconds=6)).time() < _hm(base.end))
    p = n_real / max(cycles, 1) * 1.6     # some draws are eaten by cooldowns / open positions
    outs = []
    for s in range(seeds):
        tr = run(days, base, fills, rng=random.Random(s), p_random=p)
        outs.append(summary(tr, days))
    sr = summary(real, days)
    per_trade = [o["day"] * len(days) / max(o["n"], 1) for o in outs]
    real_pt = sr["day"] * len(days) / max(sr["n"], 1)
    pct = sum(1 for x in per_trade if x < real_pt) / len(per_trade) * 100
    print(f"NULL: live exits on coin-flip entries, {seeds} seeds, fills={fills}")
    print(f"  band touch: {sr['n']} trades, {real_pt:+.2f}/trade, {sr['day']:+.2f}/day, win {sr['win']:.0f}%")
    print(f"  random:     {np.mean([o['n'] for o in outs]):.0f} trades, "
          f"{np.mean(per_trade):+.2f}/trade (5th..95th {np.percentile(per_trade, 5):+.2f}.."
          f"{np.percentile(per_trade, 95):+.2f}), win {np.mean([o['win'] for o in outs]):.0f}%")
    print(f"  band touch per trade sits at the {pct:.0f}th percentile of random entries")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    bars = load_bars()
    ds = sorted(set(bars.index.date))
    days = prep(bars, load_vix(ds))
    print(f"{len(days)} sessions {days[0].d}..{days[-1].d}\n")
    if mode in ("validate", "all"):
        mode_validate(days); print()
    if mode in ("live", "all"):
        mode_live(days); print()
    if mode in ("oneway", "all"):
        mode_oneway(days, "fees"); print()
    if mode in ("grid", "all"):
        mode_grid(days, "fees"); print()
    if mode in ("reversal", "all"):
        mode_reversal(days, "fees"); print()
    if mode in ("bands", "all"):
        mode_bands(days, "fees"); print()
    if mode in ("stopband", "all"):
        mode_stopband(days, "fees"); print()
    if mode in ("null", "all"):
        mode_null(days, "fees"); print()


if __name__ == "__main__":
    main()
