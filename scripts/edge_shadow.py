"""Edge check: the Friday weekly CREDIT shadow, settled from real closes.

Input: scratch/edge/weekly_shadow.csv, dumped read-only from the droplet:
  copy (select symbol,strategy,expiration,opened_at,short_strike,long_strike,
        put_short_strike,put_long_strike,width,spot_at_entry,short_delta,short_iv,
        entry_credit_mid,entry_credit_natural,entry_spread_width,last_value_mid,
        expiry_value,expiry_return_pct,sig_vwap_side,sig_rv_iv_ratio,sig_rv20,
        sig_atr14,live_order_id from weekly_shadow order by opened_at)
  to stdout with csv header

Each row is re-settled at the underlying's close on its expiration date
(yfinance daily). P&L per 1-lot at the MID credit and at the NATURAL credit
(what selling actually collects). No exit cost at expiry (cash value).
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(HERE, "..", "scratch", "edge", "weekly_shadow.csv")


def main() -> None:
    df = pd.read_csv(CSV)
    syms = sorted(df.symbol.unique())
    px = yf.download(syms, start="2026-08-20", end="2026-10-04", progress=False,
                     auto_adjust=False)["Close"]
    px.index = px.index.strftime("%Y-%m-%d")

    def close(r):
        try:
            return float(px.loc[r.expiration, r.symbol])
        except KeyError:
            return np.nan

    df["close_exp"] = df.apply(close, axis=1)
    df = df[df.close_exp.notna()].copy()

    def value(r):
        v = 0.0
        if not np.isnan(r.short_strike):
            v += min(max(r.close_exp - r.short_strike, 0.0), r.width)
        if not np.isnan(r.put_short_strike):
            v += min(max(r.put_short_strike - r.close_exp, 0.0), r.width)
        return v

    df["val"] = df.apply(value, axis=1)
    df["pnl_mid"] = (df.entry_credit_mid - df.val) * 100
    df["pnl_nat"] = (df.entry_credit_natural - df.val) * 100
    # Max risk: width less credit (a condor risks one side's width).
    df["risk"] = (df.width - df.entry_credit_natural) * 100
    df["ror_nat"] = df.pnl_nat / df.risk
    chk = df[df.expiry_value.notna()]
    print(f"check vs DB settle: {len(chk)} rows, "
          f"mean |my value - DB expiry_value| = {np.abs(chk.val - chk.expiry_value).mean():.3f}")
    print(f"rows settled from closes: {len(df)}  expiries: {sorted(df.expiration.unique())}")
    print(f"symbols: {', '.join(syms)}")

    exps = sorted(df.expiration.unique())
    half = exps[: len(exps) // 2 + len(exps) % 2]

    def summ(g):
        return pd.Series(dict(
            n=len(g), mid_per=g.pnl_mid.mean(), nat_per=g.pnl_nat.mean(),
            win=(g.pnl_nat > 0).mean() * 100, worst=g.pnl_nat.min(),
            ror=g.ror_nat.mean() * 100, total_nat=g.pnl_nat.sum(),
            h1=g[g.expiration.isin(half)].pnl_nat.mean(),
            h2=g[~g.expiration.isin(half)].pnl_nat.mean(),
            weeks_pos=int((g.groupby("expiration").pnl_nat.sum() > 0).sum()),
            weeks=g.expiration.nunique()))

    pd.set_option("display.width", 200)
    print("\nAll names incl. QQQ, per 1-lot, $ (nat = natural credit):")
    print(df.groupby("strategy").apply(summ).round(1).to_string())
    print("\nWorst week (sum across names) per variant:")
    wk = df.groupby(["strategy", "expiration"]).pnl_nat.sum().unstack(0).round(0)
    print(wk)
    print("\nBy vwap side (natural):")
    print(df.groupby(["strategy", "sig_vwap_side"]).pnl_nat.agg(["count", "mean"]).round(1))
    # Bootstrap over WEEKS (rows inside a week are one market move, not independent).
    rng = np.random.default_rng(0)
    print("\nWeek-block bootstrap of mean $/row (natural), 5000 draws:")
    for s, g in df.groupby("strategy"):
        by = [x.pnl_nat.values for _, x in g.groupby("expiration")]
        means = []
        for _ in range(5000):
            pick = rng.integers(0, len(by), len(by))
            means.append(np.concatenate([by[i] for i in pick]).mean())
        means = np.array(means)
        print(f"  {s:14s} P(mean<=0) = {(means <= 0).mean():.2f}   5-95%: "
              f"{np.percentile(means, 5):+.0f} .. {np.percentile(means, 95):+.0f}")


if __name__ == "__main__":
    sys.exit(main())
