"""Ticker tagging for headlines. Conservative: precision over recall.

Rules: cashtags ($NVDA) and exact uppercase tickers (len >= 3, word-bounded)
match; company names match case-sensitively on curated aliases. Ambiguous
short tickers/words are only matched as cashtags. Macro and crypto-market-wide
items get pseudo tags MACRO / CRYPTO_MARKET.
"""
from __future__ import annotations

import re

STOCK_ALIASES = {
    "AAPL": ["Apple"], "MSFT": ["Microsoft"], "NVDA": ["Nvidia", "NVIDIA"], "TSLA": ["Tesla"],
    "AMD": ["Advanced Micro Devices"], "META": ["Meta Platforms", "Facebook parent"],
    "AMZN": ["Amazon"], "GOOGL": ["Alphabet", "Google"], "SPY": ["S&P 500"], "QQQ": ["Nasdaq 100", "Nasdaq-100"],
}
CRYPTO_ALIASES = {
    "BTC-USD": ["Bitcoin", "bitcoin", "BTC"], "ETH-USD": ["Ethereum", "ethereum", "Ether", "ETH"],
    "SOL-USD": ["Solana", "solana"], "XRP-USD": ["XRP"], "DOGE-USD": ["Dogecoin"],
}
AMBIGUOUS = {"META", "AMD", "SOL", "ALL", "ON", "IT", "A", "ARE", "NOW", "OPEN", "CAN", "ONE", "RUN", "BIG", "LOW"}
MACRO_TERMS = re.compile(r"\b(Federal Reserve|FOMC|Fed (?:chair|governor|officials?|minutes)|Powell|interest rates?|"
                         r"rate (?:cut|hike|decision)|inflation|CPI|PCE|payrolls|jobs report|unemployment|GDP|"
                         r"Treasury yields?|tariffs?|recession|sanctions?|war|ceasefire)\b", re.I)
CRYPTO_MARKET_TERMS = re.compile(r"\b(crypto(?:currency|currencies)?|stablecoins?|digital assets?|DeFi|"
                                 r"spot (?:bitcoin|ether) ETFs?|exchange outage|exchange hack)\b", re.I)


class TickerTagger:
    def __init__(self, stocks: list[str], cryptos: list[str], extra_names: dict[str, str] | None = None):
        self.rules: list[tuple[re.Pattern, str, float]] = []
        for t in stocks:
            t = t.upper()
            self.rules.append((re.compile(rf"\${re.escape(t)}\b"), t, 1.0))
            if t not in AMBIGUOUS and len(t) >= 3:
                self.rules.append((re.compile(rf"(?<![\w$]){re.escape(t)}(?![\w-])"), t, 0.9))
            names = list(STOCK_ALIASES.get(t, []))
            if extra_names and extra_names.get(t):
                n = re.sub(r"\b(Inc\.?|Corp\.?|Corporation|Co\.?|Ltd\.?|Holdings|Platforms, Inc\.?)\s*$", "",
                           extra_names[t], flags=re.I).strip().rstrip(",")
                if len(n) >= 4 and not n.isupper():
                    names.append(n)
            for n in names:
                self.rules.append((re.compile(rf"(?<!\w){re.escape(n)}(?:'s)?(?!\w)"), t, 0.85))
        for c in cryptos:
            base = c.split("-")[0]
            self.rules.append((re.compile(rf"\${re.escape(base)}\b"), c, 1.0))
            for n in CRYPTO_ALIASES.get(c, []):
                self.rules.append((re.compile(rf"(?<![\w$]){re.escape(n)}(?![\w-])"), c, 0.85))

    def tag(self, text: str) -> dict[str, float]:
        found: dict[str, float] = {}
        for pat, tag, conf in self.rules:
            if pat.search(text):
                found[tag] = max(found.get(tag, 0), conf)
        if MACRO_TERMS.search(text):
            found["MACRO"] = 0.8
        if CRYPTO_MARKET_TERMS.search(text):
            found["CRYPTO_MARKET"] = 0.8
        return found

    def tag_doc(self, title: str, summary: str = "") -> dict[str, float]:
        """Title mentions keep full confidence; summary-only mentions are halved
        (a feed summary often name-drops partners/peers)."""
        t = self.tag(title)
        for k, v in self.tag(summary).items():
            if k not in t:
                t[k] = round(v * 0.5, 3)
        return t
