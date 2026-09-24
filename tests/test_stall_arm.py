"""Section 229: the same-day stall starts watching at TRADING_ORPHAN_STALL_ARM."""

import os
import subprocess
import sys

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
    assert 'and stall_arm_reached(rec["peak"])' in src
    assert 'rec["peak"] > 0\n' not in src
    src = open(os.path.join(REPO, "routes", "trading_router.py"), encoding="utf-8").read()
    assert "armed = orphans.stall_arm_reached(peak)" in src


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
