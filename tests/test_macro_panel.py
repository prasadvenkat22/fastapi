"""Macro panel pieces: the data-release list and the /trading/macro route."""

import json

from trading_engine import macro_calendar as mc


def test_releases_on_reads_only_that_day(tmp_path, monkeypatch):
    p = tmp_path / "rel.json"
    p.write_text(json.dumps({"releases": [
        {"date": "2026-09-23", "time": "10:00", "name": "B"},
        {"date": "2026-09-23", "time": "09:45", "name": "A"},
        {"date": "2026-09-24", "time": "08:30", "name": "C"}]}))
    monkeypatch.setattr(mc, "RELEASES_PATH", str(p))
    from datetime import date
    assert [r["name"] for r in mc.releases_on(date(2026, 9, 23))] == ["A", "B"]
    monkeypatch.setattr(mc, "RELEASES_PATH", str(tmp_path / "missing.json"))
    assert mc.releases_on(date(2026, 9, 23)) == []


def test_repo_release_file_is_valid():
    from datetime import date
    rows = mc.releases_on(date(2026, 9, 23))
    assert rows and "PMI" in rows[0]["name"]


def test_macro_route_needs_auth(client):
    assert client.get("/trading/macro").status_code == 401
