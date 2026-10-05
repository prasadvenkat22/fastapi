"""Section 261: the settings page's book cards are identical across books."""

from trading_engine import settings_overrides as so

CORE = ["On / off", "Budget", "Entries per day", "Bollinger band",
        "Stop loss", "Stop confirmation", "Take profit"]

DELETED = ("TRADING_BAND_TOUCH", "TRADING_BAND_ONLY", "TRADING_MORNING_TIERS",
           "TRADING_MORNING_PUT_TIERS", "TRADING_CLEAN_ENTRIES", "TRADING_ZONE_ENTRIES",
           "TRADING_REJECT_ENTRIES", "TRADING_RELAXED_ENTRIES", "TRADING_FADE_ENTRIES",
           "TRADING_STALL_MINUTES", "TRADING_STALL_GIVEBACK_PCT", "TRADING_STALL_ARM_PCT",
           "TRADING_STALL_ALL_WINDOWS", "TRADING_MORNING_PUT_CLOSE_BY", "TRADING_STALL_ON_CREDIT",
           "TRADING_CREDIT_STALL_ARM", "TRADING_CREDIT_STALL_MINUTES",
           "TRADING_CREDIT_STALL_GIVEBACK_PCT", "TRADING_ORPHAN_TAKE_PROFIT")


def _card(book):
    return sorted((s for s in so.REGISTRY if s.book == book), key=lambda s: s.order)


def test_every_book_has_the_same_core_rows_in_the_same_order():
    for book in ("qqq", "s0", "w3", "w7"):
        core = [s.label for s in _card(book) if s.order % 10 == 0]
        assert core == CORE, book


def test_global_card():
    assert [s.label for s in _card("global")] == [
        "Work orders from the mid", "Account floor", "Daily loss limit", "Force close"]


def test_orders_are_unique_within_a_card():
    for book in ("qqq", "s0", "w3", "w7", "global"):
        orders = [s.order for s in _card(book)]
        assert len(orders) == len(set(orders)) and all(o > 0 for o in orders)


def test_no_deleted_key_remains():
    for k in DELETED:
        assert k not in so.BY_KEY, k
    assert "tiers" not in {s.kind for s in so.REGISTRY}


def test_every_card_row_validates_its_default():
    for s in so.REGISTRY:
        if s.book:
            assert so.validate(s.key, s.default) == s.default or s.default == "", s.key


def test_stock_bollinger_rows_are_the_gate_switches():
    for book, key in (("s0", "TRADING_BOLLINGER_GATE_DTE0"), ("w3", "TRADING_BOLLINGER_GATE_W3"),
                      ("w7", "TRADING_BOLLINGER_GATE_W7")):
        s = so.BY_KEY[key]
        assert (s.book, s.order, s.kind, s.default) == (book, 40, "bool", "false")
