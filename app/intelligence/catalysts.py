"""Rule-based catalyst detection on headlines/summaries.

Each rule: (category, direction +1/-1/0, impact low|medium|high, regex).
Deterministic and auditable: the matched phrase is stored as evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

IMPACT_WEIGHT = {"low": 0.5, "medium": 1.0, "high": 2.0}


@dataclass(frozen=True)
class Catalyst:
    category: str
    direction: int
    impact: str
    matched: str


_R = [
    # earnings & guidance
    ("earnings_beat", 1, "high", r"\b(beats?|tops?|surpass(?:es)?|exceeds?) (?:\w+ ){0,3}(estimates|expectations|forecasts|consensus)"),
    ("earnings_miss", -1, "high", r"\b(miss(?:es|ed)?|falls? short of|below) (?:\w+ ){0,3}(estimates|expectations|forecasts|consensus)"),
    ("guidance_raise", 1, "high", r"\b(raises?|lifts?|boosts?|hikes?|ups) (?:\w+ ){0,3}(guidance|outlook|forecast)"),
    ("guidance_cut", -1, "high", r"\b(cuts?|lowers?|slashes|trims?|withdraws?) (?:\w+ ){0,3}(guidance|outlook|forecast)"),
    ("earnings_report", 0, "medium", r"\b(reports?|posts?|announces?) (?:\w+ ){0,4}(quarter(?:ly)?|Q[1-4]|fiscal|annual) (?:\w+ ){0,2}(results|earnings)"),
    # corporate actions
    ("acquisition", 1, "high", r"\b(to acquire|agrees? to buy|buyout (?:offer|bid|deal) for|takeover (?:bid|offer)|merger agreement|to be acquired|leveraged buyout)\b"),
    ("buyback", 1, "medium", r"\b(share (?:buyback|repurchase)|buyback program|repurchase program)\b"),
    ("dividend_raise", 1, "medium", r"\b(raises?|increases?|hikes?) (?:\w+ ){0,2}dividend"),
    ("equity_offering", -1, "medium", r"\b(public offering|stock offering|share sale|secondary offering|at-the-market offering|convertible (?:notes|senior notes))\b"),
    ("bankruptcy", -1, "high", r"\b(bankruptcy|Chapter 11|insolven(?:t|cy))\b"),
    ("layoffs", -1, "medium", r"\b(layoffs?|job cuts|to cut \d[\d,]* jobs)\b"),
    ("exec_departure", -1, "medium", r"\b(CEO|CFO|chief executive|chief financial officer) (?:\w+ ){0,3}(resigns?|steps down|departs?|ousted|fired)"),
    ("partnership", 1, "medium", r"\b(partnership|strategic alliance|multi-year (?:deal|agreement)|wins? (?:a )?contract|awarded (?:a )?contract)\b"),
    ("product_launch", 1, "low", r"\b(launch(?:es|ed)?|unveils?|introduces?) (?:\w+ ){0,3}(new|next-generation|AI)\b"),
    # analysts
    ("analyst_upgrade", 1, "medium", r"\bupgrade[sd]? (?:\w+ )?(?:to|at) (?:buy|outperform|overweight|strong buy|positive)\b|\bupgraded by\b|\bprice target (?:raised|hiked|boosted)|\braises? (?:\w+ ){0,3}price target"),
    ("analyst_downgrade", -1, "medium", r"\bdowngrade[sd]? (?:\w+ )?(?:to|at) (?:sell|underperform|underweight|neutral|hold|negative)\b|\bdowngraded by\b|\bprice target (?:cut|lowered|slashed)|\b(cuts?|lowers?) (?:\w+ ){0,3}price target"),
    # legal / regulatory
    ("legal_regulatory", -1, "medium", r"\b(lawsuit|sued|sues|probe|investigation|subpoena|antitrust|fined?|penalty|settles? charges|SEC charges|indicted|class action)\b"),
    ("fda_approval", 1, "high", r"\bFDA (?:\w+ ){0,2}(approv(?:es|al|ed)|clears?|grants? (?:accelerated|breakthrough))"),
    ("fda_rejection", -1, "high", r"\b(complete response letter|FDA (?:\w+ ){0,2}(rejects?|declines?|refuses?)|clinical hold)\b"),
    ("trial_success", 1, "high", r"\b(trial|study) (?:\w+ ){0,3}(met (?:its )?primary endpoint|positive (?:top-?line )?results)"),
    ("trial_failure", -1, "high", r"\b(trial|study) (?:\w+ ){0,3}(failed|missed (?:its )?primary endpoint)"),
    ("recall_safety", -1, "medium", r"\b(recall(?:s|ed)?|safety warning|data breach)\b"),
    # macro / Fed
    ("fed_rate_cut", 1, "high", r"\b(cuts?|lowers?|reduces?) (?:\w+ ){0,3}(interest )?rates?\b|\brate cut\b"),
    ("fed_rate_hike", -1, "high", r"\b(raises?|hikes?|increases?) (?:\w+ ){0,3}(interest )?rates?\b|\brate hike\b"),
    ("fed_hold", 0, "high", r"\b(holds?|keeps?|leaves?|maintains?) (?:\w+ ){0,3}rates? (?:steady|unchanged)|\bFOMC statement\b"),
    ("fed_minutes", 0, "medium", r"\bminutes of the federal open market committee\b|\bFOMC minutes\b"),
    ("inflation_hot", -1, "high", r"\b(inflation|CPI|PCE) (?:\w+ ){0,3}(accelerat|hotter|jumps?|surges?|rises? more than)"),
    ("inflation_cool", 1, "high", r"\b(inflation|CPI|PCE) (?:\w+ ){0,3}(cools?|eases?|slows?|softer|falls?)"),
    ("geopolitical", -1, "medium", r"\b(wars?|invasion|invades?|missile strikes?|military strikes?|sanctions|tariffs?|escalat(?:es|ed|ion|ing))\b"),
    # crypto
    ("crypto_etf_approval", 1, "high", r"\bETF (?:\w+ ){0,3}(approv|launch|inflows?)\w*|\b(approv\w+) (?:\w+ ){0,3}ETF"),
    ("crypto_etf_outflow", -1, "medium", r"\bETF (?:\w+ ){0,3}outflows?\b"),
    ("crypto_regulatory_negative", -1, "high", r"\b(SEC sues (?:\w+ ){0,3}(?:crypto|exchange|Coinbase|Binance|Kraken)|crypto crackdown|ban(?:s|ned)? (?:crypto|bitcoin|stablecoins?)|wells notice)\b"),
    ("regulatory_enforcement", -1, "medium", r"\b(enforcement action|cease[- ]and[- ]desist|consent order|civil penalty)\b"),
    ("crypto_regulatory_positive", 1, "medium", r"\b(regulatory clarity|bill passes|legislation passes|license granted|approved license)\b"),
    ("exchange_hack", -1, "high", r"\b(hack(?:ed)?|exploit(?:ed)?|drained|stolen funds|security breach)\b"),
    ("exchange_outage", -1, "medium", r"\b(outage|degraded performance|withdrawals? (?:paused|suspended|halted)|(?:withdrawals?|withdrawing|deposits?) delays?|trading (?:halted|suspended))\b"),
    ("exchange_maintenance", 0, "low", r"\b(scheduled maintenance|maintenance window)\b"),
    ("exchange_listing", 1, "medium", r"\b(will list|to list|lists|listing of) (?:\w+ ){0,3}(token|coin|on (?:Coinbase|Binance|Kraken|Robinhood))\b"),
    ("exchange_delisting", -1, "high", r"\b(delist(?:s|ing|ed)?)\b"),
    ("token_unlock", -1, "medium", r"\btoken unlocks?\b"),
    ("network_upgrade", 1, "low", r"\b(mainnet|hard fork|network upgrade|protocol upgrade)\b"),
    ("whale_selling", -1, "low", r"\b(whales? (?:sell|dump)|liquidations?)\w*"),
]
RULES = [(c, d, i, re.compile(p, re.I)) for c, d, i, p in _R]
UNEXPECTED = re.compile(r"\b(unexpected(?:ly)?|surpris(?:e|ing|ingly)|shock(?:ing)?|abrupt(?:ly)?|halted|emergency|"
                        r"out of nowhere|stunned)\b", re.I)


# "kills rate-hike bets", "rules out a buyback", "no rate cut": direction becomes unknown (0),
# we never flip a direction on a heuristic.
NEGATED_BEFORE = re.compile(r"\b(no|not(?! only)|never|kills?|scraps?|rules? out|ruled out|unlikely|fades?|pares?|"
                            r"abandons?|drops? plans? for|denies|denied|rejects?|termination of|terminates|lifts|lifted)\b(?:\W+\w+){0,5}\W*$", re.I)


def detect(text: str) -> tuple[list[Catalyst], bool]:
    found, seen = [], set()
    for cat, d, imp, pat in RULES:
        m = pat.search(text)
        if m and cat not in seen:
            seen.add(cat)
            if d and NEGATED_BEFORE.search(text[max(0, m.start() - 60):m.start()]):
                found.append(Catalyst(cat, 0, imp, "negated: " + m.group(0)))
            else:
                found.append(Catalyst(cat, d, imp, m.group(0)))
    # an explicit beat/miss supersedes the generic "reports results"
    if any(c.category in ("earnings_beat", "earnings_miss") for c in found):
        found = [c for c in found if c.category != "earnings_report"]
    return found, bool(UNEXPECTED.search(text))


def net_direction(cats: list[Catalyst]) -> float:
    w = sum(IMPACT_WEIGHT[c.impact] for c in cats if c.direction != 0)
    if not w:
        return 0.0
    return sum(c.direction * IMPACT_WEIGHT[c.impact] for c in cats if c.direction != 0) / w


def max_impact(cats: list[Catalyst]) -> str | None:
    if not cats:
        return None
    return max((c.impact for c in cats), key=lambda x: IMPACT_WEIGHT[x])
