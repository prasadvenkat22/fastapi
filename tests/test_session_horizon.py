"""Section 254: the screener's horizon is fractional -- what is left of today's
session plus the full sessions after it -- for both the 0DTE and weekly books."""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import weekly_pick as wp  # noqa: E402

NY = ZoneInfo("America/New_York")


def _at(h, m, day="2026-10-05"):            # a Monday
    y, mo, d = (int(x) for x in day.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=NY)


def test_same_day_entry_gets_only_what_is_left():
    assert abs(wp.session_horizon("2026-10-05", _at(11, 30)) - 270 / 390) < 1e-9
    assert abs(wp.session_horizon("2026-10-05", _at(13, 0)) - 180 / 390) < 1e-9


def test_weekly_entry_counts_the_rest_of_today():
    # Monday 09:50 to Friday: 370/390 of today + Tue..Fri.
    assert abs(wp.session_horizon("2026-10-09", _at(9, 50)) - (4 + 370 / 390)) < 1e-9


def test_before_the_open_counts_a_full_day():
    assert abs(wp.session_horizon("2026-10-05", _at(8, 0)) - 1.0) < 1e-9


def test_after_the_close_floor():
    assert wp.session_horizon("2026-10-05", _at(16, 30)) > 0


def test_monte_carlo_scales_with_a_fractional_horizon():
    full = wp.monte_carlo_terminal(100.0, 1.596, 1.0)
    half = wp.monte_carlo_terminal(100.0, 1.596, 0.25)
    sd = lambda x: float(np.std(np.log(x / 100.0)))
    assert abs(sd(half) / sd(full) - 0.5) < 0.03


def test_atm_iv_interpolates_at_the_money_not_the_wings():
    ivs = {80: 0.60, 95: 0.32, 100: 0.30, 105: 0.29, 120: 0.45}
    assert abs(wp.atm_iv_at(102.5, ivs) - 0.295) < 1e-9
    assert wp.atm_iv_at(100, ivs) == 0.30
    assert wp.atm_iv_at(130, ivs) == 0.45          # beyond the quotes: nearest
