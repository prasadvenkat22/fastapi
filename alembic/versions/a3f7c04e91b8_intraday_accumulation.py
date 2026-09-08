"""weekly_shadow: intraday accumulation columns

Five hourly-bar readings recorded beside every weekly structure: session VWAP
slope, the share of bars closing above the running VWAP, volume-weighted close
location, session volume against the 20-day average, and the final bar's close
location.

WHAT THEY ARE FOR. Price direction alone does not say whether a session was
bought or drifted up. SNDK on 2026-09-03 and 2026-09-04 both closed higher; the
first was 0.68x ADV with a +0.76% VWAP slope and NEGATIVE accumulation volume,
the second was 1.28x ADV, +2.56% slope, 100% of bars above VWAP and a close on
the high of the final hour. Only the second looks like demand being worked.

WHAT THEY ARE NOT. Not a measure of institutional buying -- every buyer has a
seller and a public OHLCV feed cannot tell them apart. They measure urgency.

Observational, like every other sig_ column: logged beside the decision, never
wired into it.

Revision ID: a3f7c04e91b8
Revises: f6a2b81d3c07
Create Date: 2026-09-08

"""
from alembic import op
import sqlalchemy as sa

revision = "a3f7c04e91b8"
down_revision = "f6a2b81d3c07"
branch_labels = None
depends_on = None

COLUMNS = (
    ("sig_vwap_slope_pct", sa.Float()),
    ("sig_bars_above_vwap", sa.Float()),
    ("sig_ad_volume_ratio", sa.Float()),
    ("sig_volume_vs_adv", sa.Float()),
    ("sig_close_location", sa.Float()),
)


def _weekly_shadow_missing() -> bool:
    """weekly_shadow has no CREATE migration -- it was made by hand on the
    server before the table was tracked. On a fresh database it therefore does
    not exist at this revision, and an unguarded add_column here crash-loops
    the container on first boot. Same guard as the three revisions before it.
    """
    bind = op.get_bind()
    return not sa.inspect(bind).has_table("weekly_shadow")


def upgrade() -> None:
    if _weekly_shadow_missing():
        return
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("weekly_shadow")}
    for name, type_ in COLUMNS:
        if name not in existing:
            op.add_column("weekly_shadow", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    if _weekly_shadow_missing():
        return
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("weekly_shadow")}
    for name, _ in reversed(COLUMNS):
        if name in existing:
            op.drop_column("weekly_shadow", name)
