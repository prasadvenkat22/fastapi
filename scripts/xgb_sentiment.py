"""Does adding sentiment to the XGBoost feature set beat the same model without it?

THE COMPARISON IS THE POINT. Two models, identical rows, identical split, and
the only difference is four sentiment columns. An AUC from a sentiment model
alone means nothing on its own -- the rows themselves may be easy or hard, and
without the without-it number beside it there is nothing to attribute the
score to.

A CORRECTION TO THE PREMISE THIS TEST WAS ASKED UNDER. xgb_probability.py never
had news as a feature. Its 0.496 test AUC came from eleven daily technical
columns failing to predict four-day direction; the news timestamp bug
(section 123) never touched its inputs, so this is not a re-run of a leaky
experiment. It is the first run with sentiment in it at all.

WALK-FORWARD, NOT A 70/30 SPLIT. A third of ~110 rows is a test set of five
sessions. Walk-forward trains on every session before day k and scores day k,
so every session after a burn-in gets tested and no model ever trains on a day
that follows the one it is scoring.

    python scripts/xgb_sentiment.py --days 4
    python scripts/xgb_sentiment.py --days 1 --threshold 0.0
"""

from __future__ import annotations

import argparse
import os
import sys

from datetime import date

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xgb_probability import DEFAULT_SYMS, build

# Ordinal, not one-hot: the classes are ordered, and there are nowhere near
# enough rows to spend a degree of freedom on each level.
ORD = {"VERY_BEARISH": -2.0, "BEARISH": -1.0, "NEUTRAL": 0.0,
       "BULLISH": 1.0, "VERY_BULLISH": 2.0}


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")


def sentiment_rows(csv_path: str = "") -> dict:
    """Verdicts from the database, or from a CSV dumped out of it.

    The CSV path exists because sklearn and xgboost are deliberately NOT
    installed in the production container -- the engine does not need them and
    a trading box should not carry a build toolchain it never calls. So the
    verdicts come out and the model runs where the libraries are.
    """
    if csv_path:
        out = {}
        with open(csv_path, encoding="utf-8") as fh:
            for line in fh:
                p = line.rstrip().split("|")
                if len(p) < 5 or not p[1]:
                    continue
                y, m, d = (int(x) for x in p[1].split("-"))
                out[(p[0].upper(), date(y, m, d))] = (
                    ORD.get(p[2], 0.0), float(p[3] or 0), int(p[4] or 0))
        return out
    with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol, trading_day, verdict, confidence, "
                    "headline_count FROM news_verdicts")
        return {(s.upper(), d): (ORD.get(v, 0.0), float(c or 0.0), int(n or 0))
                for s, d, v, c, n in cur.fetchall()}


def collinearity(df: pd.DataFrame, feat: list) -> None:
    """How many INDEPENDENT dimensions does this feature set actually carry?

    TREES DO NOT SUFFER THE WAY A REGRESSION DOES. There is no matrix to
    invert, so no variance inflation, no unstable coefficients, and predictive
    accuracy is essentially unaffected by correlated inputs. What collinearity
    DOES wreck is the importance ranking: correlated columns split the credit
    between them arbitrarily, so a "top features" table becomes a report on
    tie-breaking rather than on causes. That is the number to distrust.
    """
    X = df[feat].to_numpy(dtype=float)
    X = (X - X.mean(0)) / (X.std(0) + 1e-12)
    C = np.corrcoef(X, rowvar=False)
    ev = np.linalg.eigvalsh(C)[::-1]
    var = np.cumsum(ev) / ev.sum()

    bar = "=" * 74
    print("")
    print(bar)
    print(f"FEATURE COLLINEARITY - {len(feat)} columns")
    print(bar)
    pairs = [(feat[i], feat[j], C[i, j])
             for i in range(len(feat)) for j in range(i + 1, len(feat))
             if abs(C[i, j]) > 0.7]
    print("pairs correlated above |0.70|:")
    for a, b, r in sorted(pairs, key=lambda x: -abs(x[2])):
        print(f"  {a:14s} {b:14s} {r:+.2f}")
    if not pairs:
        print("  (none)")
    k90 = int(np.searchsorted(var, 0.90) + 1)
    k95 = int(np.searchsorted(var, 0.95) + 1)
    print("")
    print(f"principal components for 90% of variance: {k90} of {len(feat)}")
    print(f"principal components for 95% of variance: {k95} of {len(feat)}")
    print(f"So {len(feat)} columns carry roughly {k90} independent dimensions.")
    print("That is NOT why the model fails -- a tree handles correlated inputs")
    print("fine -- but it does mean the importance table is not a ranking of")
    print(f"causes, and that {k90} dimensions of public, priced technical state")
    print(f"is a thinner input than {len(feat)} column names suggest.")


def walk_forward(df: pd.DataFrame, feat: list):
    """Expanding window: train on every session before day k, score day k."""
    from sklearn.metrics import roc_auc_score
    from xgboost import XGBClassifier

    days = sorted(df["day"].unique())
    burn = max(4, len(days) // 3)
    ps, ys = [], []
    for k in range(burn, len(days)):
        tr = df[df["day"] < days[k]]
        te = df[df["day"] == days[k]]
        if te.empty or tr["y"].nunique() < 2:
            continue
        m = XGBClassifier(n_estimators=120, max_depth=3, learning_rate=0.05,
                          subsample=0.8, colsample_bytree=0.8,
                          eval_metric="logloss", reg_lambda=4.0, random_state=7)
        m.fit(tr[feat], tr["y"])
        ps.extend(m.predict_proba(te[feat])[:, 1])
        ys.extend(te["y"].tolist())
    if len(set(ys)) < 2:
        return None, 0
    return float(roc_auc_score(ys, ps)), len(ys)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--verdicts-csv", default="",
                    help="pipe-delimited dump of news_verdicts, for "
                         "running where sklearn is installed")
    args = ap.parse_args()
    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            or DEFAULT_SYMS)

    sent = sentiment_rows(args.verdicts_csv)
    print(f"{len(sent)} graded symbol-days in news_verdicts")

    frames = []
    for s in syms:
        try:
            d = build(s, args.days, args.threshold)
        except Exception as exc:
            print(f"  {s}: {exc}")
            continue
        if d is None:
            continue
        d = d.copy()
        # build() keeps yfinance's tz-aware DatetimeIndex; news_verdicts
        # is keyed by a plain date. Without this the join silently
        # matches nothing and the script reports "no rows".
        d["day"] = [x.date() if hasattr(x, "date") else x for x in d.index]
        d["sym"] = s
        d = d[[(s, x) in sent for x in d["day"]]]
        if d.empty:
            continue
        d["news_ord"] = [sent[(s, x)][0] for x in d["day"]]
        d["news_conf"] = [sent[(s, x)][1] for x in d["day"]]
        d["news_n"] = [sent[(s, x)][2] for x in d["day"]]
        d["news_signed"] = d["news_ord"] * d["news_conf"]
        frames.append(d)

    if not frames:
        print("no rows carry both a price history and a verdict")
        return
    df = pd.concat(frames).sort_values("day")
    base = [c for c in df.columns
            if c not in ("y", "sym", "day") and not c.startswith("news_")]
    withn = base + ["news_ord", "news_conf", "news_n", "news_signed"]

    print(f"{len(df)} rows over {df['day'].nunique()} sessions, "
          f"{df['sym'].nunique()} names")
    print(f"target: +{args.threshold*100:.1f}% over {args.days} sessions, "
          f"base rate (up) {df['y'].mean()*100:.1f}%")

    collinearity(df, base)

    a0, n0 = walk_forward(df, base)
    a1, n1 = walk_forward(df, withn)
    nan = float("nan")
    print("")
    print(f"{'model':30s} {'walk-forward AUC':>18s} {'scored rows':>12s}")
    print(f"{'technicals only':30s} {(a0 if a0 else nan):18.3f} {n0:12d}")
    print(f"{'technicals + sentiment':30s} {(a1 if a1 else nan):18.3f} {n1:12d}")
    if a0 and a1:
        print(f"{'difference':30s} {a1 - a0:+18.3f}")
    print("")
    print("A DIFFERENCE THIS SIZE OVER THIS MANY SESSIONS IS NOT A RESULT.")
    print("scripts/sentiment_signal_test.py puts a day-clustered 95% interval")
    print("on the sentiment column by itself and that interval contains 0.500.")
    print("A tree cannot extract information that is not in the column, so the")
    print("number above is bounded by the one over there.")


if __name__ == "__main__":
    main()
