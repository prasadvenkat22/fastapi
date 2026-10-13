"""Section 273: the decision log keeps the lines that explain a trade and drops the noise."""
import logging

from trading_engine import decisions as D

L = "2026-10-13 14:19:08,503 INFO "


def _ev(msg, level="INFO"):
    return D.event_from_line(f"2026-10-13 14:19:08,503 {level} {msg}")


def test_keeps_the_reasons_seen_on_10_13():
    cases = {
        "Band touch: macro gate: 10Y up +3.6bp (> 2bp) -- no call.": "refused",
        "Band touch read: QQQ 741.99, 1-min band 739.1 / 740.5 / 741.9 — UPPER touch -> put spread.": "touch",
        "Sizing: BAND_TOUCH sized to 0 contracts at $0.90 — entry 20% of $266 equity": "sizing",
        "Buying power: $7.61 pays for 0 contract(s) at $67 each — sizing 0, not 1.": "sizing",
        "Entering BEAR via BAND_TOUCH:TOUCH: 1 contracts, long 751.0 / short 749.0 at $0.54": "entry",
        "ACCOUNT FLOOR BREACHED: equity 532.79 is at or below 4500.00 — flattening": "floor",
        "ORPHAN ACCOUNT_FLOOR: closing MU 950/940 x1 — {'id': 1}": "exit",
        "ORPHAN booked to history: MU 950/940 x1 -20.00 (ACCOUNT_FLOOR)": "booked",
        "Engine profit lock: BEAR_PUT_SPREAD peaked +31.5% (lock arms at +30%)": "exit",
        "Band touch: QQQ 750.21 is back at the 20-SMA 750.19 — booking BULL_CALL_SPREAD at +27.0%.": "exit",
        "QQQ is -21.8%, past the -20.0% stop, but only for 0.0 of the 2 minutes": "stop_wait",
    }
    for msg, kind in cases.items():
        ev = _ev(msg)
        assert ev and ev["kind"] == kind, (msg, ev)


def test_drops_noise():
    for msg in ("Band touch read: QQQ 750.0, 1-min band 1 / 2 / 3 — inside the band.",
                "HTTP Request: GET https://api.tradier.com/v1/accounts/1/orders",
                "Macro BAD — refused by: breadth",
                "Holding BEAR_PUT_SPREAD at +1.8% (stop -20%, take-profit +50%)",
                "cycle ok — action=HOLD playbook=- exit=- macro=BAD"):
        assert _ev(msg) is None, msg
    assert D.event_from_line("not a log line") is None


def test_floor_repeats_collapse_but_refusals_do_not():
    D._last.clear()
    floor = _ev("ACCOUNT FLOOR BREACHED: equity 532.79 is at or below 4500.00")
    again = dict(floor, ts="2026-10-13T14:20:08Z")
    assert D._keep(floor) and not D._keep(again)
    ref = _ev("Band touch: trend check: the 20-SMA rose $1.10 -- no put.")
    assert D._keep(ref) and D._keep(dict(ref, ts="2026-10-13T14:20:08Z"))


def test_handler_writes_and_read_day_uses_new_york_date(tmp_path, monkeypatch):
    path = tmp_path / "d.jsonl"
    monkeypatch.setattr(D, "PATH", str(path))
    D._last.clear()
    h = D.DecisionHandler()
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "Entering BULL via BAND_TOUCH:TOUCH: 1", None, None)
    h.emit(rec)
    h.emit(logging.LogRecord("x", logging.INFO, "f", 1, "HTTP Request: GET", None, None))
    D.append({"ts": "2026-10-14T02:00:00Z", "kind": "entry", "level": "INFO", "text": "late"}, str(path))
    day = rec.created
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.fromtimestamp(day, ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    assert [e["text"] for e in D.read_day(today, str(path))][0].startswith("Entering")
    # 02:00 UTC on 10-14 is 22:00 ET on 10-13
    assert any(e["text"] == "late" for e in D.read_day("2026-10-13", str(path)))
    assert not any(e["text"] == "late" for e in D.read_day("2026-10-14", str(path)))


def test_backfill_is_idempotent(tmp_path):
    src, dst = tmp_path / "log", tmp_path / "out"
    src.write_text(L + "Band touch: macro gate: 10Y up +3.6bp -- no call.\n" + L + "HTTP Request: x\n",
                   encoding="utf-8")
    assert D.backfill(str(src), str(dst)) == 1
    assert D.backfill(str(src), str(dst)) == 1
    assert len(dst.read_text().splitlines()) == 1


def test_settings_changes_for_a_new_york_day(tmp_path, monkeypatch):
    from routes import trading_router as R
    log = tmp_path / "audit.log"
    log.write_text("2026-10-13T02:06:36Z\tme@x.com\tTRADING_ACCOUNT_FLOOR\t400 -> 4500\n"
                   "2026-10-13T14:00:00Z\tcli:root\tTRADING_STOP_CONFIRM_TOTAL\t(unset) -> true\n", encoding="utf-8")
    monkeypatch.setattr(R.settings_overrides, "AUDIT_PATH", str(log))
    # 02:06 UTC on 10-13 is 22:06 ET on 10-12
    assert [c["key"] for c in R._settings_changes("2026-10-12")] == ["TRADING_ACCOUNT_FLOOR"]
    ch = R._settings_changes("2026-10-13")
    assert ch == [{"et": "10:00:00", "who": "cli:root", "key": "TRADING_STOP_CONFIRM_TOTAL",
                   "change": "(unset) -> true"}]
