"""Index-event detection on the headlines that actually crossed the wires. Section 197."""
from datetime import date

from trading_engine import index_events as ie


def test_sandisk_join_headline():
    assert ie.detect("Sandisk Set to Join S&P 100") == [("SNDK", "S&P 100", "join")]


def test_spdji_release_format_names_before_verb_only():
    # The release format that carried SanDisk on 09-04: three names we do not
    # trade before the verb, and nothing of ours after it.
    t = ("Bloom Energy, Illumina, and Everpure Set to Join S&P 500; Others to Join S&P 100, "
         "S&P MidCap 400, and S&P SmallCap 600 - PR Newswire")
    assert ie.detect(t) == []


def test_parenthetical_names_after_the_verb_do_not_match():
    t = ("This Little-Known AI Storage Stock Will Join the S&P 500 (Not Micron or Sandisk). "
         "History Says This Will Happen Next.")
    assert ie.detect(t) == []


def test_inclusion_phrasing_and_leave():
    t = "Nike's exit from S&P 100 with SanDisk's inclusion is a major index re-rating event"
    found = ie.detect(t)
    assert ("SNDK", "S&P 100", "join") in found
    assert ie.detect("Intel to leave the Nasdaq-100 in December rebalance") == [("INTC", "Nasdaq-100", "leave")]


def test_promotion_wording_does_not_fire_on_a_price_story():
    # "S&P 100 Promotion Extends Its AI Run" is a story about the move, not the
    # announcement; no join verb with the index as object.
    assert ie.detect("Sandisk Jumps Nearly 3.5% as S&P 100 Promotion Extends Its AI Run") == []


def test_google_news_source_suffix_is_stripped():
    assert ie.detect("Micron Set to Join S&P 100 - Bloomberg.com") == [("MU", "S&P 100", "join")]


def test_effective_date_quarterly_and_ad_hoc():
    eff, basis = ie.effective_date(date(2026, 9, 4))
    assert eff == date(2026, 9, 18) and basis.startswith("quarterly")
    eff, basis = ie.effective_date(date(2026, 9, 17))       # one day before the third Friday: too late
    assert basis.startswith("ad hoc") and eff.weekday() < 5
    eff, basis = ie.effective_date(date(2026, 10, 7))       # not a rebalance month
    assert eff == date(2026, 10, 14) and basis.startswith("ad hoc")
    eff, _ = ie.effective_date(date(2026, 10, 9))           # +7 lands on Friday 16th
    assert eff == date(2026, 10, 16)


def test_scan_builds_rows():
    rows = [("g1", "gnews-index", "Sandisk Set to Join S&P 100", date(2026, 9, 4)),
            ("g2", "benzinga", "Toncoin (TON) Price Prediction 2025", date(2026, 9, 19))]
    ev = ie.scan(rows)
    assert len(ev) == 1 and ev[0]["symbol"] == "SNDK" and ev[0]["effective_date"] == date(2026, 9, 18)
