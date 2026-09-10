# Architecture — what actually runs

Current as of 2026-09-10. Read from the deployed server, not from code
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
| every 5 min, 09:30–16:00 | `price_alert.py` | level crossings, fires once per crossing. Live rules: `SNDK<1762`, `SNDK>1762`, `SNDK<1700`, `CRWV<95` |
| every 5 min, 09:30–16:00 | `profit_stall.py` | a winner giving back 5% from peak, after 15 min quiet. Decides on intrinsic |
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
changed five verdicts in both directions, 11/5/1 → 10/6/1. Nor is it the term set — the
vectors were balanced (60 → 83 terms: ceasefire, disinflation, dovish, bond
rally, soft landing, guidance raised …) and **zero verdicts changed**. Only 15
extra headlines matched. The expansionary copy is not being filtered out, it is
**not on the wire**: financial media reports risk. The tilt is a property of the
source, and no retrieval rule fixes a corpus without the other side
(section 127). The remaining candidate is demeaning each verdict against the
symbol's own trailing baseline — **not done, and not worth doing until the
re-run wobble is characterised**.

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
MU 30%, QQQ 24%. 35 of 167 verdicts changed, `NEUTRAL` 101 → 120. **Its measured effect on
prediction is unknown and probably unmeasurable here** — two runs at the same
threshold gave 1-day AUC 0.551 and 0.536, and the bearish bucket changed sign
between them (−0.11% → +0.33%). That wobble is the classifier, not the data:
Haiku returns different verdicts on the same headlines run to run, by as much
as any effect being tested (section 127). The filter is kept on the principle,
not on a measurement. The cost is that a genuine follow-up to a covered story
can be dropped with it.

**Any future A/B on this pipeline must grade each configuration several times
and report the spread.** A difference smaller than the re-run wobble is not a
difference.

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

## Was the session bought, or did it drift up

Five hourly-bar columns on every `weekly_shadow` row (`sig_vwap_slope_pct`,
`sig_bars_above_vwap`, `sig_ad_volume_ratio`, `sig_volume_vs_adv`,
`sig_close_location`), section 128. A daily bar cannot tell a bought session
from one that merely closed higher:

| | ADV | VWAP slope | bars above | A/D |
|---|---|---|---|---|
| SNDK 09-03, price up | 0.68x | +0.76% | 85.7% | **-0.05** |
| SNDK 09-04, price up | 1.16x | +2.56% | **100%** | **+0.66** |

Same direction, opposite character. On 09-04 it separates SNDK and MU (real
catalysts, accumulated) from NVDA and QQQ (sold from the open). Eight
observations, so a demonstration and not evidence.

**It does not measure institutional buying.** Every buyer has a seller and no
public OHLCV feed distinguishes them; this measures *urgency*. Real
participation needs closing-auction volume, block prints or 13Fs, none of them
reachable. The route that is reachable: **differencing option open interest
across the chain snapshots `capture_chain.py` already takes 4x a session.**
Not built.

Observational. Nothing gates on them.

**Two ways to ask who is buying, and they are not the same question**
(section 129):

- `flow.py` — **urgency**, from the tape. Net signed volume (tick rule at bar
  resolution) beside the VWAP line. Any symbol, works now.
- `oi_flow.py` — **positioning**, from open interest. Counts contracts that
  exist rather than inferring intent. Still cannot say long or short.

**Read signed volume and VWAP together or not at all.** SNDK on 2026-09-08 ran
74% up-volume and net +1.28M shares with price *below* a flat VWAP — buyers
active and not winning, a far weaker picture than the signed number alone. On
09-04 the two agreed: VWAP +2.56%, 100% of bars above it.

`capture_chain.py` never captured open interest — 42 snapshots of
`[strike, c|p, bid, ask, iv, delta]` and no OI field. Rows now append
`open_interest, volume`; nothing is backfillable, so the series starts
2026-09-08. OI is published once daily, so `oi_flow.py` compares one snapshot
**per date**.

`weekly_pick.py` prints a `flow` column beside every candidate and a
**FLOW CONFLICT** marker for a long structure into a tape being sold (or puts
into one being bought), next to the existing NEWS CONFLICT. It changes nothing
— not EV, not the probabilities, not the ordering. `BUY`/`SELL` require **four** readings to agree — price on the right side
of VWAP, VWAP sloping that way, >60% of bars on that side, and >55%
up-volume; anything else is `MIXED`. Tightened 2026-09-08 after SNDK
printed `BUY` on +0.10% vs VWAP with the slope *down* and 50% of bars
above. One function, `flow.flow_label`, imported by both the CLI and the
endpoint — it was written twice and would have drifted (section 131). On its first
run it caught CRWV reading news `BEARISH` against a tape at `BUY 81%` — the two
unscored signals contradicting each other, which is why neither sizes anything
(section 130).

Out of reach, and worth not re-proposing: block and dark-pool prints (paid
feed), 13F (quarterly, 45-day lag), Form 4 (insiders, not institutions).

---

## HTTP: the screener is callable

```
GET  /trading/screener/verticals?symbols=CRWV,AVGO&side=call&structure=debit&by=edge&per_symbol=2
GET  /trading/screener/flow?symbols=CRWV,AVGO,SNDK
POST /trading/flatten?confirm=LIQUIDATE&preview=true
POST /trading/flatten?confirm=LIQUIDATE&preview=false&plan_token=<from the preview>
```

**`/flatten` closes MANUAL spreads only** — engine positions are excluded by
passing their legs as `engine_symbols`, the same way `orphans.review()` does.
Four guards, each from an incident (sections 140–141):

| guard | why |
|---|---|
| structures, never legs | legging out turns a long into a **naked short** |
| clamped to holdings | the pairing said `SNDK 1750/1800 x5` when three existed |
| paired from holdings too | a spread whose opening order aged out of the count-limited window is **invisible**, not stale |
| market hours + `plan_token` | the same plan previewed at **$15,297** at 09:20 and **$6,721** at 09:33; and a boolean was one character between looking and trading |

The token fingerprints the plan, so execution can only follow a preview of
*that* plan — if the market moves, it stops matching and you get a fresh
preview instead of a surprise fill.

**The kill switch is not this.** `KILL_SWITCH.txt` halts the engine from
deciding and leaves every position open.

Behind `require_trading`, both GET, neither trades. Capped at 12 symbols — each
costs a daily-bar, chain and intraday fetch against one production worker.

`trading_engine/screener.py` is an **import shim** onto `scripts/weekly_pick.py`,
so the API and CLI cannot drift. `structure=debit` is the **buy** list, `structure=credit` the **sell** list —
one ranking across both, because `cost` is set to max risk either way
(`width − credit` for a credit), so `need`, `rr` and `edge` keep their meaning.

**The direction flips with the structure and this is the trap:** a call *debit*
spread is bullish, a call *credit* spread is **bearish**. The news guard, the
flow guard and the P(max) branch all keyed on `side` and would have been exactly
inverted; they now key on `direction(side, structure)`, and every row carries an
explicit `direction` field (section 132). `p_imp` is `1 − delta` for credits.

`per_symbol` caps rows per name — without it a screen over CRWV, AVGO and SNDK
returned twelve CRWV rows and nothing else.

`by` accepts `edge|ev|evpct|prob` and
**defaults to `edge`** (`Pwin − need`): the other three each top their own
ranking with a structure nobody should take — `prob` finds deep-ITM verticals
whose reward is spent (`need` 100%, EV −62.8), `evpct` finds 1:39 lottery
tickets at a 6.7% hit rate. Edge asks whether you are *paid for the odds*
(section 131).

news and flow are returned per row with their conflict flags, and neither
touches the ordering or the EV.

**Production runs `--workers 1`, no `--reload`** — new routes need a container
restart.

---

## Analysis scripts — none of them trade

| script | question |
|---|---|
| `iv_rv_screen.py` | where are options cheap vs realised vol |
| `weekly_pick.py` | which vertical has the best drift-corrected EV; prints news and tape flow beside each candidate, using neither |
| `delta_calibration.py` | is market delta a well-calibrated probability |
| `xgb_probability.py` | does a learned model beat it (no) |
| `xgb_sentiment.py` | does adding sentiment beat the same model without it (no) |
| `sentiment_signal_test.py` | does the graded verdict carry information at all, with a day-clustered interval; `--novelty X` re-grades with re-reports dropped |
| `novelty_check.py` | how much of a symbol's window is recycled coverage, and does dropping it move the verdict |
| `backfill_news_impact.py` | label stored headlines with what price did |
| `macro_outcome.py --report` | did the macro verdict separate sessions, and what would a gate have cost |
| `news_ev_backtest.py` | re-price an expired `weekly_shadow` cohort and ask whether the news overlay moved EV toward the outcome |
| `flow.py` | net signed volume and VWAP for any symbol, from Tradier intraday bars |
| `oi_flow.py` | which strikes gained open interest day over day |
| `sweep.py` | 0DTE replay. **Read the RUN CONFIG banner** |

---

## Tables

| table | holds |
|---|---|
| `trading_open_positions` / `trading_history` | live positions and closed trades |
| `weekly_shadow` | every weekly structure, with `sig_*` columns: ATR, VWAP, net greeks, RV/IV, news count, intraday accumulation |
| `market_news_vectors` | headlines + Voyage embeddings + source |
| `news_symbol_impact` | headline × symbol × forward return in ATR |
| `news_verdicts` | one graded verdict per symbol per trading day |
| `trading_macro_verdicts` | append-only macro read history |
| `trading_macro_readings` | VIX and 10Y per cycle |
| `macro_session_outcomes` | one row per session: morning verdicts vs QQQ's move and the engine's P&L |

---

## What manages an open position

`orphans.py`, every cycle. Current settings:

```
TRADING_ORPHAN_UNDERLYING=          empty = EVERY symbol
TRADING_ORPHAN_HOLD_UNTIL=09:30     acts from the opening bell
TRADING_ORPHAN_ACT_EXPIRY_DAY_ONLY=false
TRADING_ORPHAN_LATER_STALL_ARM=5    arm on any modest profit
TRADING_ORPHAN_LATER_STALL_GIVEBACK=15
TRADING_ORPHAN_LATER_STALL_MINUTES=5
TRADING_ORPHAN_LATER_TARGET_PCT=0.75
```

**It cannot sell at a loss.** `books_a_gain` compares the *mark* to entry and
`STALL_MUST_BOOK_A_GAIN` is on, which is what makes a tight give-back safe: the
worst case is leaving a winner early, never a realised loss (section 137).

**The peak trails and never ratchets down.** Each new high raises `peak_iv` and
restarts the quiet clock, so a structure still making highs cannot trip. Stored
in **dollars of intrinsic**, not return percent — a scaled-up entry once made an
untouched 20.00 of intrinsic re-read 72.4% then 51.5%, a 20.9-point phantom
give-back. Peaks survive deploys.

**0DTE positions use a different ladder.** `zero_dte` switches on the −40% stop
and the 15:45 flatten and switches `STALL_LATER` off; the verdict line says
which is live.

Worked, unattended, on 2026-09-10: `SNDK 1675/1725 x3` peaked at +89.2%, gave
back past 15 points with 5 minutes since the last high, and booked **+$771**.

**Fill prices come from cost basis, not order reconstruction** (section 141).
`filled_legs()` signs quantity by open-vs-close, so a `sell_to_open` counts as a
long open and blends its premium into genuine longs — a rolled strike returns a
price belonging to neither position (`1725C 69.60 x1` against the account's
`23.20 x3`). Where they disagree the account wins, and the override is logged.

---

## Alerts

`scripts/price_alert.py --rules "SNDK<1800"`, cron every 5 minutes in hours.
Fires **once** per crossing; re-arms only when price recovers past the level by
0.25%. Every firing is recorded in `data/price_alerts.json` **before** delivery
is attempted.

**The droplet cannot send email.** ufw allows outgoing, but the provider blocks
SMTP egress — `smtp.gmail.com:587` and `:465` both time out from the *host*,
while `:443` connects. This also means `helpers/mailer.py`, which the auth
router uses for forgot-password and reset-password, **has never been able to
deliver from this host** and fails silently (returns False, logs). Fix by
requesting SMTP unblocking, pointing the mailer at an HTTP mail API, or using
`ALERT_WEBHOOK_URL` (section 133).

`profit_stall.py --giveback 5` watches broker positions for a **winner that is
turning**: 5% below peak, but only after **15 minutes since the last new high**
(`ORPHAN_LATER_STALL_MINUTES`) and only when the exit would `books_a_gain`.
Under water is a stop's question, not a stall's. It decides on **intrinsic** and
prints the mark — six legs at 1.30–1.90 wide is several hundred dollars of
quote noise on a $17k position (section 134). **It alerts; it does not trade.**

A trigger that must not be missed belongs at the **broker**, not here.

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
