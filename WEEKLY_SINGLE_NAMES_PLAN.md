# Weekly single-name book — design for approval (2026-09-18)

Budget $2,000. EV- and Pwin-ranked, news-gated. Call debit, put debit, or
iron condor, chosen per name by what the news says. Nothing here is code yet.

## 1. What the data already says

Two weeks of weekly_shadow (124 rows, 79 settled, all CREDIT structures at
~0.12 delta):

    strategy        n  settled  avg ret%  worst%   win
    WEEKLY_CALL    41     26     -16.7   -1011    0.54
    WEEKLY_CONDOR  41     26      28.2    -646    0.56
    WEEKLY_PUT     42     27      68.3    -668    0.62

    12 of 15 names: 100% win, ~+100% return (kept the whole credit)
    SNDK -297%  STX -281%  META -184%   -- the three that moved

Read: a 0.12-delta credit book is a coin that pays $1 eleven times and takes
$10 once, and the names that move are exactly the ones the 0DTE book profits
from. That is why this book is DEBITS: a debit spread on a name with a
catalyst has bounded loss (the debit) and a known ceiling (the width), and the
manual SNDK and MU weeklies this week were debits.

The other constraint is arithmetic. SNDK 1605/1710 cost $6,680; MU 975/990
cost $660. At $2,000 the book cannot hold one SNDK-size spread. Widths are
sized to the budget, not to the name.

## 2. Universe and structure choice

Universe = TRADING_WEEKLY_SYMBOLS minus QQQ (the index is the other book):
AMZN AVGO CRWV DELL GOOGL META MRVL MSFT MU NVDA PANW SNDK STX WDC + INTC.

Structure is chosen by the news verdict, not by hand:

    verdict (news_verdicts + polygon+rss grade)   structure
    VERY_BULLISH / BULLISH                        call debit
    VERY_BEARISH / BEARISH                        put debit
    NEUTRAL with IV rank >= 60                    iron condor (credit)
    NEUTRAL with IV rank <  60                    no trade

Condors ONLY on neutral news and rich IV. The shadow data shows a condor
loses 6x its credit when the name moves; a neutral read is the one case
where that risk is being paid for.

## 3. Ranking: EV and Pwin, both must clear

Per name, per structure, sweep widths (CLAUDE.md: 0.50 / 1 / 2.50 / 5 / 10,
snapped to the name's strike grid) and compute:

    Pwin   = P(terminal spot beyond breakeven)  -- monte_carlo_terminal from
             weekly_pick.py, ATR-14 scaled to trading_days_to(expiry)
    EV     = Pwin * (width - debit) - (1 - Pwin) * debit      (debits)
           = Pwin * credit - (1 - Pwin) * (width - credit)    (condor)
    EV_adj = EV_dem + w * conf * (EV_raw - EV_dem)            (weekly_pick)
    slip   = (ask - bid) summed over legs, charged against EV (CLAUDE.md)

Gates, every one required:

    EV_adj - slip     > 0
    Pwin              >= 0.45 debits, >= 0.70 condors
    edge = Pwin - need >= 0.05   (need = debit / width)
    open interest     >= 200 per leg, bid-ask <= 10% of mid
    no event inside the expiry: describe_event / event_blackout_active plus
      the earnings date from yfinance -- a weekly debit into earnings is a
      lottery ticket, not an EV trade

Rank by EV_adj per dollar of debit (evpct). Take the top 2. Two is the
budget, not a preference: $2,000 / 2 = $1,000 a slot, which at a $5-wide
spread priced ~$2.00 is 4-5 contracts; at $10-wide, 1-2.

## 4. Sizing

    per_trade_cap = budget / 2 = $1,000      (dte0_trade pattern: size to the
                                              SLOT; unspent budget stays unspent)
    contracts     = floor(per_trade_cap / (debit * 100))
    max width     = widest width whose 1-lot cost <= per_trade_cap
    condor        = (width - credit) * 100 * qty <= per_trade_cap

Max book loss is the budget. Nothing in this book can lose more than $2,000
in a week by construction.

## 5. Entry

Not weekly_shadow's Friday 15:45 slot. That slot exists to sell Friday's
decay; debits want to be bought when the news is fresh and the move has not
happened yet.

    - Screen at 09:45 and 13:00 ET, Mon-Wed (cron, same shape as
      dte0_trade --rotate). No Thu/Fri entries into that week's expiry --
      under 2 DTE is the 0DTE book's job.
    - Expiry: nearest Friday >= 3 trading days out ("+3" in weekly_pick).
    - Macro gate: the objective crude/10Y/VIX read. BAD blocks debits in
      the direction the macro read disagrees with, not both directions.
    - News freshness: verdict < 24h old. A three-day-old BULLISH grade is
      not a catalyst.
    - One position per name, max 2 open, no re-entry on a name closed at a
      loss that week.

## 6. Exit — the weekly ladder already built this week

Positions land in orphans.py as non-0DTE and get the LATER ladder as
configured today:

    LATER_STOP        -25%, confirm 15 min
    LATER_STALL       20 min quiet, giveback 20% of (width - entry)
    LATER_TARGET      0.95 x width intrinsic
    TARGET            +70% of entry on mark, drag- and hold-gated
    drag guard        15% width ceiling, release at 10% width off peak
    STALL gain floor  +8% of entry on mark
    hold window       no profit-side exit before 09:30 ET

Plus the underlying trigger (underlying_trigger.py) -- the thing armed by
hand on SNDK and MU this week -- made automatic: arm at short strike minus
a cushion once intrinsic >= 85% of width.

Condors: close at 50% of credit (LIVE_TARGET_PCT is already 50) or at 2x
credit loss, whichever first; force close Friday 15:30 (CLOSE_BY).

NOT MEASURED: none of this ladder has a settled weekly backtest -- the
harness cannot settle weeklies. This book is what produces that data.
Every entry writes to weekly_shadow with sig_* filled and the CHOSEN
structure recorded, so in four weeks "did the news pick the right
structure" is a query, not an opinion.

## 7. Shadow first, then live

    Weeks 1-2   shadow: rank, size, log the two picks; place NOTHING.
                Settle at the expiry close (extend expiry_closes.py to the
                expiry date rather than the entry date).
    Week 3+     live at 1 contract per slot if shadow realised EV > 0 and
                no pick lost more than its debit (sizing held).
    Then        full $1,000 slots.

## 8. What has to be written

    scripts/weekly_trade.py         screen + rank + size + place;
                                    --budget 2000 --max-trades 2
                                    --shadow | --live; built from
                                    dte0_trade.py and weekly_pick.py
    trading_engine/weekly_shadow.py new strategy rows WEEKLY_CALL_DEBIT /
                                    WEEKLY_PUT_DEBIT; structure, verdict,
                                    EV_adj, Pwin stored per row
    scripts/expiry_closes.py        settle by EXPIRY date -> weekly ladder
                                    becomes measurable in exit_backtest.py
    trading_engine/orphans.py       auto-arm the underlying trigger at 85%
                                    of width (today a hand-launched script)
    routes/trading_router.py        /trading/weekly/picks -- the ranked
                                    board with every gate's reason, like
                                    /trading/screener/verticals
    env                             TRADING_WEEKLY_DEBIT_BUDGET=2000
                                    ..._MAX_TRADES=2  ..._MIN_PWIN=0.45
                                    ..._CONDOR_MIN_PWIN=0.70
                                    ..._IVR_FLOOR=60  ..._NEWS_MAX_AGE_H=24
                                    ..._LIVE=false

## 9. Risks, stated up front

- The news is mostly NEUTRAL. news_verdicts, last 7 days, 18 names:
  NEUTRAL 30, BULLISH 10, BEARISH 4. Under the structure rule in section 2
  that is roughly one to two debit candidates a day across the whole
  universe, before EV, Pwin and liquidity gates. Some weeks the book will
  hold one position or none; that is the rule working, not failing. Do not
  loosen the verdict gate to fill the second slot.
- weekly_shadow already stores sig_news_latest and sig_news_count_3d on 45
  of its 124 rows, so "did the verdict agree with the outcome" can be run
  on the existing credit book today, before a line of the debit book is
  written. That is the first thing to do.

- weekly_pick weights are chosen, not fitted. EV_adj is EV_dem with an
  opinion added; the shadow weeks test that opinion, and w = 0 is the
  fallback if it does not beat it.
- $2,000 on -25% stops means a bad week is -$500 to -$1,000 realised
  before any position reaches max loss. Expect it.
- Two positions a week is ~8 a month; at the sample sizes that have burned
  this project before (section 119), ~3 months before anything is known.
