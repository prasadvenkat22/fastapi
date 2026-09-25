"""Section 240: the trading schedule parsed from the host crontab."""

from datetime import date

from trading_engine import schedule

CRON = """
* * * * * cd /opt/fastapi && docker compose exec -T app python scripts/run_cycle.py >> /var/log/q.log 2>&1
*/15 13-18 * * 1-5 cd /opt/fastapi && docker compose exec -T app python scripts/dte0_trade.py --rotate --live --budget 5000 --max-trades 4 >> /var/log/dte0-trade.log 2>&1
50 13,17 * * 1-3 cd /opt/fastapi && docker compose exec -T app python scripts/dte0_trade.py --book weekly --rotate --live --budget 5000 --max-trades 3 --expiry friday --symbols MU,NVDA >> /var/log/weekly-trade.log 2>&1
50 13,17 * * 4-5 cd /opt/fastapi && docker compose exec -T app python scripts/dte0_trade.py --book weekly --rotate --live --budget 5000 --max-trades 3 --expiry +5 --symbols MU,NVDA >> /var/log/weekly-trade.log 2>&1
# 50 13 * * 1 ... dte0_trade.py --book weekly (commented out)
"""


def test_parses_the_three_entry_schedules_in_et():
    jobs = schedule.parse(CRON, on=date(2026, 9, 25))          # EDT, UTC-4
    assert [j["book"] for j in jobs] == ["dte0", "weekly", "weekly"]
    d0, w1, w2 = jobs
    assert d0["days"] == ["Mon", "Tue", "Wed", "Thu", "Fri"] and d0["every_minutes"] == 15
    assert d0["times_et"][0] == "09:00" and d0["times_et"][-1] == "14:45"
    assert w1["days"] == ["Mon", "Tue", "Wed"] and w1["times_et"] == ["09:50", "13:50"]
    assert w1["expiry"].startswith("this Friday")
    assert w2["days"] == ["Thu", "Fri"] and w2["expiry"] == "first Friday at least 5 days out"
    assert w2["symbols"] == ["MU", "NVDA"] and w2["max_trades"] == 3 and w2["live_flag"]


def test_times_move_when_dst_ends():
    jobs = schedule.parse(CRON, on=date(2026, 11, 2))          # EST, UTC-5
    assert jobs[1]["times_et"] == ["08:50", "12:50"]


def test_missing_snapshot_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "CRONTAB_SNAPSHOT", str(tmp_path / "nope"))
    out = schedule.snapshot()
    assert out["jobs"] == [] and "not installed" in out["note"]
