"""add entry tranche plan to open positions

Revision ID: a4e8c17b92f5
Revises: c8a2f6d40b71
Create Date: 2026-08-25

Time-sliced entry. The engine has always opened a position in one order; this
lets it open a planned fraction and add the rest on a clock.

Measured on the afternoon credit spread over 60 sessions, chain-priced, same
total size and the same exits in every arm -- only the timing of the fills
differs:

    all at once                  58% win   -9.00/tr
    2 slices: 0, +10 min         50% win  -10.34/tr
    3 slices: 0, +10, +20 min    51% win   -6.15/tr
    3 slices: 0, +15, +30 min    52% win   -3.22/tr

and it improved every short-delta bucket it was tried in, which is what
separates it from a single lucky cell:

    0.10 delta   -12.37 -> -10.25      0.30 delta   -9.14 -> -1.55
    0.20 delta   -11.05 ->  -6.73      0.40 delta   -6.28 -> +3.53

The opposite result holds for the morning DEBIT spread, where every slicing
schedule measured worse than one fill (-6.94 against -13.39 to -20.35). A
debit spread is a momentum trade where the trigger IS the edge, so delay
costs the move; a credit spread sells time value, so spreading the fills
averages the premium instead of betting on one moment's quote. Hence a knob
rather than a behaviour change: TRADING_ENTRY_SLICES stays at 1 by default.

Two columns rather than one. entry_tranche_qty is the size of each slice,
fixed at entry so later tranches cannot drift as equity moves;
entry_slices_remaining counts down. Deriving either from `quantity` alone
would break the moment a scale-in or partial fill changed it.

Both NULLABLE with no backfill: an existing open position simply has no
tranche plan, which is exactly what "opened in one order" means, and the
read path treats NULL as "nothing further to add".

GUARDED WITH IF NOT EXISTS, like every other migration here. The container
starts with "alembic upgrade head && uvicorn", so a migration that raises on
an already-applied change does not fail loudly in isolation -- it takes the
API down with it.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a4e8c17b92f5"
down_revision: Union[str, None] = "c8a2f6d40b71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trading_open_positions "
        "ADD COLUMN IF NOT EXISTS entry_tranche_qty INTEGER"
    )
    op.execute(
        "ALTER TABLE trading_open_positions "
        "ADD COLUMN IF NOT EXISTS entry_slices_remaining INTEGER"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE trading_open_positions DROP COLUMN IF EXISTS entry_slices_remaining"
    )
    op.execute(
        "ALTER TABLE trading_open_positions DROP COLUMN IF EXISTS entry_tranche_qty"
    )
