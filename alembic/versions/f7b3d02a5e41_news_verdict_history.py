"""append-only log of every news verdict, so re-grading cannot rewrite history

WHY THIS EXISTS. news_verdicts is keyed (symbol, trading_day) and upserted, so
the row holds ONE verdict per name per day -- the last one written. That was
correct while news_watch.py ran once at 09:30. It stops being correct the
moment the watcher runs hourly: a re-grade at 15:00 replaces the morning read
with one written after five and a half hours of tape.

THAT IS LOOKAHEAD, AND IT WOULD BE INVISIBLE. Six scripts join a verdict to
the session's realized move -- verdict_outcome, macro_outcome,
news_ev_backtest, xgb_sentiment, novelty_check, sentiment_signal_test. Every
one of them would keep running, keep producing numbers, and quietly start
scoring an afternoon verdict against a move it had already seen. The accuracy
would climb and the climb would mean nothing. The wrong version of this change
does not fail; it flatters.

macro_outcome.py's own docstring already names the bug, from the August macro
gate: "the verdict was overwritten in one row instead of kept". This table is
what keeping it looks like.

news_verdicts keeps its meaning -- the CURRENT verdict, which is what the
entry-time readers want (nodes' NEWS_DIRECTION gate, weekly_pick, dte0_trade's
veto). Measurement reads here instead, and takes the row in force at the
cutoff it cares about.

Revision ID: f7b3d02a5e41
Revises: e2c7a4f19d63
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f7b3d02a5e41"
down_revision: Union[str, None] = "e2c7a4f19d63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS news_verdict_history (
            id UUID PRIMARY KEY,
            symbol VARCHAR NOT NULL,
            trading_day DATE NOT NULL,
            asof TIMESTAMPTZ NOT NULL,
            verdict VARCHAR NOT NULL,
            confidence DOUBLE PRECISION,
            rationale VARCHAR,
            headline_count INTEGER,
            headline_digest VARCHAR,
            suggested_structure VARCHAR,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    # One row per distinct grade. A re-run in the same minute with the same
    # headline set must not double-log -- the watcher already skips the model
    # in that case, and this makes the write idempotent too.
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_news_verdict_hist "
               "ON news_verdict_history (symbol, trading_day, asof)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_news_verdict_hist_lookup "
               "ON news_verdict_history (symbol, trading_day, asof DESC)")

    # When the current verdict was last graded. Without it a row read at 14:00
    # cannot say whether it is the morning read or a fresh one.
    op.execute("ALTER TABLE news_verdicts "
               "ADD COLUMN IF NOT EXISTS asof TIMESTAMPTZ")

    # SEED FROM WHAT IS ALREADY THERE, so the measurement scripts read the
    # same sample tomorrow that they read today. Switching them to this table
    # without a backfill would silently restart every one of them at zero rows.
    #
    # asof IS NOT created_at, AND THE DIFFERENCE IS 82% OF THE SAMPLE.
    # created_at is when the ROW WAS WRITTEN. Only 37 of the 204 rows here
    # came from the live 09:30 watcher; the other 167 were written by
    # sentiment_signal_test.py --backfill, which grades a past session at
    # whatever wall-clock time the backfill happened to run. A row describing
    # 2026-08-20 carries a created_at of 2026-09-08, so an `asof <= the open`
    # cutoff would discard it -- and every script would quietly report on a
    # fifth of its data.
    #
    # The honest stamp is the SESSION'S OPEN. Every one of these rows, live or
    # backfilled, was graded from session_headlines(sym, day) -- a window that
    # ENDS at the open. So each is an open-of-day verdict whatever hour the
    # model ran. LEAST keeps the live rows at their true 09:30 and clamps the
    # backfilled ones to the open they actually describe.
    op.execute("""
        INSERT INTO news_verdict_history
            (id, symbol, trading_day, asof, verdict, confidence, rationale,
             headline_count, headline_digest, suggested_structure)
        SELECT id, symbol, trading_day,
               LEAST(COALESCE(created_at, 'infinity'::timestamptz),
                     (trading_day + TIME '09:45') AT TIME ZONE 'America/New_York'),
               verdict, confidence, rationale, headline_count, headline_digest,
               suggested_structure
        FROM news_verdicts
        ON CONFLICT DO NOTHING
    """)
    op.execute("UPDATE news_verdicts SET asof = COALESCE(asof, updated_at, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS news_verdict_history")
    op.execute("ALTER TABLE news_verdicts DROP COLUMN IF EXISTS asof")
