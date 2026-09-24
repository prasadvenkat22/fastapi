"""Section 229: the same-day stall starts watching at TRADING_ORPHAN_STALL_ARM."""

import os
import subprocess
import sys

import pytest

from trading_engine import orphans
from trading_engine import settings_overrides as so

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_arm_zero_keeps_the_old_any_gain_rule(monkeypatch):
    monkeypatch.setattr(orphans, "ORPHAN_STALL_ARM_PCT", 0.0)
    assert orphans.stall_arm_reached(0.1)
    assert not orphans.stall_arm_reached(0.0)
    assert not orphans.stall_arm_reached(-5.0)
    assert not orphans.stall_arm_reached(None)


def test_arm_waits_for_the_peak_to_reach_the_level(monkeypatch):
    monkeypatch.setattr(orphans, "ORPHAN_STALL_ARM_PCT", 30.0)
    assert not orphans.stall_arm_reached(29.9)
    assert orphans.stall_arm_reached(30.0)
    assert orphans.stall_arm_reached(40.0)   # a higher local max stays armed


def test_ladder_and_positions_page_use_the_arm():
    src = open(os.path.join(REPO, "trading_engine", "orphans.py"), encoding="utf-8").read()
    assert 'and (watched or stall_arm_reached(s_peak))' in src
    assert 'rec["peak"] > 0\n' not in src
    src = open(os.path.join(REPO, "routes", "trading_router.py"), encoding="utf-8").read()
    assert 'armed = bool(rec.get("watch_now")) or orphans.stall_arm_reached(peak)' in src


def test_arm_is_a_ui_setting():
    s = so.BY_KEY["TRADING_ORPHAN_STALL_ARM"]
    assert (s.group, s.kind, s.default) == (so.G_0DTE, "float", "0")
    assert so.validate("TRADING_ORPHAN_STALL_ARM", "30") == "30"


def test_fresh_process_reads_the_override(tmp_path):
    ov = tmp_path / "ov.env"
    ov.write_text("TRADING_ORPHAN_STALL_ARM=30\nTRADING_ORPHAN_STALL_MINUTES=5\n")
    env = {**os.environ, "TRADING_OVERRIDES_PATH": str(ov), "PYTHONPATH": REPO}
    out = subprocess.run(
        [sys.executable, "-c",
         "from trading_engine import orphans as o;"
         "print(o.ORPHAN_STALL_ARM_PCT, o.STALL_MINUTES, o.stall_arm_reached(25), o.stall_arm_reached(31))"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "30.0 5.0 False True"


# --- Section 231: the stall on the sale price -------------------------------

def test_mark_peak_is_a_return_on_the_current_entry():
    assert orphans.mark_peak({"mpeak_v": 4.60}, 3.56, credit=False) == pytest.approx(29.21, abs=0.01)
    assert orphans.mark_peak({"mpeak_v": 0.40}, 1.00, credit=True) == pytest.approx(60.0)
    assert orphans.mark_peak({}, 3.56, credit=False) is None


def test_sale_price_mode_is_a_ui_switch_default_off():
    s = so.BY_KEY["TRADING_ORPHAN_STALL_ON_MARK"]
    assert (s.group, s.kind, s.default) == (so.G_0DTE, "bool", "false")
    assert so.validate("TRADING_ORPHAN_STALL_ON_MARK", "on") == "true"


def test_sale_price_mode_wiring():
    src = open(os.path.join(REPO, "trading_engine", "orphans.py"), encoding="utf-8").read()
    # the stall reads the sale-price series when the switch is on ...
    assert "s_peak, s_now, s_quiet = mpeak, _gain_pct, mquiet" in src
    # ... the drag guard does not hold that exit back ...
    assert "elif stall_ready and (STALL_ON_MARK or watched or not drag_blocks):" in src
    # ... and it still never sells below the minimum gain
    assert "stall_ready = stall_armed and books_a_gain" in src
    src = open(os.path.join(REPO, "routes", "trading_router.py"), encoding="utf-8").read()
    assert "peak = orphans.mark_peak(rec, abs(st[\"entry\"]), st[\"credit\"])" in src


def test_fresh_process_reads_the_sale_price_switch(tmp_path):
    ov = tmp_path / "ov.env"
    ov.write_text("TRADING_ORPHAN_STALL_ON_MARK=true\n")
    env = {**os.environ, "TRADING_OVERRIDES_PATH": str(ov), "PYTHONPATH": REPO}
    out = subprocess.run([sys.executable, "-c", "from trading_engine import orphans as o; print(o.STALL_ON_MARK)"],
                         cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "True"
