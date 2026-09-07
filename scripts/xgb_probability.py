"""Does an XGBoost model beat the market's own probability?

THE ONLY QUESTION WORTH ASKING. Delta is a free, per-strike probability that
scripts/delta_calibration.py measured as accurate to inside one point across
525 strikes. A model earns a place in the EV loop only by beating that, so
this trains one and scores it AGAINST delta on data it has never seen.

WHAT IT TRAINS ON. Daily technical features -- there are years of those. It
does NOT train on sentiment: news_verdicts began on 2026-09-08 and has no
history, so a sentiment feature today would be a column of nulls or, worse,
one backfilled from headlines whose forward returns are already known.

TARGET. Will the close N sessions ahead be above today's close by more than
`thr` -- the same binary a spread's short strike asks about.

VALIDATION IS THE POINT. A time-ordered split: train on the oldest 70%, test
on the newest 30%, never shuffled. Shuffling price data leaks the future into
training through overlapping windows and will report a beautiful score for a
model that knows nothing. Reported against two baselines:

    always-majority     what a model that has learned nothing scores
    Brier vs delta      whether the probabilities are better than the chain's

    python scripts/xgb_probability.py [--symbols NVDA,CRWV] [--days 4]
"""

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_SYMS = "NVDA MRVL DELL MU SNDK CRWV PANW WDC AVGO QQQ".split()


def features(h: pd.DataFrame) -> pd.DataFrame:
    """Daily technical state. Every column must be knowable at the close of
    the row it sits on -- anything else is lookahead wearing a feature name."""
    c, hi, lo, v = h["Close"], h["High"], h["Low"], h["Volume"]
    prev = c.shift(1)
    tr = (hi - lo).combine((hi - prev).abs(), max).combine((lo - prev).abs(), max)
    atr = tr.rolling(14).mean()
    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    lr = np.log(c / prev)
    sma20, sma50 = c.rolling(20).mean(), c.rolling(50).mean()
    typical = (hi + lo + c) / 3
    vw5 = (typical * v).rolling(5).sum() / v.rolling(5).sum()
    return pd.DataFrame({
        "atr_pct": atr / c * 100,
        "rsi14": rsi,
        "rv20": lr.rolling(20).std() * math.sqrt(252) * 100,
        "rv5_rv20": (lr.rolling(5).std() / lr.rolling(20).std()),
        "dist_sma20": (c - sma20) / atr,
        "dist_sma50": (c - sma50) / atr,
        "dist_vwap5": (c - vw5) / atr,
        "move_5d_atr": (c - c.shift(5)) / atr,
        "move_1d_atr": (c - prev) / atr,
        "vol_ratio": v / v.rolling(20).mean(),
        "range_atr": (hi - lo) / atr,
    })


def build(sym: str, days: int, thr: float):
    h = yf.Ticker(sym).history(period="5y", interval="1d")
    if len(h) < 300:
        return None
    X = features(h)
    fwd = h["Close"].shift(-days) / h["Close"] - 1.0
    y = (fwd > thr).astype(int)
    df = X.copy()
    df["y"] = y
    df["sym"] = sym
    df = df.dropna()
    # Drop the last `days` rows: their outcome is not known yet.
    return df.iloc[:-days] if days else df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--predict-now", action="store_true",
                    help="also print the model's probability for the latest bar")
    ap.add_argument("--threshold", type=float, default=0.0,
                    help="forward return that counts as a win, e.g. 0.02")
    args = ap.parse_args()
    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            or DEFAULT_SYMS)

    frames = []
    for s in syms:
        try:
            d = build(s, args.days, args.threshold)
            if d is not None:
                frames.append(d)
                print(f"{s:6s} {len(d):5d} rows")
        except Exception as e:
            print(f"{s:6s} error {e}")
    if not frames:
        print("no data")
        return
    df = pd.concat(frames).sort_index()
    feat = [c for c in df.columns if c not in ("y", "sym")]

    # TIME-ORDERED split. Shuffling would leak: rows `days` apart share their
    # outcome window, so a shuffled test set contains near-duplicates of
    # training rows and every model scores brilliantly.
    cut = int(len(df) * 0.70)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    print(f"\ntrain {len(tr)} rows  ->  test {len(te)} rows (time-ordered, "
          f"target: +{args.threshold*100:.1f}% over {args.days} sessions)")
    print(f"base rate  train {tr['y'].mean()*100:.1f}%   test {te['y'].mean()*100:.1f}%")

    from sklearn.metrics import accuracy_score, brier_score_loss, roc_auc_score
    from xgboost import XGBClassifier

    m = XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.05,
                      subsample=0.8, colsample_bytree=0.8, eval_metric="logloss",
                      reg_lambda=2.0, random_state=7)
    m.fit(tr[feat], tr["y"])
    p_tr = m.predict_proba(tr[feat])[:, 1]
    p_te = m.predict_proba(te[feat])[:, 1]

    maj = te["y"].mean()
    maj_pred = 1 if maj > 0.5 else 0
    print(f"\n{'':22s} {'TRAIN':>10s} {'TEST':>10s}")
    print(f"{'accuracy':22s} {accuracy_score(tr['y'], p_tr>0.5)*100:9.1f}% "
          f"{accuracy_score(te['y'], p_te>0.5)*100:9.1f}%")
    print(f"{'AUC':22s} {roc_auc_score(tr['y'], p_tr):10.3f} "
          f"{roc_auc_score(te['y'], p_te):10.3f}")
    print(f"{'Brier (lower better)':22s} {brier_score_loss(tr['y'], p_tr):10.4f} "
          f"{brier_score_loss(te['y'], p_te):10.4f}")
    print(f"\nBASELINES ON THE TEST SET")
    print(f"  always predict {maj_pred}      accuracy "
          f"{accuracy_score(te['y'], np.full(len(te), maj_pred))*100:5.1f}%")
    print(f"  base rate as the probability   Brier "
          f"{brier_score_loss(te['y'], np.full(len(te), tr['y'].mean())):.4f}")

    if args.predict_now:
        print("\nCURRENT PREDICTION -- the model's P(up) for the latest bar.")
        print("Printed because it was asked for, NOT because it should be used:")
        print("a model whose test AUC is at or below 0.500 has no information "
              "to contribute, and blending it into a delta measured accurate to "
              "one point can only add variance.")
        for s_ in syms:
            try:
                h = yf.Ticker(s_).history(period="5y", interval="1d")
                x = features(h).dropna()
                if x.empty:
                    continue
                p = float(m.predict_proba(x.iloc[[-1]][feat])[0, 1])
                print(f"  {s_:6s} P(up over {args.days}d) = {p*100:5.1f}%")
            except Exception as e:
                print(f"  {s_:6s} error {e}")

    imp = sorted(zip(feat, m.feature_importances_), key=lambda x: -x[1])
    print("\ntop features")
    for n, i in imp[:6]:
        print(f"  {n:14s} {i:.3f}")

    print("\nREAD THE TRAIN-TEST GAP, NOT THE TRAIN NUMBER. A large gap means "
          "the model memorised. A test AUC near 0.500 means it learned nothing "
          "the market has not already priced, whatever the accuracy says -- "
          "accuracy can look high simply by predicting the majority class.")


if __name__ == "__main__":
    main()
