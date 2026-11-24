"""Registry of free public news sources with reliability tiers.

tier 1 = primary/official source (regulator, central bank, exchange status page,
         company filing)                          -> base confidence 0.95
tier 2 = established financial media / wires      -> 0.70
tier 3 = crypto/trade media, aggregators          -> 0.60
tier 4 = social media (X)                         -> 0.30 (never verified alone)
"""
from __future__ import annotations

from dataclasses import dataclass

TIER_CONFIDENCE = {1: 0.95, 2: 0.70, 3: 0.60, 4: 0.30}


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    tier: int
    scope: str                 # stock | crypto | macro | mixed
    default_category: str | None = None
    per_ticker: bool = False   # url contains {tickers}


FEEDS: list[FeedSource] = [
    FeedSource("yahoo_finance_ticker_rss", "https://feeds.finance.yahoo.com/rss/2.0/headline?s={tickers}&region=US&lang=en-US",
               2, "stock", per_ticker=True),
    FeedSource("marketwatch_top", "https://feeds.content.dowjones.io/public/rss/mw_topstories", 2, "mixed"),
    FeedSource("marketwatch_pulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse", 2, "mixed"),
    FeedSource("prnewswire", "https://www.prnewswire.com/rss/news-releases-list.rss", 2, "stock", "company_announcement"),
    FeedSource("fed_monetary", "https://www.federalreserve.gov/feeds/press_monetary.xml", 1, "macro", "fed"),
    FeedSource("fed_all", "https://www.federalreserve.gov/feeds/press_all.xml", 1, "macro", "fed"),
    FeedSource("bls_latest", "https://www.bls.gov/feed/bls_latest.rss", 1, "macro", "macro_data"),
    FeedSource("fda_press", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
               1, "stock", "fda"),
    FeedSource("sec_press", "https://www.sec.gov/news/pressreleases.rss", 1, "mixed", "regulatory"),
    FeedSource("cftc_press", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", 1, "mixed", "regulatory"),
    FeedSource("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", 3, "crypto"),
    FeedSource("cointelegraph", "https://cointelegraph.com/rss", 3, "crypto"),
    FeedSource("decrypt", "https://decrypt.co/feed", 3, "crypto"),
    FeedSource("coinbase_status", "https://status.coinbase.com/history.rss", 1, "crypto", "exchange"),
    FeedSource("kraken_status", "https://status.kraken.com/history.rss", 1, "crypto", "exchange"),
]
