"""a paper 0DTE book for the names that have Monday and Wednesday expiries

WHY A SHADOW AND NOT A TRADE. The engine's 0DTE record is QQQ-only and thin --
MORNING_DRIFT 6 live trades, AFTERNOON_CREDIT 7 and negative -- and there is
no single-name 0DTE evidence in this repository at all. weekly_shadow records
five-day holds; a Monday NVDA spread is a different instrument with different
gamma, and nothing here says whether it works.

WHICH NAMES, measured 2026-09-12 against the chain rather than assumed:

    QQQ                                   daily
    AMZN AVGO GOOGL META MSFT NVDA MU     Mon, Wed, Fri
    SNDK CRWV                             Friday only

So the two names with the worst weekly tails are also the two that cannot do
mid-week 0DTE, which is convenient rather than planned.

AND WHICH ARE TRADEABLE IS A SEPARATE QUESTION. Median near-ATM quote width on
Monday's chain, as a share of mid:

    NVDA 2.6%   MU 2.8%   QQQ 3.9%   META 6.3%
    AMZN 11.9%  GOOGL 13.3%  MSFT 17.0%  AVGO 21.2%

A vertical pays that twice, on two legs, in and out. Against a spread whose
maximum return is 30-50%, AVGO's quote eats the trade before direction
matters. The shadow records the width it paid so a result can be separated
from the cost of getting it.

STRUCTURE IS CHOSEN ON IV/RV, which section 50 established as the only column
on this book with measured backing: a credit spread's break-even win rate IS
its risk ratio and delta IS the market's probability estimate, so the only
place an edge can come from is implied exceeding realised. Above 1.0 the
shadow sells premium, below it buys.

Revision ID: d4b91e7a2c68
Revises: c8e4a1f37b92
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d4b91e7a2c68"
down_revision: Union[str, None] = "c8e4a1f37b92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS dte0_shadow (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR NOT NULL,
            trading_day DATE NOT NULL,
            expiration VARCHAR NOT NULL,
            opened_at TIMESTAMPTZ DEFAULT now(),
            -- CALL or PUT: both sides are recorded every session so the data
            -- can answer which one worked instead of only the one a signal
            -- happened to pick.
            variant VARCHAR NOT NULL,
            -- DEBIT or CREDIT, decided by iv_rv_ratio at entry
            structure VARCHAR NOT NULL,
            long_strike DOUBLE PRECISION,
            short_strike DOUBLE PRECISION,
            width DOUBLE PRECISION,
            spot_at_entry DOUBLE PRECISION,
            -- the basis for the structure choice, stored so a later analysis
            -- can condition on it rather than recompute it from stale data
            atm_iv DOUBLE PRECISION,
            rv20 DOUBLE PRECISION,
            iv_rv_ratio DOUBLE PRECISION,
            -- what it cost to exist, separately from what it earned
            entry_mid DOUBLE PRECISION,
            entry_natural DOUBLE PRECISION,
            quote_width_pct DOUBLE PRECISION,
            short_delta DOUBLE PRECISION,
            -- the session
            last_marked_at TIMESTAMPTZ,
            last_value DOUBLE PRECISION,
            last_return_pct DOUBLE PRECISION,
            peak_return_pct DOUBLE PRECISION,
            worst_return_pct DOUBLE PRECISION,
            target_hit_at TIMESTAMPTZ,
            target_return_pct DOUBLE PRECISION,
            -- settled at the close, from spot: a 0DTE spread's value at
            -- expiry is arithmetic, not a quote
            expiry_value DOUBLE PRECISION,
            expiry_return_pct DOUBLE PRECISION,
            notes VARCHAR
        )
    """)
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_dte0_shadow_row "
               "ON dte0_shadow (symbol, trading_day, variant)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_dte0_shadow_day "
               "ON dte0_shadow (trading_day DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dte0_shadow")
