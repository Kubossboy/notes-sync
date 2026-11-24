"""Finance lexicon sentiment with negation handling.

A compact, hand-curated lexicon in the spirit of Loughran-McDonald (finance
specific: 'liability' or 'tax' are not negative here). Deterministic and
explainable: matched words are returned. Not ML - limitations documented.
"""
from __future__ import annotations

import re

POS = set("""beat beats surge surges surged soar soars soared rally rallies rallied jump jumps jumped gain gains gained
record strong stronger strongest growth grow grows grew upgrade upgraded upgrades outperform outperforms bullish
boost boosts boosted rise rises rose rising climb climbs climbed rebound rebounds rebounded profit profitable
profits optimistic optimism robust exceed exceeds exceeded raise raises raised higher high approval approves
approved breakthrough wins win won expand expands expansion accelerate accelerates momentum positive upbeat
recovery recover recovers recovered inflows tops topped success successful milestone favorable buy""".split())
NEG = set("""miss misses missed plunge plunges plunged plummet plummets tumble tumbles tumbled drop drops dropped fall
falls fell falling slump slumps slumped crash crashes crashed loss losses lose loses lost weak weaker weakest decline
declines declined downgrade downgraded downgrades underperform bearish cut cuts slash slashes slashed lower lowers
lowered sink sinks sank slide slides slid concern concerns worry worries fear fears risk risks warning warns warned
lawsuit probe investigation fraud default bankruptcy layoffs recall halt halted suspend suspended outflows hack
hacked exploit exploited delist delisted negative pessimistic disappointing disappoints disappointed sell selloff
sell-off dump dumps dumped volatile turmoil crisis recession inflation shortfall penalty fine fined charges""".split())
NEGATORS = {"not", "no", "never", "without", "fails", "failed", "isn't", "wasn't", "didn't", "doesn't", "won't",
            "neither", "nor", "despite"}
_TOK = re.compile(r"[a-z][a-z'\-]*")


def lexicon_sentiment(text: str) -> tuple[float, dict]:
    toks = _TOK.findall(text.lower())
    pos, neg, hits = 0, 0, []
    for i, t in enumerate(toks):
        pol = 1 if t in POS else -1 if t in NEG else 0
        if not pol:
            continue
        if any(w in NEGATORS for w in toks[max(0, i - 3):i]):
            pol = -pol
            hits.append(f"not:{t}")
        else:
            hits.append(t)
        if pol > 0:
            pos += 1
        else:
            neg += 1
    score = (pos - neg) / (pos + neg + 2)          # bounded in (-1, 1), dampened for few hits
    return round(score, 4), {"pos": pos, "neg": neg, "hits": hits[:12]}
