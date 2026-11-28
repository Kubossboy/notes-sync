"""D105 specialised analysts. Pure, deterministic functions on a normalised feature dict `c` (no I/O, no extra LLM
calls): each returns a Verdict {score 0-100, stance APPROVE / NEUTRAL / REJECT, short plain-English reason}.

A `hard` REJECT is a clear, concrete reason (not just a low score). Analysts can only remove or downsize a trade
(`mult` <= 1); nothing here can raise a size, widen a stop or loosen a risk rail."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

APPROVE_AT, REJECT_BELOW = 60, 40
LABEL = {"market": "Market Analyst", "technical": "Technical Analyst", "news": "News Analyst",
         "strategy": "Strategy Analyst", "risk": "Risk Engine"}
DIP_BUY = {"rsi2_swing"}          # buys weakness on purpose: trend / RSI penalties do not apply


@dataclass
class Verdict:
    agent: str
    score: int
    stance: str
    reason: str
    hard: bool = False
    mult: float = 1.0                     # <= 1 only: a size ceiling, never a boost
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["label"] = LABEL.get(self.agent, self.agent)
        return d


def stance_for(score: float) -> str:
    return "APPROVE" if score >= APPROVE_AT else "REJECT" if score < REJECT_BELOW else "NEUTRAL"


def _v(agent, score, reasons: list[str], hard: bool = False, mult: float = 1.0, data=None) -> Verdict:
    s = int(round(max(0.0, min(100.0, score))))
    if hard:
        s = min(s, 15)
    return Verdict(agent, s, "REJECT" if hard else stance_for(s), " ".join(reasons)[:260], hard,
                   max(0.0, min(1.0, float(mult))), data or {})


def _num(x):
    return x if isinstance(x, (int, float)) and x == x else None


# ---------------------------------------------------------------- Market Analyst
def market_analyst(c: dict) -> Verdict:
    """Regime (D100), benchmark trend and breadth: is the whole market friendly to this kind of trade?"""
    s, why = 50.0, []
    reg = c.get("regime")
    if reg:
        name = reg.get("text") or f"{reg.get('trend')}/{reg.get('vol')}"
        if c.get("regime_ok") is False:
            return _v("market", 10, [f"Market is {name.lower()}, which this strategy is switched off in."], hard=True)
        s += 15 if c.get("regime_ok") else 0
        s += {"bull": 15, "bear": -10}.get(reg.get("trend"), 0)
        why.append(f"Market is {name.lower()}.")
    else:
        why.append("Market mood is not known yet.")
    mt = c.get("market_trend")
    if mt in ("up", "down"):
        s += 10 if mt == "up" else -10
        why.append(f"The benchmark is {'rising' if mt == 'up' else 'falling'} short term.")
    b = _num(c.get("breadth"))
    if b is not None:
        s += 10 if b >= 0.6 else -10 if b <= 0.3 else 0
        why.append(f"{round(b * 100)}% of watched symbols are trending up.")
    return _v("market", s, why, data={"breadth": b})


# ---------------------------------------------------------------- Technical Analyst
def technical_analyst(c: dict) -> Verdict:
    """Setup quality: reward/risk, trend, momentum (RSI), volume and room to the next resistance."""
    s, why = 50.0, []
    dip = c.get("strategy") in DIP_BUY
    rr, rsi, rv = _num(c.get("rr_net")), _num(c.get("rsi")), _num(c.get("rvol"))
    res, tr = _num(c.get("res_dist_atr")), c.get("trend")
    hard = False
    if rr is not None:
        s += 15 if rr >= 2 else 8 if rr >= 1.5 else -15 if rr < 1.0 else 0
        why.append(f"Reward is {rr:.1f}x the risk after costs.")
        hard |= rr < 0.8
    if not dip and tr in ("up", "down", "range"):
        s += {"up": 15, "down": -15}.get(tr, 0)
        why.append({"up": "Trend is up.", "down": "Trend is down.", "range": "Price is moving sideways."}[tr])
    if not dip and rsi is not None and rsi > 78:
        s -= 15
        why.append("Price already ran hot (RSI above 78).")
    if rv is not None:
        s += 10 if rv >= 1.5 else 3 if rv >= 1.0 else -8 if rv < 0.7 else 0
        why.append("Volume is strong." if rv >= 1.5 else "Volume is thin." if rv < 0.7 else "Volume is normal.")
    if res is not None:
        s += 8 if res >= 2 else -15 if res < 0.5 else -5 if res < 1 else 0
        if res < 0.5:
            why.append("Resistance sits right above the entry.")
        hard |= res < 0.2
    elif rr is not None:
        s += 5
    if hard and not why[-1:] == ["Resistance sits right above the entry."]:
        why.append("Reward is too small for the risk.")
    return _v("technical", s, why or ["Not enough chart data."], hard=hard, data={"rr": rr, "rsi": rsi, "rvol": rv,
                                                                                  "res_dist_atr": res, "trend": tr})


# ---------------------------------------------------------------- News Analyst
def news_analyst(c: dict) -> Verdict:
    """FinBERT headline tone, verified catalysts, macro mood and the D102 Qwen APPROVE / VETO."""
    s, why = 50.0, []
    llm = c.get("llm") or {}
    if llm.get("decision") == "VETO":
        return _v("news", 5, [f"The AI news check vetoed it: {llm.get('reason') or 'negative news'}".rstrip(".") + "."],
                  hard=True, data={"qwen": "VETO"})
    if (c.get("neg_verified") or 0) > 0:
        return _v("news", 8, ["There is verified negative news on this asset."], hard=True)
    fb = _num(c.get("finbert"))
    if fb is not None:
        s += fb * 40
        why.append(f"Headlines are {'positive' if fb > 0.15 else 'negative' if fb < -0.15 else 'mixed or quiet'} "
                   f"({fb:+.2f}).")
        if fb <= -0.35:
            return _v("news", s, why + ["That is clearly negative."], hard=True, data={"finbert": fb})
    else:
        why.append("No fresh headlines for this asset.")
    if (c.get("pos_catalysts") or 0) > 0:
        s += 10
        why.append("A verified positive catalyst is present.")
    mac = _num(c.get("macro"))
    if mac is not None:
        s += max(-10.0, min(10.0, mac * 30))
        if abs(mac) >= 0.15:
            why.append("Macro backdrop is " + ("supportive." if mac > 0 else "weak."))
    if llm.get("decision") == "APPROVE":
        s += 5
        why.append("The AI news check found no problem.")
    return _v("news", s, why, data={"finbert": fb, "qwen": llm.get("decision")})


# ---------------------------------------------------------------- Strategy Analyst
def strategy_analyst(c: dict) -> Verdict:
    """D104 scorecard: is this strategy currently allowed and healthy, and at what size?"""
    sc = c.get("scorecard") or {"state": "active", "mult": 1.0}
    st, why = sc.get("state", "active"), []
    if st == "paused":
        return _v("strategy", 5, [f"This strategy is paused ({sc.get('why') or 'poor recent results'})."], hard=True,
                  mult=0.0, data={"state": st})
    if st == "reduced":
        return _v("strategy", 42, [f"Running at reduced size: {sc.get('why') or 'recent results are slightly negative'}."],
                  mult=float(sc.get("mult") or 0.5), data={"state": st})
    s = 60.0
    vs = c.get("validation_status")
    if vs == "validated":
        s += 15
        why.append("Backtest-validated strategy.")
    elif vs in ("forward_test", "shadow_promoted"):
        s -= 5
        why.append("Still proving itself on paper.")
    n, avg = sc.get("n") or 0, _num(sc.get("avg_r"))
    if n >= 8 and avg is not None:
        s += max(-20.0, min(20.0, avg * 40))
        why.append(f"Recent results: {avg:+.2f} R per trade over {n} trades.")
    else:
        why.append("Not enough closed trades yet to judge it.")
    if (sc.get("streak") or 0) >= 3:
        s -= 10
        why.append(f"{sc['streak']} losses in a row.")
    return _v("strategy", s, why, data={"state": st, "n": n, "avg_r": avg})
