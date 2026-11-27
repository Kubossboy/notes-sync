import re

from app.dashboard.plain import explain, sentiment, strategy


def test_examples():
    assert explain("net reward/risk 0.49 < 1.2 (target capped by resistance 359.50)") == (
        "Skipped: after fees, the profit would be only 0.49x the risk (needs 1.2x). "
        "Price is near a ceiling at 359.5, so there is little room to rise.")
    assert explain("net reward/risk -0.58 < 1.2").startswith("Skipped: fees would eat the whole profit.")
    assert "1.19x the risk (needs 1.2x)" in explain("net reward/risk 1.196 < 1.2")
    assert explain("price 2.87 < 5.0") == "Stock is too cheap (under $5)."
    assert explain("avg $vol/bar 1,300,000 < 2,000,000").startswith("Too little trading volume")
    assert explain("momentum_breakout: close not above 48-bar high", tf="15m") == \
        "No breakout: price has not beaten its high of the last 12 hours."
    assert "first 120 min after market open" in explain("opening_range_breakout: 190 min after open > window")
    assert explain("close not above 20-bar high", tf="5m").endswith("last 100 minutes.")


def test_every_known_code_is_translated():
    codes = ["no tested resistance broken on the last bar", "no fresh VERIFIED positive catalyst (>= medium impact)",
             "rvol 0.8 below anomaly threshold", "rvol 0.86 too low", "only 0 of last 6 bars below VWAP",
             "ATR 0.12% below round-trip cost 0.17%", "volume not expanded on last 3 bars",
             "no Bollinger squeeze on previous bar", "trend is down", "trend is range", "no pullback to EMA20",
             "bid/ask imbalance 0.1 below threshold", "no bullish resumption bar above EMA9",
             "ask depth change -5% (no thinning)", "data not tradable (status=delayed, stale=True)", "market closed",
             "no VWAP reclaim on the last bar", "only 3 stored book snapshots (need history)", "below SMA200",
             "price below EMA20/VWAP", "close not above upper band", "bar not bullish", "last bar not bullish",
             "close not above opening-range high", "breakout not fresh (previous bar already above)",
             "close at 40% of bar range", "ROC12 0.1% too low", "EMA 9/20/50 not stacked bullish", "price not rising",
             "ATR not expanding", "RSI 88 outside range", "score 55 < 70"]
    for c in codes:
        out = explain(c)
        assert out != c and not re.search(r"[a-z]+_[a-z]+", out), (c, out)


def test_helpers():
    assert sentiment(0.5) == ("positive", "good") and sentiment(-0.4)[0] == "negative" and sentiment(0.0)[0] == "neutral"
    assert strategy("trend_continuation") == "Trend continuation"
