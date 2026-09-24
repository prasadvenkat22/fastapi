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
| every 15 min, 09:00–16:45 | `macro_objective.py` | the macro read from PRICES: crude + 10Y + VIX → one signed score. Stored beside the text read, **gates nothing** |
| hourly at :12, 09:12–16:12 | `news_enrich.py` | macro leg: RSS → feedparser → GUID dedupe → promo regex → **one Gemini call** → topic clustering → one QQQ macro row. No HuggingFace, nothing installed |
| hourly at :25, 09:25–16:25 | `news_watch.py` | grades everything published since the previous close, writes `news_verdicts` (current) **and `news_verdict_history` (append-only)**. Window guard 09:20–16:00, `TRADING_NEWS_HOURLY`. Unchanged headlines skip the model via the digest |
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

**`trading_macro_verdicts` still gates nothing.** It accumulates so the
question can be settled: did BEARISH verdicts precede down sessions, or refuse
days the engine would have won?

**`news_verdicts` DOES gate, as of 2026-09-13 — see sections 143–144.** Two
gates, on both books:

- **Level**, asymmetric. Puts refused into `BULLISH+`; calls into
  `VERY_BEARISH` only. BEARISH is 53% of this feed, so a BEARISH-level call
  gate would refuse half of all sessions against a 4.5bp separation.
- **Turn**, symmetric. One step from the day's *opening* verdict refuses the
  contradicted side. This is the intraday regime change — a read BEARISH since
  09:30 has said nothing new by 14:00; one that was NEUTRAL and just went
  BEARISH is the event.

The turn gate has never fired on a historical session and **cannot be
backtested** — every past day holds one verdict, so there is no delta in the
record. Armed on its shape, not on a result.
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

## One symbol list, after six drifted apart

Audited 2026-09-13. Six lists existed and no two matched: names tracked for
news that nothing could trade, names traded that had no 09:30 verdict, and
Friday-only chains sitting in an intraday list burning API calls for a signal
nothing could act on.

**The tradeable set is now eight names**, all confirmed to carry Mon/Wed/Fri
expiries, with the quote width that decides whether they clear the liquidity
gate:

```
NVDA  2.6%   TSLA  2.7%   META  6.3%   AAPL  7.3%     tradeable
AMZN 12.2%   GOOGL 14.4%                              marginal, passes
MSFT 17.5%   AVGO 21.2%                               REFUSED by the gate
```

`dte0_trade`, `dte0_shadow`, `news_hourly` and `TRADING_MANAGE_UNDERLYING` all
carry the same eight; QQQ is added to the news and paper lists only, since the
engine owns it and Polygon returns ETF comparisons for it.

**TSLA and AAPL needed aliases.** Without them `patterns_for()` falls back to
the bare ticker, so "Tesla" and "Apple" -- which is what a wire actually prints
-- would never have matched.

**Dropped:** SNDK, MU, CRWV, MRVL, PANW, DELL, WDC, STX, ADBE. MU is the one
worth noting: at 2.8% it had the second-tightest quote on the board. SNDK and
CRWV list Friday expiries only and could never have traded intraday anyway.

---

## Hourly per-symbol sentiment, from Polygon and RSS

`symbol_sentiment_hourly`, written by `scripts/news_hourly.py` every hour
08:00-16:30 ET. **It scores. It does not gate.**

**Claude is out of the trading path (2026-09-13, sections 146–150).** Two
legs, asymmetric because the problem is:

| leg | source | model |
|---|---|---|
| ticker | Polygon `insights.sentiment` **+ RSS headlines naming the symbol** | one hosted API |
| analysis | `scripts/exit_backtest.py` + `expiry_closes.py` | none — stdlib, runs on the host |
| macro | RSS → promo regex → **one Gemini call** | one hosted API |

**The ticker leg gained an RSS half on 2026-09-16 (section 163).** Reuters
broke the SK Hynix / Intel story, INTC opened +5.2%, CNBC's feed had it in
`news_seen` by 09:12 ET — and Polygon carried **zero** mentions across 618
articles in 72 hours. "Macro from RSS, tickers from Polygon" assumed Polygon
covers ticker news; a wire service getting there first is the ordinary case.

RSS headlines matching a symbol's `patterns_for()` aliases (word-boundary, so
"artificial intelligence" is not an Intel story) are scored per company in
**one Gemini call per sweep** and then **pooled with Polygon's articles** —
not averaged with Polygon's score. Pooling is the point: the recency
half-life, the pre-open ageing and the `n/(n+k)` shrinkage all act on the
union, so one specific story sits inside one sample rather than forming a
second opinion.

Pooled rows are written as source **`polygon+rss`**, never `polygon` — a
pooled read is not a Polygon read, and mislabelling it would make every later
"how accurate is Polygon here" answer itself with a different corpus. The
reader accepts either and takes the newest, breaking an equal-`asof` tie
toward the pooled row explicitly.

`TRADING_NEWS_RSS_TICKER=false` reverts it with a restart. A Gemini outage
already falls back to Polygon alone: an outage must produce no opinion, never
a wrong one.

Nothing is installed locally — no torch, no spaCy, no transformers, and as of
2026-09-14 no HuggingFace call either. `gemini-3.1-flash-lite` decides
macro/skip, topic and direction in a single batched call.

**NER, FinBERT and the rules engine were all removed (section 155).** Measured
on ten controlled macro headlines: FinBERT 2/10 with 8 inverted, distilroberta
4/10 with 5 inverted, hand-written rules 10/10 but *fitted* to those ten,
Gemini **9/10 unfitted**. The sentence classifiers were not noisy — they were
domain-mismatched, reading word polarity instead of economic implication
("unemployment falls" is good, "oil spikes" is bad), which is why a better
classifier was not the answer. The file went 1240 → 690 lines.

What is given up: the rules were free, deterministic and auditable. This is a
paid API, and when it is unreachable there is **no** macro verdict rather than a
degraded one — the failure latches, unclassified articles vote on nothing, and
the gates fall back to the last stored row.

**The RSS scrape is retired (2026-09-12).** `news_hourly.py` is the only
fetcher; the per-minute cycle now READS the stored corpus through
`_stored_headlines()` and makes no network call at all. It had to stop
fetching rather than merely change source: Polygon's free tier allows five
calls a minute across twelve tickers, so a single cycle would exhaust it. The
same constraint removes network I/O from the hot path, which section 55
records the cost of -- three cycles lost at the open with seven positions live.
**Two sources, because they do different jobs.** Polygon serves per-ticker
news and **cannot serve the macro tape**. Measured 2026-09-12, both ways:

```
ticker=QQQ, 12 days   8 articles, every one an ETF comparison --
                      "Should Schwab U.S. Large-Cap Growth ETF (SCHG)
                       Be on Your Investing Radar?"
market-wide, 50 rows  0 matched any of the 114 MACRO_TERMS
```

Which is the finding `symbol_news.py` already recorded: QQQ is not a company,
a ticker feed returns fund-comparison articles for it, and what moves it is
rates, yields, oil and geopolitics. So `MACRO_FEEDS` (MarketWatch top-stories
and CNBC) supplies the macro tape and nothing else -- no per-symbol RSS, no
alias matching against a scrape. Without it `TRADING_NEWS_DIRECTION` is a
switch that is on and does nothing.

**`macro_headlines()` refuses a stale feed and says so.** `mw_marketpulse`
answered 200, parsed cleanly and served July-2025 headlines for months;
freshness is the only check that would have caught it, so a feed whose newest
item is over 48 hours old is skipped by name.

**The per-symbol RSS code is deleted, not flagged off** -- `RSS_FEEDS`,
`PER_SYMBOL_FEEDS`, `_feed_name()`, `_entry_published()`,
`_scrape_headlines()` and the `feedparser` import, 124 lines in all. Two
sources meant two failure modes and one of them was silent.

`SECTOR_TERMS` and `SYMBOL_SECTORS` went with them. They were live code that
did nothing: **0 additional matches across ten symbols on 236 headlines**,
while correlating four memory names into a single verdict. `patterns_for()` is
now the alias list, or `MACRO_TERMS` for QQQ, and nothing else.

**Polygon replaces the RSS scrape because articles arrive TICKER-TAGGED**,
which deletes the alias-matching layer and the three bug classes it produced
in a single evening: `ALIASES["SNDK"] = ["sandisk","sndk"]` could not see a
sector story; `SECTOR_TERMS` matched 0 of 236 headlines; `mw_marketpulse`
answered 200 for months while serving headlines a year old. **A dead RSS feed
and a quiet news day are indistinguishable from inside a scrape. A Polygon 429
is not**, and it is logged by name.

**The rate limit is measured, not assumed.** The free tier refused the 6th call
inside two seconds and returned 429 on eight of eight when hammered. Pacing is
a delay BETWEEN EVERY CALL (13s), not a sleep after each fourth -- that pattern
still bursts four calls into one second.

**Polygon ships its own sentiment with reasoning**, free with the news call:

```json
{"ticker":"NKE","sentiment":"negative",
 "sentiment_reasoning":"Stock at 12-year lows, declining revenue..."}
```

**FinBERT was tried alongside it and removed the same day.** It is a sentence
classifier, not an aspect-based one, so it received a bare headline with no way
to know which ticker it was rating. The case that settled it:

```
"Nike Is Being Deleted From the S&P 100. Is Its Seat in the Dow
 Jones Industrial Average in Jeopardy?"

polygon  positive  "Being added to S&P 100, ranked top 50 by market cap"
finbert  -0.81     read "Deleted... in Jeopardy?"
```

**Nike is deleted; SanDisk is ADDED.** Polygon was right and FinBERT was
answering a different question -- which also explains its 50-52% on the graded
outcomes: it was scoring the wrong subject a good part of the time. No prompt
or threshold fixes that; there is no way to tell a sentence classifier "score
this headline FOR SanDisk".

**BEING THE RIGHT SHAPE IS NOT THE SAME AS BEING RIGHT.** Polygon's read has
never been scored against an outcome here — and as of 2026-09-13 it IS what
grades every ticker, Claude having been removed from the trading path entirely
(section 146). So the thing that now feeds the gates is the thing that has
never been measured. `verdict_outcome` grades it nightly; that is what will
settle it.

**Retention is 10 days, matching `NOVELTY_LOOKBACK_DAYS` exactly.** That is the
binding constraint: the novelty filter asks for prior coverage over 10 days, and
cutting below its window turns every re-reported story into a fresh catalyst --
the failure that gave the QQQ read its standing bearish tilt.
`news_verdict_outcomes` is unaffected, storing the verdict and the session
return rather than the headlines.

---

## The news pipeline

**A NEUTRAL ANCHOR BEATS AN ENUMERATED DIRECTION.** Vectors 1-6 of
`MACRO_TERMS` are directional phrases -- `yields fall`, `core inflation`,
`non-farm payroll`, `crude oil` -- so the set could only retrieve the
directions somebody had listed. Measured over 2,650 headlines on 2026-09-12:

```
subject         headlines   missed before   after vector 7
inflation            101         44              0
jobs/growth            9          3              0
fed/rates            132         36             20
yields/bonds         111         50             34
oil/energy           134         52             38
geopolitics           99         30             27
```

It had `yields fall` and not `yields`, `core inflation` and not `inflation`,
`non-farm payroll` and not `payroll`. So *"Yields Retreat after Waller Signals
a Hold"*, *"Sticky Inflation Report"* and *"Private payrolls rose by 38,000"*
all missed.

**And an anchor needs no counterpart.** Vector 5 exists because a set of
only-bad terms produced a standing bearish tilt, and the answer then was to
enumerate the good terms too. `inflation` retrieves *"inflation cools"* and
*"sticky inflation"* alike and hands the model both -- which is what the model
is for. **Enumerating directions is what created the bias in the first place.**
Terms are kept unambiguous in a finance feed: `pipeline` is excluded because it
matched a Novartis drug pipeline, `attack` because it matched a cyber-security
story.

**CHECK A FEED'S DATES, NOT ITS STATUS CODE.** Measured 2026-09-12:

```
mw_marketpulse   200, 30 entries, newest Jul 2025    ABANDONED, and configured
mw_realtime      200, 10 entries, newest Jun 2025    ABANDONED
mw_topstories    200, 10 entries, newest today       live  <- now used
fool index       200, 50 entries, newest today       live  <- now added
```

`mw_marketpulse` answered 200 and parsed cleanly every cycle for the life of
this pipeline while serving headlines over a year old -- *"Consumer credit
growth soars in December"*, scraped in September. Nothing was logged because
nothing was wrong: reachable, parsed, and the same ten stale titles stored once
and filtered as known ever after. **A dead feed and a quiet news day are
indistinguishable from inside the scrape.** The Motley Fool was never
configured, which is why an article on SanDisk's crash had no chance of being
graded.

After the swap: 216 headlines a scrape across YAHOO_FINANCE 99, SEEKING_ALPHA
88, MARKETWATCH 10, CNBC 10, MOTLEY_FOOL 9.

**And the store was duplicating.** `store_headlines` reads what is known,
filters the batch, then inserts -- two overlapping runs both see an empty
`known` and both write. 52 of 236 rows on 2026-09-11 were exact duplicates.
That is not cosmetic: the novelty filter drops a headline within 0.83 cosine of
prior coverage, and **a duplicate is a perfect match for itself**. SanDisk had
three distinct headlines before 09:30 that day and the model received two. A
unique index on `headline_text` plus `ON CONFLICT DO NOTHING` is the fix that
holds under a race; the corpus was deduplicated 3,762 -> 3,464.

**Closed 2026-09-13: the per-symbol read now runs hourly.** Five of
SanDisk's eleven headlines on 2026-09-11 were published intraday, including
*"NAND Party Likely To End In 2027"* at 10:08, and the once-a-day read never
graded them. `news_watch.py` now re-grades 09:20–16:00; the headline digest
means an unchanged hour costs one query and no model call, so only a genuinely
new headline pays.

Three things had to be fixed for the schedule change to mean anything — the
verdict overwriting itself under six measurement scripts, a dead `ingest()`
left behind by the RSS removal, and a day-long cache in the engine that would
have held the morning verdict until midnight. Section 143.

---



```
ingest   nodes._scrape_headlines()
         3 general feeds  +  per-ticker Yahoo & Seeking Alpha for every
         name in TRADING_MANAGE_UNDERLYING
store    market_news_vectors — Voyage embeddings, deduped, with `source`
tag      symbol_news.ALIASES — the engine holds SNDK, the wires write SanDisk
grade    classify_day() → VERY_BULLISH..VERY_BEARISH. NO MODEL CALL as of
         2026-09-13: tickers read Polygon's stored aspect score, QQQ reads the
         RSS/rules/Gemini macro row. SINCE THE PREVIOUS SESSION'S CLOSE.
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
between them (−0.11% → +0.33%). That wobble was the classifier, not the data:
Haiku returned different verdicts on the same headlines run to run, by as much
as any effect being tested (section 127). Haiku is gone as of 2026-09-13, but
the warning survives it — Gemini is sampled too, and the rules layer is the
only part of the macro read that is deterministic. The filter is kept on the principle,
not on a measurement. The cost is that a genuine follow-up to a covered story
can be dropped with it.

**Any future A/B on this pipeline must grade each configuration several times
and report the spread.** A difference smaller than the re-run wobble is not a
difference.

**`news_hourly.py` is the only fetcher; `news_watch.py` only grades.** The
original reason still holds — nothing else fills the store outside market
hours, so a window reaching back to the previous close finds an empty table and
reads as "there was simply no news", every morning, forever (section 124). What
changed is who fills it: the RSS scrape was deleted on 2026-09-12 and
`news_hourly.py` (Polygon per ticker, plus `MACRO_FEEDS` for the macro tape)
took over, running at :07 — eighteen minutes ahead of the grader.

`news_watch.ingest()` is now a **safety net, not the fetcher**: it measures
corpus age and sweeps Polygon only when the newest headline is older than
`TRADING_NEWS_MAX_CORPUS_AGE_MIN` (90), i.e. only when the hourly job has
actually stopped. It previously called `nodes._scrape_headlines()` — deleted
with the RSS machinery — inside a bare `except`, so it printed a one-line
failure and graded whatever happened to be stored (section 143).

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

## Trading the morning on the macro read

Built 2026-09-12, **off behind two separate switches**, and every measurement
this repository holds on the idea is negative:

```
morning put debit, measured    27% wins   -50.62 a trade
MORNING_CREDIT, 60 sessions    38% wins   -57.64 a trade   halves -37.95/-77.32
QQQ macro news read            5/10 on next-session direction
QQQ mornings down >0.25%       -0.41% by 10:30, -0.16% by 11:30, n=8
```

`MORNING_PUT` is the structure: ITM put debit, 09:45-11:30, width 4,
**CLEAN,ZONE tiers as of 2026-09-15** (code default is still CLEAN), **closing
at 11:30 rather than the 13:25 the other morning windows use**. That last part is the only thing the evidence supports -- the
continuation measured about 45 minutes long and gone by lunch, and holding is
what turned both previous attempts at a bearish morning into losses.

`TRADING_NEWS_DIRECTION` is the gate: the QQQ macro verdict **vetoes the side
it contradicts**, above `TRADING_MACRO_DIRECTION_MIN_CONF` (0.25). NEUTRAL
never gates.

**That verdict is no longer a news read.** Since 2026-09-14 it comes from
crude/10Y/VIX (`source='objective'`), not from headlines — the text read
printed seven identical values through a session that turned, and the price
read called the turn while it happened (section 156). The floor moved from 0.70
to 0.25 with it: 0.70 was a Claude confidence score, and `|score|` on three
clamped price channels is not the same quantity.

**IT NEVER INVENTS AN ENTRY.** A tier still has to fire on the technicals, and
if none does there is no trade whatever the news says. The failure mode of
every previous attempt at a news- or macro-driven morning was taking a
position the tape did not support, so this gate can only ever remove one. It
sits after the tier ladder and before the event blackout.

Two switches because they fail differently: the window is a strategy nobody
has measured positive, and the gate is a news read scoring 5/10 whose
corrections (balanced terms, novelty filter) landed 2026-09-07 and have no
post-fix evidence at all yet. `news_verdict_outcomes` accumulates that
nightly.

```
TRADING_ENABLED_WINDOWS=MORNING_DRIFT,MORNING_PUT,ITM_GRINDER,AFTERNOON_CREDIT
TRADING_NEWS_DIRECTION=true
TRADING_MORNING_PUT_TIERS=CLEAN,ZONE
```

**Bearish coverage across the session** (section 160). `ITM_GRINDER` was
enabled 2026-09-15 because 11:30-13:30 had no book that could go short —
`MORNING_PUT` and `MORNING_CREDIT` both shut at 11:30 and `MORNING_DRIFT` is
long-only, so five valid CLEAN/bear setups at 12:00 ET were refused with
*"MORNING_DRIFT is long-only"*:

| window | hours | tiers | side |
|---|---|---|---|
| `MORNING_PUT` | 09:45–11:30 | CLEAN,ZONE | bearish |
| `MORNING_DRIFT` | 10:15–12:30 | CLEAN | long |
| `ITM_GRINDER` | 11:30–13:30 | ALL | both |
| `AFTERNOON_CREDIT` | 14:00–15:00 | ALL | both |

Both enablements are **experiments, not corrections**: ZONE measured −21/−13/−25
a trade on its own, and `ITM_GRINDER` has two live trades of record. The case
for trying them is that ZONE was measured *with the macro gate off*, and that
gate now reads prices.

---

## The one script that trades by itself

**Rotation (`--rotate`) re-enters a name after it exits.** The engine has had
this for QQQ all along and its numbers were tuned on outcomes --
`TRADING_WIN_COOLDOWN_MINUTES=30`, `TRADING_REENTRY_COOLDOWN_MINUTES=90`; this
borrows the shape rather than inventing one.

```
TRADING_DTE0_ROTATE          off by default, and SEPARATE from --live
TRADING_DTE0_ROTATE_COOLDOWN_MIN   30   after any exit, however it left
TRADING_DTE0_MAX_ROTATIONS          3   per symbol per day
TRADING_DTE0_ROTATE_CUTOFF      13:30   no NEW entry after this
```

Exits are read from `trading_history`, where orphan exits also land -- so a
stall, a target and a flatten all count the same, which is what makes
"re-enter after it exits" mean one thing.

**What it costs, because it compounds:** NVDA's quote is 2.6% of mid, so a
round trip is ~5.2% of premium -- about **$19 a rotation on a $374 position
against a $112 target**, roughly 17% of each target spent getting in and out.
And the later entries are structurally worse: extrinsic has decayed,
entry/width drifts to the bottom of the band, and a 14:30 entry has 75 minutes
before the flatten. Hence the cutoff and the cap.

**It fails CLOSED.** If the exit history cannot be read, every symbol is
reported at the cap and nothing rotates -- verified when a missing import
triggered exactly that path.

**This is the first order-placing path here without a track record.**
Everything else that trades by itself is QQQ-only and measured. `dte0_shadow`
accumulates the paper version alongside.

---



`scripts/dte0_trade.py`. Everything else in `scripts/` observes; this one
places orders, so the guards come first:

```
--live required            dry run is the default and prints the same plan
TRADING_DTE0_LIVE=true     must ALSO be set; --live alone does nothing
TRADING_DTE0_MAX_BUDGET    hard ceiling, 1500
--max-trades 3             one per underlying
already-held check         refuses a symbol the account already holds for
                           today's expiry, and FAILS CLOSED if it cannot read
                           positions at all
MAX_ORDER_CONTRACTS        sizing respects the clamp rather than discovering it
```

**QQQ is excluded by default.** The engine trades QQQ 0DTE itself from 09:45;
a second position here would be an independent bet on the same underlying with
the engine logging RECONCILE every minute.

**Debits only.** Credit structures lock capital against the full width, this
book has no measured record selling premium, and IV/RV across these names came
back 0.48-0.83 on 2026-09-12 -- implied below realised, premium cheap.

Ranked by EV from `screener.rank()`, the same call `/screener/verticals` and
`weekly_pick.py` make, then filtered by the three constraints. **EV picks the
best of what is sound; the constraints decide what is sound** -- the screener
knows nothing about them and happily returned NVDA 200/220 at +28 points of
edge with 35% of its premium in time value.

Three rules earned during the first dry run:

**A negative edge is not a trade.** Ranking on EV alone selected META at Pwin
50.1% against a 54.3% break-even. The best of a bad set is still bad.

**Affordability is a selection criterion, not a post-check.** MU's top row was
a 50-wide at $1,936, so the name was dropped entirely instead of falling back
to a structure that fits.

**Size against the slot, not against what qualified.** Dividing the budget by
the number of survivors put the whole $1,500 into one name on the day the
filters had just rejected everything else -- the day to be smaller, not
larger. Unspent budget stays unspent.

**Rejections are tallied by reason.** Five filters in series with a silent
"nothing cleared" leaves no way to tell a quiet market from a knob set wrong.

**The two tools enumerate differently and can disagree.** `dte0_pick` builds
its own ATR-derived widths from the chain; `dte0_trade` filters whatever
`screener.rank()` generated. On 2026-09-12's crossed weekend quotes `pick`
found MU and META structures and `trade` found none. If they disagree on a
live session, `pick` is the one seeing the full width ladder.

Exits are `orphans.py`'s job and are not duplicated here.

---

## Picking a 0DTE structure: the three constraints

`scripts/dte0_pick.py`. **Prints, never trades.** Each constraint was learned
by losing money to its opposite in the week of 2026-09-08:

**The exit ladder, as deployed 2026-09-16** (sections 157–173). Two books,
separate settings — they were briefly one rule by accident, see section 171.

| rule | 0DTE | weekly | note |
|---|---|---|---|
| target | **+70%** on the mark | **+70%** on the mark, and **95% of width on INTRINSIC** | a level, so a sub-minute spike books nothing |
| stall quiet | **2 min** | **30 min** at 5+ sessions → 10 min at 1 (§199) | 2 measured better or equal on *every* 0DTE session (§172); the weekly clock is a judgement (§192) |
| stall giveback | **15% of band** | **0.25 ATR** at 5+ sessions → 0.10 ATR at 1, in spread points | band = width − entry, fixed at entry. On SNDK 1700/1780 @38.65 the ATR basis is 24.8 spread points against 8.3 on the old 20% band (§192) |
| stall arms at | any positive peak | **+25%** at 5+ sessions → +5% at 1 (§199) | a multi-day position may pause without being finished; below +25% a weekly's pauses are noise (§192) |

| stop — **SOFT** | **−10%**, held **30 min** | none — `LATER_STOP` covers weeklies | a slow bleed. Ignores the intrinsic guard. Below 30 min it fires on noise: 10 min ≈ −$1,400, 5 min ≈ −$9,500 (§181) |
| stop — **HARD** | **−30%**, held **5 min** | **−45%**, 15 min with 5+ sessions left, **scaling to −30%, 5 min at 1 session** (§199) | a fast drop. On a 5-day spread −25% of mark was a fifth of one ATR day (§192). Respects the intrinsic guard. **A cliff below −30%**: −25% ≈ −$5,500, −15% ≈ −$11,500 (§184) |
| flatten | 15:45 | none — runs to expiry | |
| opening quiet | **09:35** | **09:45** | `ORPHAN_HOLD_UNTIL` / `ORPHAN_LATER_HOLD_UNTIL`. Holds **10 of 12** branches; only `ACCOUNT_FLOOR` and `FORCE_CLOSE` can act before it (§182) |

**All four profit-taking branches are drag-gated** — `TARGET`, `LATER_TARGET`,
`STALL`, `STALL_LATER`. `TARGET` was the one that was not, until 2026-09-17,
when it sold a SNDK weekly at a 26.70 mark against 40.00 of intrinsic four
minutes after the drag ceiling had refused the identical close twice (§174).
A change to one branch of this chain means enumerating **all twelve** and
stating which it touches and which it deliberately does not.

**`ORPHAN_HOLD_UNTIL` now holds ten of the twelve.** Only `ACCOUNT_FLOOR`
(account-level, outranks any single position) and `FORCE_CLOSE` (15:45 only)
can act before it.

Checking that requires reading each guard **where it is computed**, not the
`elif` text: `SLOW_STOP`, `LATER_STOP` and `OTM_STOP` carry `past_hold` in
their *clock*, and the 0DTE stop has its own branch that logs and holds —
*"declines to trust a −25% mark printed into the opening spread at all."* Two
separate audit scripts have now reported these as ungated by reading condition
text alone (§174, §182).

**THE DUAL-TIER STOP — four settings, two rules.** Each stop needs a *level*
and a *wait*, and the wait is what makes a level usable:

| | level | wait | env |
|---|---|---|---|
| **SOFT** | −10% of premium | 30 min | `SLOW_STOP_PCT` / `SLOW_STOP_MINUTES` |
| **HARD** | −10% of premium since 2026-09-21 (was −30%) | 2 min (was 5) | `STOP_PCT` / `STOP_CONFIRM_MINUTES` |
| **TAPE** (§211) | any loss, underlying on the wrong side of a session VWAP moving against it | 15 min | `TAPE_EXIT` / `TAPE_EXIT_MINUTES` / `TAPE_EXIT_SLOPE_BARS` / `TAPE_EXIT_LOSERS_ONLY` |

The waits differ on purpose: a slow bleed might recover and needs confirming;
a 30% drop has already told you something and waiting costs money. **All
clocks are continuous** — a tick back above the level resets them to zero.
The HARD level was re-swept on 130 structure-days on 2026-09-21 (§207): −10%/2 min
beat the deployed −30%/5 min by 26,879 over nine sessions and cut the worst day
by 10,700, reversing §82's 39-day result. The TAPE exit (§211) sells a *losing*
0DTE debit once the underlying has spent 15 minutes under a falling session VWAP
(call) or over a rising one (put): +9,891 on top of the stop alone, worst day
4,000 better; it sits below the stops in the `elif` chain and names the exit
`TAPE_EXIT`. §208 measured the opposite use, VWAP as a reason to *wait* before
the stop sells, at −9,260: the tape earns its place selling sooner, not later.

**Why a level alone cannot work.** Same level, the only difference being
persistence: `−10% with no wait = −$12,559` against `−10% held an hour =
+$996`. On a 0DTE spread the bid-ask alone is often more than 10% of premium,
so an unconfirmed level fires on the quote rather than the position. 5% / 10%
of premium was measured at **−$12,481** for exactly this reason — 5% of a
$0.55 spread is under three cents.

**Best measured combination**, 221 positions over 10 sessions: soft −10%/30min
+ hard −30%/5min at **+$5,938**, against −$4,187 for the 5-minute pair it
replaced. The hard stop fires 9 times rather than 40, and the soft stop gets
to act instead of being pre-empted.

**There is no resting stop order at the broker.** Every exit is a `multileg`
**limit** order submitted when a rule fires, on a **60-second poll**. Nothing
watches between cycles, while the engine is down, or overnight; a gap is
caught at the next poll at whatever price exists then, and the limit can fail
to fill. This is not a broker stop and must not be relied on as one (§183).

**Three guards sit across both books:**

| guard | setting | what it refuses |
|---|---|---|
| `STOP_RESPECTS_INTRINSIC` | on | stopping a spread whose intrinsic exceeds entry — it pays at expiry |

**The slow stop deliberately overrides `STOP_RESPECTS_INTRINSIC`** — with the
guard applied it never fires at all. So the book holds both positions at once:
the guard protects a spread whose mark is depressed by drag, and the slow stop
closes one depressed for ten minutes. **The duration test is the only thing
separating them.** Watch for it firing on a position whose intrinsic is above
entry; if that starts costing money, the duration is the lever, not the level.
| `STALL_MIN_GAIN_PCT` | **8%** on the mark | booking a gain not worth taking; removing it costs $5,380 |
| `ORPHAN_MAX_DRAG_WIDTH` | **15% of width** | closing while forfeiting intrinsic; removing it costs $6,337 |

Measured and deliberately **off**: `ORPHAN_INTRINSIC_GIVEBACK_PCT` (monotonic —
the less it fires the better) and `ORPHAN_OTM_STOP` (a coin flip, and it
preempted the whole ladder, leaving zero stalls and zero stops).

**Intraday realizability is capped, and width is not the lever** (§178–179).
Across 212 positions the best mark gain a position ever offers averages **~11%
in every width band**, against peak *intrinsic* of 20–48%:

| width | n | best MARK | peak INTRINSIC | max drag |
|---|---|---|---|---|
| ≤3 | 24 | 11% | 20% | 12% of width |
| 3–6 | 56 | 12% | 28% | 14% |
| 6–15 | 42 | −1% | 20% | 22% |
| 15–30 | 28 | 8% | 48% | **32%** |
| >30 | 62 | 11% | 33% | 18% |

A **maximum absolute width at entry** was proposed and **measured away**: drag
as a *share* of width peaks in the 15–30 band and is lower above 30, so a cap
would have admitted the worst band and blocked a better one. "Drag scales with
width" is true in dollars and false as a fraction, and the fraction is what the
ceiling measures.

The live case: a SNDK 1570/1625 peaked at **+81.8% on intrinsic while its mark
never passed +9.1%** — intrinsic gained 19.5 points, the mark 4.8, because spot
was climbing *toward* the short strike where its extrinsic is maximal. Its
realizable ceiling was **+$263**. Both rules declined correctly and logged why.

Open candidate, **not deployed**: the weekly book has no mark-based target at
all — `LATER_TARGET` aims at 95% of width on *intrinsic*, a price that exists
only at expiry. "Best mark" above is a hindsight maximum, a live target at 11%
would cap every winner that runs further, and the harness cannot settle a
weekly.

`scripts/exit_backtest.py [date] [symbol] | --all` replays every position
against the engine's own logged marks, rotated logs included. **It now settles
held 0DTE runs at their real value** — `scripts/expiry_closes.py` caches each
session's close and 15:45 price, and a held run is scored at intrinsic at the
*flatten*, since the engine never holds 0DTE to 16:00. Before that fix a rule
that held more was punished for holding using data that did not exist, which
reversed two conclusions in one evening (§169).

**What it still cannot do**, and both matter when reading its output: it cannot
settle a **weekly** (that needs the expiry date's close, which for an open
position does not exist), so those stay truncated and held counts still vary
across configurations — treat any difference under about $1,000 as noise. And
it replays **exits only**: a trade never taken leaves no marks, so entry-side
changes are invisible to it.

Use it before moving any of these. Every setting above that carries a dollar
figure was argued against 206 positions over 9 sessions; every one that does
not is an operator judgement, and the weekly column is entirely the latter.

It replays **exits only**. `ITM_GRINDER`, the ZONE tier and the engine-side stop
confirmation change which trades are *taken* or sit on a path no position
touched that day, so roughly half of that day's changes have no backtest behind
them.

```
entry 30-65% of width     above 65% the +30% target is arithmetically
                          unreachable, since max return is (width-entry)/entry
                          and 0.77 of width IS exactly 30%. Four QQQ positions
                          on 09-11 were bought at 0.68-0.81 and their target
                          rung did nothing; the 15:45 flatten became the exit.
                          Below 30% the premium is mostly time value and the
                          stop is nearer than one morning of theta. (That read
                          -10% until 2026-09-15; the 0DTE stop is now -35% --
                          section 157.)
extrinsic under 25%       the same failure in the units that cause it. NVDA
                          220/200 screened at +28 points of edge with 35% time
                          value: the stop sat 0.26 below entry against 0.91 of
                          extrinsic, so theta alone covered it before lunch.
                          The same arithmetic is why -10% was widened: on a
                          2.46 entry it was a QUARTER POINT of QQQ, 4% of the
                          day's range.
target within 0.3 ATR     the move to +30% has to be an ordinary session. ATR
                          converts at ATR/1.596, the constant the probability
                          engines already use.
short leg within 0.4 ATR  the strike SOLD has to be somewhere price can reach.
                          NVDA's 225 call fetched 0.11 against a 3.85 long --
                          2.9% of the cost -- while capping every gain above
                          225. At 222.5 the same leg earns 0.30, at 220 it
                          earns 0.84. The number comes from what these names
                          actually travel in a session: NVDA 3-4 of ATR 7.67
                          = 0.46, META 5-10 of 21.32 = 0.35, MU 10-15 of
                          44.15 = 0.28. Widths are DERIVED from this span and
                          the chain's own strike spacing, not listed -- a $5
                          spread on NVDA and on MU were never the same trade.
quote under 15% of mid    a vertical crosses the quote twice, on two legs.
                          NVDA 2.6%, MU 2.8%, META 6.3%, AMZN 12.2%, GOOGL
                          14.4%, MSFT 17.5%, AVGO 21.2% -- the last two are
                          refused, which is what let the universe widen to
                          every Mon/Wed name.
```

It scores calls and puts identically and prints the best of each. **It does not
pick direction** -- that is the morning's question (news verdict, flow, tape),
and pretending a strike rule answers it is the same category error as a +55%
target that could never fire.

Worked on NVDA for 2026-09-14: CALL 215/222 at 47% of width with 7% extrinsic,
target 0.17 ATR away; PUT 220/215 at 42% with 18%, target 0.13 ATR. Both
inside a quarter of a daily range.

---

## The Mon/Wed 0DTE shadow

`dte0_shadow`, written by `scripts/dte0_shadow.py`. **It never trades** -- no
order path, no live slice. It exists because the engine's 0DTE record is
QQQ-only and thin (MORNING_DRIFT 6 live trades, AFTERNOON_CREDIT 7 and
negative) and there is no single-name 0DTE evidence here at all.
`weekly_shadow` records five-day holds; a Monday NVDA spread is a different
instrument with different gamma.

**Which names have mid-week expiries, checked against the chain:**

```
QQQ                                daily
AMZN AVGO GOOGL META MSFT NVDA MU  Mon, Wed, Fri
SNDK CRWV                          Friday only
```

The two names with the worst weekly tails are also the two that cannot do
mid-week 0DTE.

**Structure comes from IV/RV**, the one column section 50 measured on this
book: a credit spread's break-even win rate IS its risk ratio and delta IS the
market's probability estimate, so an edge can only come from implied exceeding
realised. At or above 1.05 it sells premium, at or below 0.95 it buys, and
between it records nothing -- an abstention is data.

**Both sides every session.** One CALL row and one PUT row, so the data can
answer which direction worked rather than only the one a signal picked.

**The quote is a gate, not a footnote.** Median near-ATM width as a share of
mid: NVDA 2.6%, MU 2.8%, QQQ 3.9%, META 6.3%, AMZN 11.9%, GOOGL 13.3%, MSFT
17.0%, AVGO 21.2%. A vertical pays that twice on two legs, so against a 30-50%
maximum return AVGO's quote eats the trade before direction matters.
`TRADING_DTE0_MAX_QUOTE_PCT` (15) refuses those chains and the width it did pay
is stored on every row.

First dry run against Monday 2026-09-14's chain: 10 rows across QQQ, NVDA, MU,
META and AMZN; GOOGL abstained at IV/RV 0.97; MSFT (17.5%) and AVGO (21.2%)
refused on quote width. **Every ratio came back between 0.48 and 0.83** --
realised far above implied, options cheap, so the rule says buy premium rather
than sell it.

Cron: `--open` at 09:46 ET, `--settle` after the close, both DST-covered.
Settlement is arithmetic from spot, never a quote -- the closing quote on an
expiring option is the widest of the day.

---

## Does the 09:30 verdict predict the day

**A ticker match cannot see the story that moves the sector.** 2026-09-11:
SanDisk fell on Chinese memory-efficiency research that pressured the whole
NAND complex, and the 09:30 read returned **two headlines** and graded it
NEUTRAL -- reasoning that SK Hynix and Samsung had declined while SanDisk
"held up". It never saw the story: a wire writing *"memory-efficient model
pressures NAND makers"* prints no ticker, and `ALIASES["SNDK"]` was
`["sandisk", "sndk"]`.

That is the discovery that produced `MACRO_TERMS`, one level down. `SECTOR_TERMS`
gives the single names their sector vectors the way QQQ got its macro ones:

```
memory     SNDK MU WDC STX        glut / oversupply / efficiency / prices fall
                                  <-> shortage / undersupply / HBM demand / capacity cuts
ai_model   META GOOGL MSFT NVDA   model efficiency / cheaper training / capex cut
           AMZN AVGO MU           <-> frontier model / agent launch / compute demand
```

**Balanced 8 and 8, for the reason vector 5 exists** -- a set assembled only
from gluts and cuts hands the classifier nothing but trouble, which is exactly
what produced the 10-of-14 bearish tilt on QQQ. SNDK goes from 2 match terms
to 18; CRWV keeps its 2, because its story really is its own.

**It makes these names correlated on purpose.** One memory headline now grades
for SNDK, MU, WDC and STX together, so four verdicts can move as one. They do
move as one -- but it means the row count in `news_verdict_outcomes` overstates
the evidence, which is why that report prints a per-session column beside it.

**QQQ is graded again from 2026-09-12, and that is a test, not a decision.**
The index read was pulled after coming back BEARISH on 10 of 14 sessions at
**5/10 on direction**, with the tilt surviving the tape reversing -- five
straight bearish reads while QQQ printed +0.22, +0.04, +0.30, -0.05. The
diagnosis was RETRIEVAL, not the model: a term set built from tightening,
conflict, debt stress and hard data can only hand the classifier trouble.

Both corrections landed 2026-09-07 -- vector 5 (easing, disinflation, soft
landing) gave the term set a side it could not previously express, and the
novelty filter stopped the wires re-reporting standing macro stories into
every window. **And then nobody checked**: the symbol stayed out of the graded
list, so the fix shipped untested. `QQQ` resolves to `MACRO_TERMS` through
`patterns_for()`, never to the ticker, so what is graded is the macro tape --
99 terms across six vectors: Fed and rates, geopolitics and oil, the global
bond complex, hard data, easing and disinflation, and shipping and trade
policy. **Every disruption term is paired with the term that describes it
ending.** A set that can only surface trouble hands the classifier nothing
else to report, which is what produced the 10-of-14 bearish tilt in the
first place; adding Houthi and tariffs without their relief counterparts
would have rebuilt it.

Grading it feeds two things that were already built for it: `verdict_outcome`
scores it nightly, and `macro_outcome.py` reads `news_verdicts WHERE
symbol='QQQ'` -- the column it has always wanted exists only now.

**Nothing gates on it.** Section 22 and section 14 are the precedent: a term
nobody has scored is logged beside the decision, never wired into it. 5/10 on
direction is the number the corrected read has to beat first.



`news_verdict_outcomes`, one row per graded verdict, written by
`scripts/verdict_outcome.py` after the close. **Advisory: nothing reads it at
runtime.**

It exists because the claim it replaces was narrower than it read. `news_watch`
carried *"being in the news predicts nothing"* on the strength of
`news_symbol_impact` -- 365 rows, all labelled 2026-09-06, with the
**sentiment column empty on every one**. What that measured is whether a
MENTION precedes a move (+0.086 ATR against a 1.178 sd: noise, as a mention
should be). The graded verdict had never been joined to an outcome at all, and
the whole set predates the 2026-09-07 window and macro-term fixes.

First run, 165 verdicts over 19 sessions, open to close:

```
       verdict  rows     mean   right   sessions  mean/session
  VERY_BULLISH     5  +1.110%    60%          4       +0.457%
       BULLISH    18  +0.995%    61%         13       +0.506%
       NEUTRAL   115  -0.011%      -         18       -0.115%
       BEARISH    26  -0.378%    62%         14       +0.093%   <- sign flips
```

**THE SESSION COLUMN IS THE RESULT.** By row, BEARISH separates the sessions at
62% accuracy. Averaged within a day first, it is +0.093% -- nothing. Fifteen
correlated tech names reading bearish on one morning and falling together is
one observation, and counting it as fifteen manufactured the entire effect.
Only the bullish side survives clustering, at +0.506% over 13 sessions.

**THE ERA SPLIT IS NOT COSMETIC.** Pre-fix rows came from a pipeline reading
the wrong window, so they are never pooled with post-fix ones in the summary.
Post-fix currently holds nine non-neutral verdicts across four sessions and
points the other way -- too few to mean anything either direction.

`macro_outcome.py` answers the same question for the index and had **never
produced a graded row**: it reads `news_verdicts WHERE symbol='QQQ'`, and QQQ
is not a tracked symbol -- the index is covered by the macro half, which lands
in `trading_macro_verdicts` and was already being recorded beside the outcome
as `macro_gate_verdict`. The report now falls back to it and names which read
it used. That side needs sessions too: 28 of its 29 readings are BAD.

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

## The site: dataaisys.com, one nginx, the API on loopback

Sections 201 and 202. The Next.js app (GitHub `prasadvenkat22/data-ai-solutions`,
on the droplet at `/opt/data-ai-solutions`, container `data-ai-web`) runs on the
same Docker network as this stack with **no published port**. The stack's nginx
is the only thing listening on 80/443: it proxies `/auth`, `/trading`, `/api`,
`/CRUD`, `/images`, `/static` to `app:8000` and everything else to
`data-ai-web:3000`, so the browser talks to **one origin** and no CORS is
involved. The API's own port is published on `127.0.0.1` only — a
Docker-published port bypasses UFW, and 8000 had been reachable from the
internet — and UFW allows 22, 80 and 443.

**The domain is dataaisys.com** (since 2026-09-20; data-ai-systems.com was
dropped, dataiqsystems.com before it). `app/nginx/conf.d/00-security.conf` sets
per-IP request-rate zones (site 30/s, API 10/s, **login 5/min**), a
30-connection cap, `server_tokens off`; `dataaisys.conf` is the `:80`
catch-all with security headers and the ACME path; `scripts/issue_cert.sh`
runs one certbot command that issues, widens (`--expand`, which is how www was
added after its A record appeared) and renews, then writes `dataaisys-ssl.conf`
— the HTTPS server plus the `:80` redirect for the domain names — **only once
the certificate files exist**, because nginx will not start on a missing
`ssl_certificate`. Cron on the droplet runs it Mondays 04:17 UTC. The
certificate covers the apex and www and is Let's Encrypt.

The site: public landing page (consulting expertise, the FinAI Options
Auto-Trader as the featured production system) and a public **contact form at
`/contact`**; everything under `/desk` (positions with the live ladder, closed
trades, screener board, engine controls with a two-step flatten) needs a
`trader` or `admin` login; `/ai` (ask the book, upload analysis, direct prompt)
and every CRM page (customers, products, services catalog, registrations, users,
roles, invoices, transactions, service requests) need `admin` — the navbar
hides those menus from anyone else, and the API refuses them anyway. Tokens
come from `/auth/login`, refresh through `/auth/refresh`, and travel as a
bearer on every call. Budgets, cron and env are not exposed in the UI.
Passwords (section 204): `/forgot-password` mails a link that opens the site's
`/reset-password` page (`PASSWORD_RESET_PAGE`; the API's bare fallback at
`GET /auth/reset-password` remains), `/account` changes your own with the
current one required, and the admin Users page has a Reset password button
on `POST /api/users/{id}/reset-password` — the admin users router lives under
`/api/users` because nginx hands unlisted paths, `/users` included, to Next.

**`POST /api/contact/inquiry` is the one unauthenticated write.** It stores the
inquiry as a row in `registrations` (the admin's Demo Registrations page lists
it; `status` and `notes` columns were added for this, alembic `a9c4e17b52d3`),
mails `CONTACT_EMAIL` with the visitor as Reply-To, and acknowledges the
visitor without echoing their text. nginx puts it in the login zone (5/min per
IP); a hidden `website` field is a honeypot answered 202 with no side effects.

**Product updates are a list, not accounts** (section 203). `POST
/api/contact/subscribe` stores a `subscribers` row unconfirmed and mails one
confirmation link; `GET /api/contact/confirm` sets `confirmed_at` and tells
`CONTACT_EMAIL`; `GET /api/contact/unsubscribe` is one click and never expires;
`GET /api/contact/subscribers` is the admin list. Nothing but the confirmation
mail ever goes to an unconfirmed address, and the form's answer is the same
whatever happened. A subscriber has no password, no role and no access — desk
access is still an account an admin creates for an investor.

**Mail** (`helpers/mailer.py`) goes out through SendGrid's HTTP API from
senders on the authenticated domain: `MAIL_FROM` (services@dataaisys.com) for
account mail, `ALERT_MAIL_FROM` (trading@dataaisys.com) for the price and
profit-stall alerts, which `send_alert()` routes and `ALERT_EMAIL` receives.

## GENAI: the agents run on Gemini and one of them reads the trading database

`/api/genai/*` (admin bearer token; the trading chat `/agent/ask` is admin or trader since 2026-09-23) is the RAG and multi-agent surface. Since
2026-09-19 (section 200) every model call in it goes to **gemini-3.1-flash-lite
over the same REST endpoint and `GEMINI_API_KEY` the news grader uses**
(`GENAI/gemini_llm.py`; `GeminiChat` is a LangChain chat model over that call,
`GeminiLLM` the provider behind `/llm` and `/query/upload`). Claude is gone
from the defaults; `llm_provider=anthropic` still works if a key is set.

The LangGraph supervisor has three specialists. `csv_agent` (pandas dataframe
agent) and `pdf_agent` (chunks into pgvector, then RAG) route on what was
uploaded. **`trading_db_agent`** routes when nothing was uploaded or `use_db`
is set, and answers questions about the book: Gemini writes **one `SELECT`**
against a whitelist of trading and news tables shown with a one-line note each
(never users, customers, tokens); the query is **guarded** — single statement,
no write or admin verb, whitelisted tables only, `LIMIT` added — and run in a
`READ ONLY` transaction with a 10-second timeout; questions about news also
embed the question with voyage-4 and cosine-search `market_news_vectors`
(1024 dims, the same model and width as GENAI's `documents` table); the answer
ends with the SQL. `POST /api/genai/agent/ask {"query": ...}` is the entry
point. First live answers on 2026-09-19: realised P&L by underlying for the
week with close reasons, and SanDisk's September verdict history.

**`news_agent`** (2026-09-23) answers "what is the latest news on MU": it
detects the ticker (`$TICKER`, an `ALIASES` company name, a known symbol, or a
capitalised word) and a news intent, reads the last 72 hours of stored RSS and
Polygon headlines (`market_news_vectors`, `news_seen`) read-only, falls back to
Polygon `/v2/reference/news` live when the newest stored row is over 12 hours
old or there are fewer than three, and has Gemini summarise only those
headlines. The supervisor routes news questions to it.

**The site chat is for signed-in accounts only** (2026-09-23): anonymous
visitors get no AI. `POST /api/chat/ask` (`GENAI/chat_router.py`) is mounted
behind `require_role("admin", "trader", "user")`, and a sign-up cannot log in
before its email is verified, so a token means registered and approved. It
sends news questions to `news_agent` and everything else to Gemini with a
fixed system prompt and no tools: never the trading-database agent, positions
or SQL. 2000-character input, nginx `chat` zone (20 a minute per IP). A
Gemini 429/5xx or timeout is retried once after 1.5 s; a second failure is
logged with its status and answered 503 "busy, try again", never a bare 502
(the first live chat hit exactly that blip on 2026-09-23). The
trading chat is the AI lab (`/api/genai/agent/ask`, admin and trader; uploads and direct prompt admin only). The widget shows
anonymous visitors a sign-up prompt, and only a desk admin gets file uploads.

## Trade buckets (section 227)

Three entry switches, **off by default**, on `/desk/settings` (first group) or
`tset`: `TRADING_BUCKET_QQQ_0DTE` (the engine's own QQQ entries; action
`BUCKET_OFF`), `TRADING_BUCKET_STOCK_0DTE` and `TRADING_BUCKET_STOCK_WEEKLY`
(`dte0_trade.py`, which then runs as a dry run). Off stops NEW entries only;
open positions keep their exits. `TRADING_DTE0_LIVE` / `TRADING_LIVE_ORDERS`
still exist beneath them as the account-level live switches.

## The Macro panel and data releases (section 222)

`GET /trading/macro` (admin/trader) returns what the engine's risk gates see:
10Y, VIX and crude against the session open with the thresholds that force
risk-off (`TRADING_TNX_SPIKE_BPS`, `TRADING_VIX_LEVEL_MAX`, `TRADING_VIX_SPIKE_PCT`,
`TRADING_CRUDE_SPIKE_PCT`), the rotation's QQQ macro verdict, and today's
calendar: FOMC / `TRADING_EVENT_DATES` plus `config/data_releases.json`
(informational, never a blackout; dates only from the publishers' calendars).
The desk shows it as the Macro card. The weekly/0DTE rotation refuses call
debits whenever the 10Y is spiking, not only when its 15-minute verdict turns.

## HTTP: the screener is callable

```
GET  /trading/positions        every structure the BROKER holds + its live ladder state
GET  /trading/position         the ENGINE's own row only
GET  /trading/screener/verticals?symbols=CRWV,AVGO&side=call&structure=debit&by=edge&per_symbol=2
GET  /trading/screener/verticals?...&expiry=2026-09-21      one date for the whole board; "+3" = first expiry >= 3 days out
GET  /trading/screener/flow?symbols=CRWV,AVGO,SNDK
POST /api/genai/agent/ask   {"query": "..."}   ask the trading book; Gemini writes a guarded SELECT (§200)

All of these are reachable only through nginx on the site's origin since §201; the API port is loopback-only.
`/docs`, `/redoc` and `/openapi.json` return 404 publicly; read them over an SSH tunnel to 127.0.0.1:8000.
POST /trading/flatten?confirm=LIQUIDATE&preview=true
POST /trading/flatten?confirm=LIQUIDATE&preview=false&plan_token=<from the preview>
```

**`expiry` matters on a Monday.** Without it each name ranks on its NEAREST
expiry, which is the same day for MU, NVDA, TSLA, AMZN, AAPL, META, MSFT, GOOGL,
AMD, AVGO and INTC and the Friday for SNDK, CRWV, MRVL, PANW, DELL, STX and WDC.
`scripts/board.py <date> [--weekly]` prints the same ranking from the shell.

**Before the open the board must not 500** (§205, 2026-09-21). yfinance can
append today's row to the daily history with every price NaN; `evaluate()`
drops bars with no Close/High/Low before any maths, and both screener
endpoints pass their payload through `_json_safe`, which turns any NaN or
±inf into `null` rather than letting FastAPI's `allow_nan=False` encoder fail
the whole response. A null reads as "not measured"; a bodyless 500 reads as
nothing.

**The screener prices off the broker's book, Yahoo is the fallback** (§206,
2026-09-21). Yahoo's chains now carry bid/ask on only a fraction of strikes
(AAPL 09-25 calls: 0 of 68), so `usable()` rejected everything and the board
returned zero rows for twelve names with no warning. `weekly_pick.chain_quotes()`
takes `data_feed.fetch_option_chain()` (Tradier, greeks on, `mid_iv` as the IV)
and falls back to `yfinance` only when the broker returns nothing; `meta.quotes`
names the source, and `rank()` adds a warning for any symbol that yields no
candidate, naming the filter that emptied it.

**Every board row carries the week's VWAP** (§212, 2026-09-21). One
`weekly_vwap_gate.read()` per underlying, the same anchored read the weekly
gate makes at entry, so the column and the verdict cannot disagree:
`week_vwap`, `week_vwap_side` (ABOVE/AT/BELOW inside a quarter-ATR band),
`week_vwap_slope_pct`, `week_vwap_sessions`, `week_vwap_trend` (**LONG** above
and rising, **SHORT** below and falling, **MIXED** otherwise) and
`week_vwap_conflict` when a row's direction leans against the week. Shown, not
ranked on. The site's screener board renders it as a "Week VWAP" column.

**And the volatility regime** (§213): `iv` (ATM implied of the screened expiry),
`rv` (20-day realised), `iv_rv` and `vol_regime` — **RICH** ≥ 1.2 favours selling
spreads, **CHEAP** ≤ 0.8 favours buying them, FAIR between — on every row and
every underlying; the board shows it as an "IV/RV" column.

**And the chain's deltas** (§215): `delta_long`, `delta_short`, `delta_net` on
every row — the leg you own, the leg you sold, and the market's odds of finishing
between the strikes — beside Pwin for comparison, not an input to the ranking. A
same-day cross-section on 2026-09-22 showed net delta over 0.50 selecting the
widest, most expensive spreads with the lowest P(max) and negative average edge;
the board flags such rows in amber so the comparison is visible before any rule
is written. The two paper books
already run the credit branch by this ratio: `dte0_shadow` chooses credit or debit
from it at 09:45 and records both directions, `weekly_shadow` stores it beside every
Friday credit row. `scripts/shadow_iv_rv_report.py` scores both by bucket.
The board prices the ODDS, not the direction: a name whose options are cheap
against its realised moves tops both the call and the put list (TSLA, 2026-09-21).
Direction comes from the macro veto, the day's news verdict and the tape gate,
none of which exist before the session opens.

**`/trading/positions` answers "why is this still open?"** Per structure it
returns entry, mark, `intrinsic`/`extrinsic`, return and peak, then the ladder
*as it applies to that structure*: `stop_pct`, `stop_confirm_minutes`,
`stall_giveback_points`, `stall_quiet_minutes`, `stall_armed`,
`stall_min_gain_pct`, `drag_ceiling`, `drag_now`, `drag_blocks`, `hold_until`,
`past_hold`, `managed`, `quote_tradeable`.

**Every one is asked of the function the engine asks, never read off a
setting** (§177). The first version read the knobs and reported the flat
giveback percents the band overrides — 40.0 on a weekly whose real threshold
was 9.1 points — plus null for the stop on a weekly that has one. `drag_blocks`
is the field that explains a held winner, and it was the one missing.

Read-only, deliberately: the exit rules run in the cycle, not in a request
handler, and a dashboard that can trade is a dashboard that will.

**`/flatten` closes MANUAL spreads only** — engine positions are excluded by
passing their legs as `engine_symbols`, the same way `orphans.review()` does.
Four guards, each from an incident (sections 140–141):

| guard | why |
|---|---|
| structures, never legs | legging out turns a long into a **naked short** |
| clamped to holdings | the pairing said `SNDK 1750/1800 x5` when three existed. **`orphans._close()` got the same clamp on 2026-09-18** — it had none, and a rejected close reads as success (§185) |
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

`orphans.py`, every cycle. The values below are `.env.production`; any of the
whitelisted knobs can be overridden live (next section), so `tset list` on the
droplet or `/desk/settings` is the authority on what is in force today.

### Tuning without a restart (2026-09-23, section 216)

`trading_engine/settings_overrides.py` holds a whitelist (`REGISTRY`) of about
37 exit and entry knobs with types and bounds. Overrides live in
`/opt/fastapi/trading_overrides.env` (gitignored, audit trail in
`trading_overrides.log`) and `trading_engine/__init__.py` loads them into
`os.environ` before any module reads a knob. Cron starts a fresh process each
minute, so the next cycle trades on a change; the uvicorn process only sees it
after an `app` recreate, which is why the Positions page's rule columns lag.
Precedence: override > `.env.production` > code default. Going live
(`TRADING_DTE0_LIVE`, `TRADING_MANAGE_ORPHANS`) is deliberately not tunable.
Writers: `PUT /trading/settings` (role `admin`), `scripts/settings.py`
(`list/get/set/unset/reset/log`), both validating before a single atomic write.

**Two stalls, two groups** (section 228). The "0DTE exits" stall
(`TRADING_ORPHAN_STALL_*`) is orphans.py's and manages positions the engine did
not open. The QQQ bucket's own trades exit on nodes.py's stall, tunable in the
"QQQ engine exits" group: `TRADING_STALL_MINUTES` / `TRADING_STALL_GIVEBACK_PCT`
(morning debit), `TRADING_STALL_ON_CREDIT`, `TRADING_CREDIT_STALL_ARM`, and
`TRADING_CREDIT_STALL_MINUTES` / `_GIVEBACK_PCT` (blank = follow the morning
values). `tset list --group engine` shows them.

**Same-day stall arm** (section 229): `TRADING_ORPHAN_STALL_ARM` ("Stall starts
watching at", 0DTE exits) holds the same-day stall off until the peak gain
reaches it; after that each new intrinsic high resets the watch level and the
`TRADING_ORPHAN_STALL_MINUTES` clock. 0 = watch from any gain.

**Ask mode is the auto-managed 0DTE sell limit** (`TRADING_ORPHAN_ASK_*`,
tunable since section 217): on a pinned spread it rests a sell at START x
width, holds while the underlying is at or above session VWAP, steps down
while it is below, never under FLOOR, and every loss rule and the flatten stay
live. A manual limit on the same legs switches all of that off (in_flight);
the log now says so every cycle: "an order this engine did not place is
working on its legs".

**The ladder in force at the end of 2026-09-23** (sections 224-226; the
overrides file is the authority -- `tset list`):

| | Same-day (expires today) | Weekly / later expiry |
|---|---|---|
| Loss | `STOP_PCT=-20` with a 5-minute confirmation (09-24), no intrinsic hold-off (`STOP_RESPECTS_INTRINSIC=false`) | `LATER_STOP_PCT=-20` with 2+ sessions left, `LATER_STOP_PCT_1D=-10` on the last day, both with a 2-minute confirmation; `LATER_SCALE_DAYS=2` makes it a step (section 226) |
| Profit | profit lock at cost + 10% of width on the mark (`PROFIT_LOCK_WIDTH=0.10`); resting ask 0.90 x width, floor 0.80 | `LATER_TARGET_PCT=0.90` of width; later stall (arm +25%, 30 min, 0.25 ATR) |
| Off | underlying stop, strike guard (code kept, tunable) | |
| End | flatten 15:45 (ask withdrawn 15:40) | held overnight |

On its expiry day a weekly IS a same-day position: from 09:35 it gets the
left-hand column. A fresh fill marks at the sell side and usually reads 10-20%
under its cost on MU-sized spreads, so the -5% first-pass stop closes most
offer-side entries about a minute in; entries near the mid avoid that.

**Between the short strike and break-even** (sections 218-219): the stop and
the tape exit both hold off while intrinsic is above the entry, so the
**short-strike guard** (`TRADING_ORPHAN_STRIKE_GUARD*`) covers that slide: the
underlying through the short strike and under an adverse VWAP for N continuous
minutes closes the position (cancelling the ask first); a bounce back over the
strike or a VWAP that stops moving against it resets the clock.

Current settings:

```
TRADING_ORPHAN_UNDERLYING=          empty = EVERY symbol
TRADING_ORPHAN_HOLD_UNTIL=09:35     0DTE waits five minutes for the open to settle
TRADING_ORPHAN_LATER_HOLD_UNTIL=09:45   weeklies wait out the opening spread
TRADING_ORPHAN_ACT_EXPIRY_DAY_ONLY=false
TRADING_ORPHAN_LATER_STALL_ARM=25   arm only on a real run
TRADING_ORPHAN_LATER_STALL_GIVEBACK=40
TRADING_ORPHAN_LATER_STALL_GIVEBACK_ATR=0.25   the weekly give-back basis; BAND=0 hands over to it
TRADING_ORPHAN_LATER_STALL_MINUTES=30
TRADING_ORPHAN_LATER_TARGET_PCT=0.95    see the ordering note below
TRADING_ORPHAN_LATER_STOP_PCT=-45   held 15 min; respects intrinsic -- the FULL-WEEK value
TRADING_ORPHAN_LATER_SCALE=true     interpolate every LATER number by sessions left (§199): 1-session anchors
  TRADING_ORPHAN_LATER_STOP_PCT_1D=-30  _STOP_MINUTES_1D=5  _STALL_ARM_1D=5  _STALL_MINUTES_1D=10  _STALL_GIVEBACK_ATR_1D=0.10
TRADING_ORPHAN_ASK=true             rest a sell above the bid on a pinned 0DTE spread (§193)
TRADING_ORPHAN_ASK_START_WIDTH=0.88 TRADING_ORPHAN_ASK_STEP=0.10 TRADING_ORPHAN_ASK_STEP_MINUTES=3
TRADING_ORPHAN_ASK_VWAP_FROM=09:40  TRADING_ORPHAN_ASK_CANCEL_BY=15:40  floor = the TARGET level
TRADING_DTE0_VWAP_GATE=veto         the tape veto on single-name entries (§194); record = log only
TRADING_VWAP_MIN_BARS=3  TRADING_VWAP_SLOPE_BARS=6  TRADING_VWAP_BARS_ABOVE_MIN=0.60
TRADING_VWAP_GATE_UNTIL=            blank since 2026-09-21 = veto all day; was 10:30, veto until here and record after (§194)
TRADING_WEEKLY_VWAP_GATE=veto       the WEEK-anchored VWAP gate on weekly-book entries (§210); record = log only, off = skip
TRADING_WEEKLY_VWAP_TOL_ATR=0.25  TRADING_WEEKLY_VWAP_SLOPE_BARS=6  TRADING_WEEKLY_VWAP_MIN_BARS=3
TRADING_SEC_USER_AGENT=<name email>  required by EDGAR; the edgar-* feeds are skipped without it (§196)
TRADING_DTE0_OPTIONS_FLOW=record    OPTIONS line per candidate; veto refuses against the read (§197)
TRADING_OPTFLOW_CP_RATIO=2.0  TRADING_OPTFLOW_TURNOVER=0.5  TRADING_OPTFLOW_MIN_VOLUME=500
TRADING_INDEX_EVENT_LIVE=false      "would place" until true (§197)
TRADING_INDEX_EVENT_BUDGET=1000  TRADING_INDEX_EVENT_MIN_PWIN=0.40  TRADING_INDEX_EVENT_MIN_DAYS=1
TRADING_DTE0_MAX_BUDGET=5000        ceiling on the 0DTE rotation's --budget (cron passes 5000 / 4 slots)
TRADING_WEEKLY_MAX_BUDGET=5000      ceiling on the weekly book's --budget (cron passes 5000 / 3 slots)
TRADING_WEEKLY_MIN_ENTRY_WIDTH=0.20 TRADING_WEEKLY_MIN_PWIN=0.45 TRADING_WEEKLY_RR_MIN=1.0 TRADING_WEEKLY_RR_MAX=3.0
TRADING_MAX_ORDER_CONTRACTS=10      raised from 5 on 2026-09-19 after 5+5 fills on the QQQ close; one order covers the 9-lot MU exit and its ask
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

Worked on 2026-09-10: `QQQ 707/710 x3` peaked at +31.3% at 15:10, drifted rather
than turned, so no give-back window ever met the threshold — the **15:45 flatten**
booked it at +$99 (+18.4%) and left nothing to be assigned into the close. On
expiry day that backstop is the exit that matters; the stall is the one that
does not fire.

**The target must sit BELOW what the stall can reach, or the stall is dead
code.** At `TARGET_PCT=0.75` on SNDK 1670/1730 the take-profit needed 1715 while
the stall needed a peak near 1740 to have anything to give back — the target
always fired first and the trail never ran. Raised to **0.90**, so the position
has to climb far enough for the trail to arm before the target takes it. These
two numbers are one setting, not two, and changing either alone re-orders them.

**A successful close no longer logs at ERROR.** `_post_order` raises
`OrderError` on every Tradier rejection, so the line after `submit_vertical`
only runs when the order was accepted — it logged at ERROR anyway, which meant
`grep ERROR` on the trading log returned mostly confirmations and buried the
rejections worth finding. Now INFO, except `status: suppressed`, which is
WARNING: nothing was sent, and the engine believes it closed a position that is
still open at the broker.

Worked, unattended, on 2026-09-10: `SNDK 1675/1725 x3` peaked at +89.2%, gave
back past 15 points with 5 minutes since the last high, and booked **+$771**.

**A weekly now CAN have a stop, and it is off by default.** Both original
stops are gated on `zero_dte`, so `TRADING_ORPHAN_STOP_PCT=-10` did nothing to
a 09/18 position until 09/18 itself -- and it could not be switched on with the
existing flags, because `TRADING_ORPHAN_TODAY_ONLY=false` grants the stop and
the 15:45 flatten together, which would close a five-day position on day one.
`TRADING_ORPHAN_LATER_STOP_PCT` is its own gate, with three guards the 0DTE
stop does not need:

```
debits only        -10% on a credit structure is absurd; credit keeps
                   ORPHAN_CREDIT_STOP_PCT (-600, off) as on expiry day
intrinsic wins     section 88's SNDK 1600/1700 sat at full intrinsic
                   (+3,825 at expiry) marking -0.7% a week out
must persist       TRADING_ORPHAN_LATER_STOP_MINUTES, default 15. A weekly's
                   quote is wide and has no convergence pressure, so one print
                   at -12% means nothing. The 0DTE stop confirms in 0 minutes
                   because there the clock IS the risk; here it is free.
```

Default 0. Section 88 measured stops as a tax on multi-day positions and
nothing since contradicts it -- every stop that fired on 2026-09-11 was on an
expiry-day position.

**Three bases for the give-back, and only one travels between positions.**
Anchored to the ENTRY, one setting is several rules -- measured 2026-09-11:

```
QQQ  714/717 x18 @ 2.04   max return +47%   40 pts = 85% of the profit band
SNDK 1670/1730 x2 @ 32.27 max return +86%   40 pts = 47% of the profit band
```

On the QQQ spread that surrendered $1,469 of a $1,728 peak before the trail
could act; the same 40 booked +$771 on SNDK the day before. An ITM debit spread
whose cost is two thirds of its width has almost no band for a fixed give-back
to sit inside. `TRADING_ORPHAN_STALL_GIVEBACK_FRACTION` expresses it as a share
of the PEAK instead, which is self-scaling and is what the engine's own trail
has always done (`TRADING_TRAIL_GIVEBACK=0.20` is 20% of the peak). At 0.30 a
+47% peak gives back 14.1 points and a +21% peak gives back 6.3. **Off by
default**: every give-back measurement on this account was taken on the flat
percent, and this changes what the number means, not only its value.

**A fixed percent target only fires if it is below `(width - entry) / entry`.**
Three positions in one session had an unreachable take-profit: 55% against a
+47% ceiling, then +25%, then +47% again. The rung silently does nothing and
the 15:45 flatten becomes the exit. The same arithmetic is already written
beside the engine's own `TAKE_PROFIT_PCT` -- *"a target that can't be hit isn't
a target"* -- and it applies to the orphan path identically.

**The stall decides on intrinsic and executes at the MARK.** On 2026-09-11 the
QQQ spread carried a -0.45 quote discount, which is $810 on 18 lots -- larger
than the difference between a 15-point and a 40-point give-back. Any trail exit
before the quote converges gives that up on top of whatever the rule surrenders.

**The give-back is a percent of ENTRY, so it re-tunes itself on every roll.**
Three SNDK positions in two days at a constant setting of 30 meant 7.86, 6.31
and 5.40 points of the underlying — the same number, three different rules.
`TRADING_ORPHAN_LATER_STALL_GIVEBACK_ATR` expresses it as a fraction of ATR14
instead, which holds across rolls. **Off by default**: SNDK's 97-point ATR
against a 50-wide spread makes any sensible fraction larger than the whole
profit band, so it needs an instrument where the two are better matched.
`stall_replay.py --giveback-atr` tests either before it goes live.

**Two exit paths place orders, and both need the holdings check.**
`service._broker_holds()` guards the engine's own exits; `orphans.py` calls
`submit_vertical` **directly** and bypasses it entirely. A guard on one path is
not a guard — both are now checked (section 142).

**`None` means the account is unreadable, `{}` means it is flat.** Those are
opposite facts and `not held` spelled them the same way, so a flat account
resurrected six closed structures — the oldest four days stale — and two of
them submitted real closing orders once a minute. The bug only lives in the
window between closing one position and opening the next, which on an active
account is minutes at a time (section 142).

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

`underlying_trigger.py SYM LONG SHORT FLOOR [--cushion C --shade S]` is the
one operator tool that **does trade**: a one-shot close of a named debit spread
when the **share price** crosses the level, through `orphans._close` so the
holdings clamp applies. Below the level for a call spread, above it for a put.
With `--cushion` the level trails the best print and only ever tightens, capped
a `shade` inside the short strike because past the short strike a debit spread
has nothing more to earn (sections 190–191). It runs as a detached process in
the container; kill it with a pattern anchored to the process, never with a bare
`pkill -f` over ssh, which matches the ssh session itself.

**Index events** (`index_events.py`, section 197). Every headline the sweep
stores is scanned for "<names> set to join / will replace / added to the S&P
500 | S&P 100 | Nasdaq-100" and the inclusion phrasing; only names **before the
verb** are matched against the alias table, so "(Not Micron or Sandisk)" after
the verb cannot fire. A hit becomes a row in `index_events` with an effective
close: the third Friday when announced in a rebalance month at least three
days ahead, otherwise announcement + 7 days. `scripts/index_event_trade.py`
(cron 10:05 ET weekdays) takes one call debit per join event on the latest
expiry on or before that close, edge-ranked, Pwin ≥ 0.40, EV > 0, one contract
within `TRADING_INDEX_EVENT_BUDGET`; **never on the effective day**, because
the closing tape predicts nothing after (§193). Live only when
`TRADING_INDEX_EVENT_LIVE=true`; otherwise "would place".

**Options flow** (`options_flow.py`, section 197). From the chain the rotation
already fetches, on the traded expiry plus the next: call vs put volume,
turnover (volume / open interest) per side, the top strikes by turnover with
notional. `bullish` needs calls ≥ 2× puts, call turnover ≥ 0.5 and ≥ 500
contracts; `bearish` mirrors; else `neutral`. `TRADING_DTE0_OPTIONS_FLOW` is
`record` (default: one OPTIONS line per candidate) | `veto` | `off`.

**The weekly book** (section 198) runs through the same script: `dte0_trade.py
--book weekly --rotate --live --budget 5000 --max-trades 3 --expiry friday`, cron
**09:50 and 13:50 ET, Monday to Wednesday**, over the 18-name universe. Same
gate chain in the same order (macro veto, news veto, tape veto until 10:30,
options-flow line, EV/Pwin/edge ranking, rotation cooldown, quote-width
ceiling, per-slot budget, already-held check on **any** expiry). Four things
differ and nothing else: the expiry resolves to this Friday from Monday to
Wednesday and next Friday after; the ceiling is `TRADING_WEEKLY_MAX_BUDGET`;
the structure band is the plan's, not the day's — entry 20–75% of width, R:R
1..3 ranked by edge, **Pwin ≥ 0.45**, with the 0DTE extrinsic and ATR-distance
limits switched off because a five-day spread is mostly time value and a daily
ATR is the wrong ruler; and the positions land in the LATER ladder. Budgets are
**not** an endpoint: each is the `--budget` argument on a cron line, capped by
`TRADING_DTE0_MAX_BUDGET` / `TRADING_WEEKLY_MAX_BUDGET` in the server env.

**News sources, after 2026-09-19.** The hourly sweep (`news_enrich.py`, cron
13–20 UTC weekdays) reads two kinds of feed. **Macro wires** — Fed, CNBC,
MarketWatch, Investing.com, Yahoo — are classified by Gemini into the macro
verdict. **Company feeds** are stored in `news_seen` for the per-symbol grader
in `news_hourly.py` (alias match on the title) and never reach the macro model:
Benzinga's wire; Google News RSS searches for index changes (`"set to join"`,
`"will replace"`, `"rebalance"` against S&P 500/100 and Nasdaq-100), for S&P
Dow Jones Indices releases on PR Newswire, for Business Wire and Benzinga
stories naming the universe, and for Benzinga options-flow stories; and SEC
EDGAR Atom feeds per company by numeric CIK, 8-K and SC 13D, each with the
filer's name prefixed to the title so the alias matcher can attribute a
filing titled only "8-K - Current report". spglobal.com and its RSS answer 403
to every non-browser client; Google News is the only public route to those
releases. EDGAR requires `TRADING_SEC_USER_AGENT` with a contact address.
Filing and index-release feeds are quiet by nature and are exempt from the
dead/stale warnings. Section 196.

**Single-name 0DTE entries** (`dte0_trade.py --rotate --live`, every 15 min
09:00–13:45 ET, budget **$5,000 over four slots** since 2026-09-19, no entries after 13:30) clear, in
order: the **macro veto** (bearish read refuses call debits, bullish refuses
puts), the **Polygon news veto** per symbol (BEARISH ≥ 0.50 confidence refuses
calls, BULLISH refuses puts), the **VWAP flow veto** (`vwap_gate.py`, section
194: a call debit needs spot above the running session VWAP, VWAP higher than
30 minutes ago, ≥ 60% of 5-minute bars closing above it, and volume arriving on
closes near bar highs; puts need the mirror; an unreadable tape refuses; it vetoed only on runs up to 10:30
and recorded after, because the tape's direction measured as continuing into the flatten from the open and
reversing from 11:00 — **since 2026-09-21 it vetoes all day** at the operator's decision, §207–208 era, with the
afternoon refusals still logged so that choice can be scored), and for the **weekly book only** the
**week-anchored VWAP gate** (`weekly_vwap_gate.py`, §210: cumulative VWAP from Monday's open over Tradier's
5-minute bars; a call debit needs spot ABOVE it by more than a quarter-ATR band and the level rising over the
last 30 minutes, a put the mirror; unreadable refuses; `TRADING_WEEKLY_VWAP_GATE` veto|record|off, veto by
the operator's choice before any outcome data, every reading logged as a WEEKVWAP line), then
**EV, Pwin and edge** ranking under the structure limits. Every veto only
removes; nothing in news or the tape proposes a trade. `TRADING_DTE0_VWAP_GATE`
is `veto` | `record` | `off`; `scripts/flow_gate_replay.py` replays the gate
over past rotation entries. Weekly single-name debits have **no automatic
path** yet (WEEKLY_SINGLE_NAMES_PLAN.md).

**ASK mode** (`TRADING_ORPHAN_ASK=true`, 0DTE only, section 193): when a debit
spread sits at **full intrinsic** and nothing in the ladder wants to act, the
engine rests a sell at **88% of width**, holds it while the underlying is at or
above session **VWAP**, steps it **−0.10 every 3 min** while below (from 09:40),
never below the **+70% TARGET level**, and withdraws it at **15:40** so the
flatten runs. It suppresses only `TARGET`/`CEILING`/`LATER_TARGET`; every stop,
the stalls and the give-back **cancel the ask first, confirm, then act** — a
cancel that comes back filled is booked, an unconfirmed cancel sends nothing.
Measured basis: pinned spreads bid a median 59% of width in the first half
hour and 90%+ in one cycle in five at midday, never 93%.

**A sell-all is not a stand-down.** `dte0_trade --rotate` fires every 15 minutes
and does not know the operator wants to be flat; on 2026-09-18 it opened two
spreads four seconds into a sell-all. Disable the rotation first, then close.

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
