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


def test_bucket_switches_are_tunable_bools():
    for k in ("TRADING_BUCKET_QQQ_0DTE", "TRADING_BUCKET_STOCK_0DTE", "TRADING_BUCKET_STOCK_WEEKLY"):
        assert so.BY_KEY[k].kind == "bool"
        assert so.validate(k, "off") == "false"


def test_rotation_honours_its_bucket_switch():
    src = open(os.path.join(REPO, "scripts", "dte0_trade.py"), encoding="utf-8").read()
    assert 'TRADING_BUCKET_STOCK_WEEKLY" if args.book == "weekly"' in src
    src = open(os.path.join(REPO, "trading_engine", "nodes.py"), encoding="utf-8").read()
    assert 'action = "BUCKET_OFF"' in src


def test_buckets_default_off():
    for k in ("TRADING_BUCKET_QQQ_0DTE", "TRADING_BUCKET_STOCK_0DTE", "TRADING_BUCKET_STOCK_WEEKLY"):
        assert so.BY_KEY[k].default == "false"
    for path, needle in (("trading_engine/nodes.py", 'os.getenv("TRADING_BUCKET_QQQ_0DTE", "false")'),
                         ("scripts/dte0_trade.py", 'os.getenv(bucket_key, "false")')):
        assert needle in open(os.path.join(REPO, path), encoding="utf-8").read()


def test_bucket_budgets_sit_in_the_bucket_group():
    for k in ("TRADING_POSITION_BUDGET", "TRADING_DTE0_MAX_BUDGET", "TRADING_WEEKLY_MAX_BUDGET"):
        assert so.BY_KEY[k].group == so.G_BUCKETS
    assert "TRADING_BUCKET_INDEX_EVENT" not in so.BY_KEY
    src = open(os.path.join(REPO, "scripts", "dte0_trade.py"), encoding="utf-8").read()
    assert 'budget = WEEKLY_MAX_BUDGET if args.book == "weekly" else MAX_BUDGET' in src


ENGINE_STALL_KEYS = ("TRADING_STALL_MINUTES", "TRADING_STALL_GIVEBACK_PCT", "TRADING_STALL_ON_CREDIT",
                     "TRADING_CREDIT_STALL_ARM", "TRADING_CREDIT_STALL_MINUTES",
                     "TRADING_CREDIT_STALL_GIVEBACK_PCT")


def test_engine_stall_knobs_are_tunable_in_their_own_group():
    """Section 228: the QQQ bucket's trades exit on nodes.py's stall, not the orphan one."""
    for k in ENGINE_STALL_KEYS:
        assert so.BY_KEY[k].group == so.G_ENGINE
    assert so.validate("TRADING_STALL_GIVEBACK_PCT", "3.3") == "3.3"
    assert so.validate("TRADING_STALL_ON_CREDIT", "on") == "true"
    assert so.validate("TRADING_CREDIT_STALL_MINUTES", "") == ""   # blank = follow the ride
    for key, bad in [("TRADING_STALL_MINUTES", ""), ("TRADING_STALL_MINUTES", "-1"),
                     ("TRADING_STALL_GIVEBACK_PCT", "500")]:
        with pytest.raises(ValueError):
            so.validate(key, bad)


def test_fresh_process_engine_stall_follows_the_override(tmp_path):
    ov = tmp_path / "ov.env"
    ov.write_text("TRADING_STALL_MINUTES=7\nTRADING_STALL_GIVEBACK_PCT=4.5\n"
                  "TRADING_STALL_ON_CREDIT=true\nTRADING_CREDIT_STALL_ARM=false\n"
                  "TRADING_CREDIT_STALL_MINUTES=\n")
    env = {**os.environ, "TRADING_OVERRIDES_PATH": str(ov), "TRADING_STALL_MINUTES": "5",
           "TRADING_CREDIT_STALL_GIVEBACK_PCT": "9", "PYTHONPATH": REPO}
    out = subprocess.run(
        [sys.executable, "-c",
         "from trading_engine import nodes as n;"
         "print(n.STALL_MINUTES, n.STALL_GIVEBACK_PCT, n.STALL_ON_CREDIT,"
         " n.CREDIT_STALL_REQUIRES_ARM, n.CREDIT_STALL_MINUTES, n.CREDIT_STALL_GIVEBACK_PCT)"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    # blank credit window falls back to the ride's 7; the env's credit give-back still applies
    assert out.stdout.strip().splitlines()[-1] == "7.0 4.5 True False 7.0 9.0"
