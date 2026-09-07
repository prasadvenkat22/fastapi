# Architecture — what actually runs

Current as of 2026-09-07. Read from the deployed server, not from code
defaults. Where this file and a docstring disagree, check
`docker exec app env | grep '^TRADING_'` and believe that.

`strategy_notes.txt` is the decision record — why each number is what it is.
This file is the map.

---

## The three books

| book | automation | news input | ML |
|---|---|---|---|
| **0DTE QQQ** | full — enters and exits by itself | macro terms only (see below) | none |
| **Weekly single names** | observational + one live slice | per-symbol verdict → EV overlay | none |
| **Manual / orphan** | managed once opened by hand | none | none |

---

## What runs when (America/New_York — every job checks its own clock)

| time | job | what it does |
|---|---|---|
| every minute | `run_cycle.py` | the 0DTE engine. Owns its market-hours and holiday check via `market_calendar.py` |
| 09:30 | `news_watch.py` | **scrapes the wires, then** grades everything published since the previous close, writes `news_verdicts`. Window guard 09:20–10:05 |
| 10:00 / 12:00 / 14:00 / 15:30 | `capture_chain.py` | option-chain snapshots |
| 17:15 (21:15 UTC, both DST offsets land after the close) | `macro_outcome.py` | records the morning's macro read against the session that followed |

Cron is UTC and lists both DST offsets; the scripts reject the wrong one.

---

## 0DTE QQQ — what feeds a decision

**Live gates**, evaluated every cycle, anchored to **09:45** not the open
(`TRADING_MACRO_ANCHOR_MINUTES=15` — the 09:30 print already contains the
overnight, and the engine cannot trade it anyway):

- breadth, and its trend from the session peak
- VIX **level** (22.0) and **velocity** (7% from the anchor)
- 10-year yield spike (≥4bp, rising only)
- crude spike (≥2.5%) — added 2026-09-06, knowingly against the only
  measurement of it (section 112)

**Not used:**

- **The macro LLM verdict is INERT.** `TRADING_MACRO_LLM_GATE=false`. It is
  computed hourly, recorded to `trading_macro_verdicts`, and gates nothing.
  Turned off 2026-08-25 after refusing 55 of 55 cycles on a day QQQ rose $6.50
  off its low (section 14).
**A macro news verdict now exists for QQQ, and it is recorded, not wired in.**
QQQ does not match its own ticker — matching `"qqq"` returned fund-comparison
articles ("Forget JEPQ...") because that is what an ETF ticker feed carries.
It matches `MACRO_TERMS` instead, grouped into the four vectors that move an
index: central bank and liquidity, geopolitics and commodity shocks, sovereign
debt and fixed income, systemic economic data.

Measured across consecutive sessions it flips direction correctly and reads
the transmission channel, not the headline:

| session | verdict | driver |
|---|---|---|
| 2026-09-03 | **BULLISH** 0.72 | *"yields falling, participants paring rate-hike expectations"* — Dow +635 |
| 2026-09-04 | **BEARISH** 0.72 | *"stronger-than-expected jobs report... raises rate-hike odds, pressuring rate-sensitive tech"* — Dow −250 |
| 2026-09-07 | **BEARISH** 0.72 | oil to 6-week high on Iran, Fed hike in view, yields testing 4.8% |

Good jobs data reading as bearish for equities through the rate channel is the
kind of inference a keyword count or a tree on technicals cannot reach.

**It gates nothing.** `trading_macro_verdicts` and `news_verdicts` are
accumulating so the question can be settled: did BEARISH verdicts precede down
sessions, or refuse days the engine would have won? That is the same test the
weekly overlay is built for, and it is the honest route back to the LLM gate
that was switched off on two days of evidence.
- **No ML model.** See below.

---

## Weekly single names — what feeds a decision

`scripts/weekly_pick.py`, run by hand. For every liquid strike pair:

1. **Price at fills you could get** — ask on the long leg, bid on the short,
   behind a 25%-of-mid quote-width filter.
2. **Three probability engines**, printed side by side:
   - `Pimp` — leg delta from the chain (measured accurate to <1 point,
     section 118)
   - `Phist` — the name's own drift-removed forward moves, resampled
   - `Pmc` — ATR-calibrated Monte Carlo (`sigma = ATR / 1.596`)
3. **EV two ways** — `EVdem` (drift removed) and `EVraw` (drift included).
4. **Sentiment as an overlay weight**, not a probability:

   ```
   EVadj = EVdem + w × confidence × (EVraw − EVdem)
   VERY_BULLISH +1.0 · BULLISH +0.5 · NEUTRAL 0 · BEARISH −0.5 · VERY_BEARISH −1.0
   ```

   Sign flips for puts. `EVadj` cannot leave the interval between two numbers
   already computed by other means, so a wrong verdict moves the answer to a
   figure that was on the table anyway.

**The weights are stated, not fitted.** `news_verdicts` now holds 167 graded
symbol-days over ~15 sessions — all of it, because RSS feeds carry about two
weeks and cannot be backfilled further. The sample accrues forward only.
Once a few hundred labelled days exist, score outcomes against `EVadj` at
several settings and find whether any beat `w = 0`; if none do, they go to
zero (section 120).

**The QQQ macro read has a standing bearish tilt** — 10 BEARISH of 14 graded
sessions, 5/10 on next-session direction, and the tilt survives the tape
reversing. That is the same failure that keeps `TRADING_MACRO_LLM_GATE` off
(section 125).

**It is not repetition** — re-grading all 17 sessions with re-reports dropped
changed five verdicts in both directions, 11/5/1 → 10/6/1. It is the term set:
`MACRO_TERMS` has four vectors and all four only fire on trouble. A feed
selected for alarm reports alarm daily. Fix is to balance the vectors or to
demean each verdict against the symbol's own trailing baseline. **Not yet
done** (section 126).

---

## The news pipeline

```
ingest   nodes._scrape_headlines()
         3 general feeds  +  per-ticker Yahoo & Seeking Alpha for every
         name in TRADING_MANAGE_UNDERLYING
store    market_news_vectors — Voyage embeddings, deduped, with `source`
tag      symbol_news.ALIASES — the engine holds SNDK, the wires write SanDisk
grade    classify_day() → VERY_BULLISH..VERY_BEARISH, TRADING_NEWS_MODEL,
         SINCE THE PREVIOUS SESSION'S CLOSE. Fires on a digest change.
label    news_symbol_impact — forward 1d/5d returns in percent AND in ATR
```

**The window is the previous close to now, not the calendar day** (corrected
2026-09-07, section 123). A calendar filter is wrong twice: it drops the
after-hours and weekend catalysts that are the only ones the market has not
traded on yet — SanDisk's S&P 100 inclusion, stamped Friday 22:11, was
invisible to a Monday "today only" read — and it leaks, because grading a
session's open from headlines written at 14:00 that same session is reading the
tape. `market_calendar` supplies both ends, so the window spans holidays and
respects half-day closes. Backtests must pass an explicit 09:30 cutoff.

A story that merely recaps the LAST session still grades NEUTRAL: that move is
priced, and the classifier is told so.

**Only what is NEW at 09:30 is graded** (section 126). A window of
[previous close, 09:30] still admits the wires re-reporting a standing story
for the ninth morning running, and the market priced that story weeks ago. Each
headline is scored by cosine against the same symbol's previous 10 sessions
using the Voyage embeddings already in the table — one SQL query, no model
calls — and anything at or above `TRADING_NEWS_NOVELTY` (0.83, the 75th
percentile of the observed distribution) is dropped as a re-report.

The recycling is not verbatim: median similarity to prior coverage is 0.767, so
an exact-match rule catches nothing. Re-report share: NVDA 42%, SNDK 42%,
MU 30%, QQQ 24%. 35 of 167 verdicts changed, `NEUTRAL` 101 → 120. The measured
effect is a **narrower interval, not a better point estimate** — 1-day AUC
0.546 [0.441, 0.634] → 0.551 [0.481, 0.626] — and a bearish bucket that finally
points down (+0.22% → −0.11%). The cost is that a genuine follow-up to a
covered story can be dropped with it.

**`news_watch.py` scrapes before it grades, and must.** The only other scraper
is the trading cycle, which refuses to run outside market hours — so at 09:30
the freshest row in the store is 16:00 the previous session and the overnight
window is empty by construction. Fixing the window without fixing what fills it
gives a filter that works perfectly on an empty table (section 124).

**Why per-symbol feeds are not optional.** Measured against the five events
that moved this book's names on 2026-09-04:

| catalyst | general feeds | with per-symbol |
|---|---|---|
| Nvidia / Hugging Face $13B | 9 headlines | ✅ |
| SNDK added to S&P 100 | **0** | ✅ |
| Micron HBM capacity | **0** | ✅ |
| Lynx upgrade PT $1,325 | **0** | ✅ |
| Dell NAND commentary | **0** | ✅ |

SNDK rose 11.9% that day on an index inclusion the store had no record of.

---

## Machine learning — tested, measured, not deployed

`scripts/xgb_probability.py` trains XGBoost on 11 daily technical features,
10 names, 5 years, 10,264 rows, **time-ordered split** (shuffling leaks: rows
4 days apart share an outcome window).

```
                 TRAIN     TEST      baseline
accuracy         75.3%     50.7%     59.7%  (always "up")
AUC              0.847     0.496     0.500  (coin flip)
Brier           0.1913    0.2683    0.2449  (base rate)
```

**It loses to every baseline on every metric.** Not a sample-size problem —
daily technical state does not predict 4-day direction. Asked for a live read
on 2026-09-07 it returned SNDK 39.6% (bearish) on the afternoon of the S&P 100
inclusion.

The proposed `PoP = 0.40×P_model + 0.60×Delta` is also malformed: the model
outputs P(any up move), delta outputs P(beyond a specific strike). Averaging
them is not a probability of anything.

**Sentiment was added as a feature and it made the model worse**
(section 125, `scripts/xgb_sentiment.py`). Identical rows, identical
walk-forward split, four sentiment columns:

```
horizon        technicals only   + sentiment   difference
4 sessions           0.493          0.462        -0.031
1 session            0.450          0.447        -0.004
```

Sentiment *alone* as a score scores AUC 0.546 [0.441, 0.634] at 1 day and
0.532 [0.405, 0.641] at 4 days — day-clustered bootstrap, 0.500 inside both.
A tree cannot extract what is not in the column.

**Collinearity, since it comes up:** `atr_pct`/`rv20` +0.95, `rsi14`/`dist_sma20`
+0.93; 11 columns carry ~6 independent dimensions. A tree is unharmed by that —
no matrix to invert — but the importance table above is a report on tie-breaking,
not a ranking of causes. Six dimensions of public, priced state is still why it
loses to a constant.

**Greeks are not a way out.** Delta, gamma, theta and vega are deterministic in
spot, strike, time, rate and IV. Feeding them re-encodes inputs the model
already has, and delta *is* the market's probability — accurate to inside one
point across 525 strikes (section 118).

---

## Analysis scripts — none of them trade

| script | question |
|---|---|
| `iv_rv_screen.py` | where are options cheap vs realised vol |
| `weekly_pick.py` | which vertical has the best drift-corrected EV |
| `delta_calibration.py` | is market delta a well-calibrated probability |
| `xgb_probability.py` | does a learned model beat it (no) |
| `xgb_sentiment.py` | does adding sentiment beat the same model without it (no) |
| `sentiment_signal_test.py` | does the graded verdict carry information at all, with a day-clustered interval; `--novelty X` re-grades with re-reports dropped |
| `novelty_check.py` | how much of a symbol's window is recycled coverage, and does dropping it move the verdict |
| `backfill_news_impact.py` | label stored headlines with what price did |
| `macro_outcome.py --report` | did the macro verdict separate sessions, and what would a gate have cost |
| `news_ev_backtest.py` | re-price an expired `weekly_shadow` cohort and ask whether the news overlay moved EV toward the outcome |
| `sweep.py` | 0DTE replay. **Read the RUN CONFIG banner** |

---

## Tables

| table | holds |
|---|---|
| `trading_open_positions` / `trading_history` | live positions and closed trades |
| `weekly_shadow` | every weekly structure, with `sig_*` columns: ATR, VWAP, net greeks, RV/IV, news count |
| `market_news_vectors` | headlines + Voyage embeddings + source |
| `news_symbol_impact` | headline × symbol × forward return in ATR |
| `news_verdicts` | one graded verdict per symbol per trading day |
| `trading_macro_verdicts` | append-only macro read history |
| `trading_macro_readings` | VIX and 10Y per cycle |
| `macro_session_outcomes` | one row per session: morning verdicts vs QQQ's move and the engine's P&L |

---

## The rule that keeps this honest

**Nothing in the news or ML path gates a trade.** On the 365 labelled rows in
`news_symbol_impact`, news mentions do not predict a move — forward moves sit
inside ±0.6 ATR with larger standard deviations, and 35% of the sample is one
trending name. Sentiment reaches the weekly EV as a bounded overlay and
reaches the 0DTE book not at all.

That is the same discipline section 22 applied to crude and section 14 to the
macro verdict: an unmeasured term is logged beside the decision, never wired
into it.

**The first cohort with realized outcomes went against the overlay**
(section 124). On 2026-08-28, 24 structures, 2 losers. SNDK's short call
carried `EVraw −96.4` — the most negative figure in the cohort — and expired at
−1011%; the news window was empty, so `w = 0` collapsed `EVadj` to `EVdem`
−48.5 and deleted the warning the drift term had already produced. On META a
BEARISH verdict moved `EVadj` *up*, on the structure that lost 921%.

`EVfloor = min(EVdem, EVraw)` is reported alongside. It needs no verdict, and it
moves SNDK's short call from third-worst to worst — but also flags CRWV and QQQ
calls that paid +100%. Two losers is not a sample. It is computed on every run
and wired into nothing.
