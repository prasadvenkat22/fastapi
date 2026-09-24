"""Section 232: per-position 'start watching profits now'."""

import os

from trading_engine import orphans

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
KEY = "QQQ260924P00735000|QQQ260924P00740000"


def test_request_is_queued_then_taken_once(tmp_path, monkeypatch):
    monkeypatch.setattr(orphans, "WATCH_NOW_PATH", str(tmp_path / "w.json"))
    assert orphans._take_watch_now(KEY) is None
    req = orphans.request_watch_now(KEY, who="op@x")
    assert req["who"] == "op@x" and KEY in orphans._read_watch_now()
    assert orphans._take_watch_now(KEY)["at"] == req["at"]
    assert orphans._take_watch_now(KEY) is None          # applied once, then gone
    assert orphans._read_watch_now() == {}


def test_a_corrupt_request_file_reads_as_empty(tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    p.write_text("{not json")
    monkeypatch.setattr(orphans, "WATCH_NOW_PATH", str(p))
    assert orphans._read_watch_now() == {}


def test_watched_position_uses_the_sale_price_and_skips_the_arm():
    src = open(os.path.join(REPO, "trading_engine", "orphans.py"), encoding="utf-8").read()
    assert "if STALL_ON_MARK or watched:" in src
    assert "and (watched or stall_arm_reached(s_peak))" in src
    assert "elif stall_ready and (STALL_ON_MARK or watched or not drag_blocks):" in src
    assert "stall_ready = stall_armed and books_a_gain" in src      # never at a loss


def test_endpoint_is_admin_only_and_same_day_only():
    src = open(os.path.join(REPO, "routes", "trading_router.py"), encoding="utf-8").read()
    i = src.index('@router.post("/positions/watch-now")')
    block = src[i:i + 1500]
    assert "Depends(require_admin())" in block
    assert "same-day positions only" in block
    assert "key=st[\"key\"]" in src and "watching_now=" in src
