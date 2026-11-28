"""D105 consensus of the four analysts + the Risk Engine's final word.

Weighted score (0-100): Technical 35 %, Market 25 %, Strategy 20 %, News 20 %. Technical carries most weight because it is
about this exact setup; Market / Strategy / News are context. Rules:
- A hard REJECT from any analyst (clear concrete reason: strategy paused, Qwen / verified negative news, regime off,
  resistance on top of the entry, reward < 0.8x risk) removes the trade. Always on.
- `enforce` (config, decided by the D105 out-of-sample check): score < block_below removes the trade, score <
  downsize_below halves it. When off (advisory) the score is shown but only clear rejects act.
- The size multiplier is only ever <= 1 (min of the strategy's scorecard multiplier and the consensus downsize).
- The Risk Engine runs LAST on whatever survives and decides. If it rejects, the answer is no, whatever the analysts
  said; its rails (leverage, liquidation distance, exposure, drawdown, max positions, gap risk, R:R) are not touched
  by anything in this package."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.analysts import APPROVE_AT, LABEL, Verdict

WEIGHTS = {"technical": 0.35, "market": 0.25, "strategy": 0.20, "news": 0.20}


@dataclass
class Consensus:
    score: int
    stance: str                       # APPROVE | NEUTRAL | REJECT
    mult: float                       # <= 1
    blocked_by: str | None            # agent key, "consensus" or None
    reason: str
    enforced: bool

    def as_dict(self) -> dict:
        return {"score": self.score, "stance": self.stance, "mult": self.mult, "blocked_by": self.blocked_by,
                "reason": self.reason, "enforced": self.enforced}


def combine(verdicts: dict[str, Verdict], enforce: bool = False, block_below: float = 35,
            downsize_below: float = 50, downsize_mult: float = 0.5, weights: dict | None = None) -> Consensus:
    w = weights or WEIGHTS
    tot = sum(w[k] for k in w if k in verdicts) or 1.0
    score = round(sum(verdicts[k].score * w[k] for k in w if k in verdicts) / tot)
    mult = 1.0
    for v in verdicts.values():
        mult = min(mult, max(0.0, min(1.0, v.mult)))            # never above 1, whatever an analyst returns
    hard = next((v for k, v in verdicts.items() if k in w and v.hard), None)
    if hard is not None:
        return Consensus(score, "REJECT", 0.0, hard.agent, f"{LABEL[hard.agent]}: {hard.reason}", enforce)
    if enforce and score < block_below:
        return Consensus(score, "REJECT", 0.0, "consensus",
                         f"The team's combined score is only {score}/100 (needs {int(block_below)}).", enforce)
    if enforce and score < downsize_below:
        mult = min(mult, max(0.0, min(1.0, downsize_mult)))
        return Consensus(score, "NEUTRAL", mult, None,
                         f"Mixed signals ({score}/100): trade at {int(mult * 100)}% size.", enforce)
    stance = "APPROVE" if score >= APPROVE_AT else "NEUTRAL"
    return Consensus(score, stance, mult, None,
                     f"The team agrees ({score}/100)." if stance == "APPROVE" else f"Mixed signals ({score}/100).",
                     enforce)


def final_call(consensus: Consensus, risk: Verdict | None) -> dict:
    """The Risk Engine has the last word. `risk` None = analysts removed the trade before it reached the engine."""
    if consensus.blocked_by is not None:
        return {"decision": "NO_TRADE", "by": consensus.blocked_by, "reason": consensus.reason, "mult": 0.0}
    if risk is None:
        return {"decision": "NO_TRADE", "by": "risk", "reason": "Risk Engine not consulted.", "mult": 0.0}
    if risk.stance == "REJECT":
        return {"decision": "NO_TRADE", "by": "risk", "reason": risk.reason, "mult": 0.0}
    return {"decision": "TRADE", "by": "risk", "reason": risk.reason, "mult": consensus.mult}
