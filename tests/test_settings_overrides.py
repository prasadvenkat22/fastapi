"""trading_overrides.env: whitelist, validation, precedence, and that the file
actually reaches the module constants a fresh cron process reads."""

import os
import subprocess
import sys

import pytest

from trading_engine import settings_overrides as so

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def paths(tmp_path, monkeypatch):
    ov, log = tmp_path / "ov.env", tmp_path / "ov.log"
    monkeypatch.setattr(so, "OVERRIDES_PATH", str(ov))
    monkeypatch.setattr(so, "AUDIT_PATH", str(log))
    return ov, log


def test_validate_bounds_and_types():
    assert so.validate("TRADING_ORPHAN_STOP_PCT", " -30 ") == "-30"
    assert so.validate("TRADING_ORPHAN_OTM_STOP", "OFF") == "false"
    assert so.validate("TRADING_ORPHAN_HOLD_UNTIL", "") == ""
    assert so.validate("TRADING_DTE0_MAX_ROTATIONS", "4") == "4"
    for key, bad in [("TRADING_ORPHAN_STOP_PCT", "25"),        # a positive stop
                     ("TRADING_ORPHAN_STOP_PCT", "abc"),
                     ("TRADING_ORPHAN_STOP_PCT", "nan"),
                     ("TRADING_ORPHAN_FORCE_CLOSE", ""),         # blank not allowed here
                     ("TRADING_ORPHAN_FORCE_CLOSE", "3:45pm"),
                     ("TRADING_DTE0_MAX_ROTATIONS", "2.5")]:
        with pytest.raises(ValueError):
            so.validate(key, bad)


def test_non_whitelisted_keys_are_refused_on_write_and_ignored_on_read(paths):
    ov, _ = paths
    with pytest.raises(ValueError):
        so.set_many({"TRADIER_ACCESS_TOKEN": "x"}, who="t")
    with pytest.raises(ValueError):
        so.set_many({"TRADING_DTE0_LIVE": "true"}, who="t")
    ov.write_text("DATABASE_URL=postgres://evil\nTRADING_ORPHAN_STOP_PCT=-40\n"
                  "TRADING_ORPHAN_STALL_MINUTES=lots\n")
    got, problems = so.read_file()
    assert got == {"TRADING_ORPHAN_STOP_PCT": "-40"}
    assert len(problems) == 2


def test_set_is_all_or_nothing_and_audited(paths):
    ov, log = paths
    so.set_many({"TRADING_ORPHAN_STOP_PCT": -30}, who="alice")
    with pytest.raises(ValueError):
        so.set_many({"TRADING_ORPHAN_STALL_MINUTES": 20, "TRADING_ORPHAN_STOP_PCT": 5}, who="alice")
    assert so.read_file()[0] == {"TRADING_ORPHAN_STOP_PCT": "-30"}
    so.unset(["TRADING_ORPHAN_STOP_PCT"], who="bob")
    assert so.read_file()[0] == {}
    lines = log.read_text().splitlines()
    assert "alice\tTRADING_ORPHAN_STOP_PCT\t(unset) -> -30" in lines[0]
    assert "bob\tTRADING_ORPHAN_STOP_PCT\t-30 -> (unset)" in lines[1]


def test_snapshot_precedence(paths, monkeypatch):
    monkeypatch.setattr(so, "_BASE_ENV", {"TRADING_ORPHAN_STOP_PCT": "-35",
                                          "TRADING_ORPHAN_STALL_MINUTES": "25"})
    so.set_many({"TRADING_ORPHAN_STALL_MINUTES": 20}, who="t")
    rows = {r["key"]: r for r in so.snapshot()["settings"]}
    assert (rows["TRADING_ORPHAN_STOP_PCT"]["effective"], rows["TRADING_ORPHAN_STOP_PCT"]["source"]) == ("-35", "env")
    assert (rows["TRADING_ORPHAN_STALL_MINUTES"]["effective"], rows["TRADING_ORPHAN_STALL_MINUTES"]["source"]) == ("20", "override")
    assert rows["TRADING_ORPHAN_CEILING"]["source"] == "default"


def test_fresh_process_trades_on_the_override(tmp_path):
    """The whole point: a new process (what cron starts) sees the file in orphans' constants."""
    ov = tmp_path / "ov.env"
    ov.write_text("TRADING_ORPHAN_STOP_PCT=-41\nTRADING_ORPHAN_LATER_STALL_MINUTES=33\n")
    env = {**os.environ, "TRADING_OVERRIDES_PATH": str(ov),
           "TRADING_ORPHAN_STOP_PCT": "-25", "PYTHONPATH": REPO}
    out = subprocess.run(
        [sys.executable, "-c",
         "from trading_engine import orphans as o;"
         "print(o.ORPHAN_STOP_PCT, o.ORPHAN_LATER_STALL_MINUTES)"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "-41.0 33.0"
