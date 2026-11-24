"""Offline tests for the news intelligence pieces (synthetic fixtures, not market data)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.intelligence import catalysts as C
from app.intelligence.feeds import clean_text, fetch_feed, parse_date, parse_feed
from app.intelligence.sec import Filing, SecClient, parse_form4
from app.intelligence.sentiment_lexicon import lexicon_sentiment
from app.intelligence.tagging import TickerTagger
from app.intelligence.verify import (content_hash, normalize_title, same_story, similarity, time_quality,
                                     verification_status)
from tests.fakes import FakeResp, SeqSession

FX = Path(__file__).parent / "fixtures"
UTC = timezone.utc
STOCKS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META"]
CRYPTO = ["BTC-USD", "ETH-USD", "SOL-USD"]


# ---------------------------------------------------------------- feeds
def test_parse_rss_items_dates_and_html():
    items = parse_feed((FX / "rss_sample.xml").read_text(), "t")
    assert len(items) == 3
    a, b, c = items
    assert a.title.startswith("Nvidia beats") and a.url == "https://example.test/a"
    assert a.published == datetime(2026, 11, 23, 21, 5, tzinfo=UTC)
    assert b.published == datetime(2026, 11, 24, 3, 0, tzinfo=UTC)      # -0500 converted to UTC
    assert c.published is None                                           # never invented
    assert "<p>" not in a.summary and a.summary == "NVIDIA Corp. reported record quarterly revenue."


def test_parse_atom_and_bom():
    raw = b"\xef\xbb\xbf" + (FX / "atom_sample.xml").read_bytes()        # federalreserve.gov sends a BOM
    items = parse_feed(raw, "t")
    assert len(items) == 1 and items[0].url == "https://status.example.test/x"
    assert items[0].published == datetime(2026, 11, 24, 6, 30, tzinfo=UTC)


def test_parse_invalid_xml_raises():
    with pytest.raises(ValueError):
        parse_feed("<html>blocked</html", "t")


def test_parse_date_variants():
    assert parse_date("2026-11-24T06:30:00Z") == datetime(2026, 11, 24, 6, 30, tzinfo=UTC)
    assert parse_date("Tue, 24 Nov 2026 07:00:00 +0100") == datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
    assert parse_date("garbage") is None and parse_date(None) is None
    assert clean_text("a &amp; <b>b</b>\n c") == "a & b c"


def test_fetch_feed_http_error_and_live_guard():
    with pytest.raises(ConnectionError):
        fetch_feed("https://example.test/feed", "t", SeqSession(FakeResp(403, text="no")), "ua")
    with pytest.raises(Exception):
        fetch_feed("https://api.alpaca.markets/v2/orders", "t", SeqSession(), "ua")   # live endpoint blocked


# ---------------------------------------------------------------- tagging
@pytest.fixture(scope="module")
def tagger():
    return TickerTagger(STOCKS, CRYPTO, {"NVDA": "NVIDIA CORP", "AAPL": "Apple Inc."})


@pytest.mark.parametrize("text,expected", [
    ("Nvidia beats estimates", {"NVDA"}),
    ("$TSLA rips higher", {"TSLA"}),
    ("Apple and Microsoft lead gains", {"AAPL", "MSFT"}),
    ("Bitcoin and Ether slide; Solana holds", {"BTC-USD", "ETH-USD", "SOL-USD"}),
    ("Meta Platforms unveils new glasses", {"META"}),
])
def test_tagging_positive(tagger, text, expected):
    got = {k for k in tagger.tag(text) if k not in ("MACRO", "CRYPTO_MARKET")}
    assert got == expected


@pytest.mark.parametrize("text", [
    "I ate an apple at the meta conference",           # lowercase common words
    "The team will run it on the open sol beach",      # ambiguous tokens
    "Teslas are popular in Norway",                    # no false match on plural brand? allowed only if exact
    "Ethernet adoption grows",                         # 'Ether' inside a word
])
def test_tagging_precision(tagger, text):
    got = {k for k in tagger.tag(text) if k not in ("MACRO", "CRYPTO_MARKET")}
    assert not (got & {"AAPL", "META", "SOL-USD", "ETH-USD", "AMD"}), got


def test_summary_only_mention_is_down_weighted(tagger):
    t = tagger.tag_doc("Coherent authorizes $2B buyback", "Coherent is an optical partner to Nvidia.")
    assert t["NVDA"] < 0.6                       # below min_tag_confidence -> not attributed to NVDA
    assert tagger.tag_doc("Nvidia beats", "")["NVDA"] >= 0.85


def test_macro_and_crypto_market_tags(tagger):
    assert "MACRO" in tagger.tag("Federal Reserve holds interest rates steady")
    assert "CRYPTO_MARKET" in tagger.tag("Stablecoin bill advances")


# ---------------------------------------------------------------- catalysts
@pytest.mark.parametrize("text,cat,direction", [
    ("Nvidia beats estimates on record revenue", "earnings_beat", 1),
    ("Company misses estimates, shares sink", "earnings_miss", -1),
    ("Board approves $10 billion share repurchase", "buyback", 1),
    ("Biotech files for Chapter 11 bankruptcy", "bankruptcy", -1),
    ("Goldman upgrades Tesla to Buy", "analyst_upgrade", 1),
    ("NVDA downgraded to Neutral at Citi", "analyst_downgrade", -1),
    ("FDA approves new drug", "fda_approval", 1),
    ("Fed cuts interest rates by 25 bps", "fed_rate_cut", 1),
    ("Exchange hacked, $200M drained", "exchange_hack", -1),
    ("Coinbase: withdrawals paused for SOL", "exchange_outage", -1),
    ("Pfizer agrees to buy Metsera", "acquisition", 1),
    ("Federal Reserve Board issues enforcement action against a bank", "regulatory_enforcement", -1),
    ("Federal Reserve Board announces termination of enforcement action", "regulatory_enforcement", 0),
])
def test_catalyst_rules(text, cat, direction):
    cats, _ = C.detect(text)
    hit = [c for c in cats if c.category == cat]
    assert hit and hit[0].direction == direction, cats


@pytest.mark.parametrize("text", [
    "Microsoft offered 60,000 employees buyouts",          # not M&A
    "Foundation doubles security team ahead of overhaul",  # not an analyst upgrade
    "Talks are moving at warp speed",                      # not 'war'
    "Grayscale warns of near-term risks",                  # 'warns' is not geopolitical
    "Federal Reserve Board issues enforcement action against a bank",   # not crypto-regulatory
])
def test_catalyst_false_positives(text):
    cats, _ = C.detect(text)
    assert not [c for c in cats if c.category in ("acquisition", "analyst_upgrade", "geopolitical")], cats


def test_catalyst_negation_sets_unknown_direction():
    cats, _ = C.detect("Market rout kills 50 basis points December Fed rate hike bets")
    hike = next(c for c in cats if c.category == "fed_rate_hike")
    assert hike.direction == 0 and hike.matched.startswith("negated")
    assert C.detect("Not only did Apple beat estimates")[0][0].direction == 1   # 'not only' is not a negation


def test_unexpected_and_net_direction():
    cats, unexpected = C.detect("Shock: company unexpectedly cuts guidance")
    assert unexpected and C.net_direction(cats) < 0
    assert C.max_impact(cats) in ("medium", "high")
    assert C.net_direction([]) == 0.0


# ---------------------------------------------------------------- lexicon sentiment
def test_lexicon_sentiment_and_negation():
    pos, _ = lexicon_sentiment("Shares surge after strong record results")
    neg, _ = lexicon_sentiment("Shares plunge after weak results and lawsuit")
    negated, _ = lexicon_sentiment("Results were not strong")
    assert pos > 0.3 and neg < -0.3 and negated < 0
    assert lexicon_sentiment("The meeting is on Tuesday")[0] == 0


# ---------------------------------------------------------------- verification / dedup
def test_dedup_hash_and_story_clustering():
    assert content_hash("s", "Nvidia Beats!", "u") == content_hash("s", "nvidia beats", "u")
    assert normalize_title("  Nvidia  BEATS ") == "nvidia beats"
    t1 = datetime(2026, 11, 23, 21, tzinfo=UTC)
    a = "Nvidia beats estimates as data center revenue surges"
    b = "Nvidia beats revenue estimates as data center sales surge"
    assert similarity(a, b) >= 0.45 and same_story(a, t1, b, t1 + timedelta(hours=2))
    assert not same_story(a, t1, b, t1 + timedelta(hours=40))                    # outside window
    assert not same_story(a, t1, "Tesla recalls 200,000 vehicles", t1)


def test_verification_and_time_quality():
    assert verification_status(1, 1) == (True, "primary_source")
    assert verification_status(3, 2)[0] is True
    assert verification_status(2, 1) == (False, "single_source")
    assert verification_status(4, 5)[0] is False                                 # social never verifies alone
    now = datetime(2026, 11, 24, 8, tzinfo=UTC)
    assert time_quality(now - timedelta(hours=1), now)[0] == 1.0
    assert time_quality(None, now)[0] == 0.6
    assert time_quality(now + timedelta(hours=5), now)[0] == 0.0
    assert time_quality(now - timedelta(days=5), now)[0] == 0.5


# ---------------------------------------------------------------- SEC
def test_parse_form4():
    txs = parse_form4((FX / "form4_sample.xml").read_text())
    assert len(txs) == 2
    s, p = txs
    assert s["owner"] == "Doe Jane" and s["is_officer"] and s["code"] == "S"
    assert s["shares"] == 1000 and s["price"] == 150.25 and s["acq_disp"] == "D" and s["plan_10b5_1"]
    assert p["code"] == "P" and p["date"] == "2026-11-21"


def test_sec_recent_filings(tmp_path):
    tick = {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}}
    sub = {"filings": {"recent": {
        "form": ["8-K", "4", "10-Q"],
        "accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3"],
        "acceptanceDateTime": ["2026-11-23T16:05:00.000Z", "2026-11-20T18:00:00.000Z", "2026-10-01T10:00:00.000Z"],
        "items": ["2.02,9.01", "", ""], "primaryDocument": ["a.htm", "xslF345X05/f4.xml", "q.htm"],
        "primaryDocDescription": ["8-K", "4", "10-Q"]}}}
    sec = SecClient(SeqSession(FakeResp(200, tick), FakeResp(200, sub)), "t@example.test", tmp_path)
    fs = sec.recent_filings("NVDA", datetime(2026, 11, 15, tzinfo=UTC))
    assert [f.form for f in fs] == ["8-K", "4"]                                # older 10-Q cut off
    assert fs[0].items == ["2.02", "9.01"] and "000126" in fs[0].url.replace("-", "")
    assert sec.recent_filings("SPY", datetime(2026, 11, 15, tzinfo=UTC)) == []  # ETF: no filings
    assert sec.company_name("NVDA") == "NVIDIA CORP"
