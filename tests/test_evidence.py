from datetime import date

from staledetect.evidence import freshness_indicator, parse_date, quote_in_source, recency_score

SRC = "Python 3.15.0 landed on October 9, 2026, closing out a release cycle. Former X CEO Linda Yaccarino has been appointed an independent director at Better Home & Finance."


def test_exact_quote_found():
    assert quote_in_source("Python 3.15.0 landed on October 9, 2026", SRC)


def test_whitespace_and_case_and_smart_quotes_ignored():
    assert quote_in_source("python   3.15.0 LANDED on October 9,\n2026", SRC)


def test_paraphrase_rejected():
    assert not quote_in_source("Python 3.15 was released in October 2026", SRC)


def test_ellipsis_quote_needs_every_piece():
    assert quote_in_source("Former X CEO Linda Yaccarino ... independent director at Better Home", SRC)
    assert not quote_in_source("Former X CEO Linda Yaccarino ... now runs a bakery in Paris", SRC)


def test_short_quote_rejected():
    assert not quote_in_source("2026", SRC)


def test_parse_dates():
    assert parse_date("Fri, 25 Sep 2026 00:00:00 GMT") == date(2026, 9, 25)
    assert parse_date("2026-10-12") == date(2026, 10, 12)
    assert parse_date(None) is None
    assert parse_date("sometime") is None


def test_recency_buckets():
    today = date(2026, 11, 2)
    assert recency_score(date(2026, 10, 1), today) == 1.0
    assert recency_score(date(2023, 1, 1), today) == 0.2
    assert recency_score(None, today) == 0.4


def test_indicator_needs_verified_quotes():
    today = date(2026, 11, 2)
    no_quotes = freshness_indicator("supported", 0.95, 0, 0, None, today)
    strong = freshness_indicator("supported", 0.95, 3, 3, date(2026, 10, 20), today)
    assert no_quotes["confidence"] == 0.0
    assert strong["confidence_level"] == "high"
    assert freshness_indicator("unverifiable", 0.9, 0, 0, None, today)["label"] == "Could not verify"


def test_date_in_text():
    from staledetect.evidence import date_in_text
    assert date_in_text("As of January 2025, Linda Yaccarino is the CEO") == date(2025, 1, 1)
    assert date_in_text("Python 3.15.0 - Oct. 9, 2026") == date(2026, 10, 9)
    assert date_in_text("As of 2026-03-27, Node.js 24 is in Active LTS") == date(2026, 3, 27)
    assert date_in_text("no date here") is None


def test_future_schedule_dates_ignored():
    from staledetect.evidence import date_in_text
    q = "Node.js 26 moved to LTS on October 28, 2026 and is supported until April 30, 2029."
    assert date_in_text(q, not_after=date(2026, 11, 2)) == date(2026, 10, 28)
