"""D105 team runner: builds each analyst's inputs from a signal row (+ state), runs the analysts, the consensus and
turns the Risk Engine's decision into the fifth verdict; persists one `agent_reviews` row per candidate."""
from __future__ import annotations

import json
from datetime import datetime

from app.agents import analysts as A
from app.agents.consensus import combine, final_call
from app.agents.analysts import Verdict, _v

PLAIN_RISK = {
    "safe_state_normal": "the bot is in a safety pause", "execution_mode_paper": "not in paper mode",
    "strategy_validated_or_forward_test": "the strategy is not validated", "score_threshold": "score too low",
    "signal_not_expired": "the signal is stale", "data_live": "data is not live",
    "quote_reliable": "price quote unreliable", "quote_fresh": "price quote too old",
    "stock_regular_session": "stock market is closed", "stock_entry_window": "too close to the market close",
    "swing_entry_window": "outside the near-the-close entry window", "swing_unlevered": "overnight trades can't use leverage",
    "max_open_positions": "already at the maximum number of open trades",
    "no_existing_position_in_symbol": "already holding this symbol", "daily_loss_limit_not_reached": "the daily loss limit is reached",
    "consecutive_losses_today": "too many losses in a row today", "symbol_cooldown_after_loss": "cooling down after a loss in this symbol",
    "leveraged_drawdown_floor": "account is at the drawdown floor", "no_chasing_entry_drift": "price already ran away from the entry",
    "stop_before_liquidation": "the stop would sit beyond the liquidation price", "net_reward_risk": "reward/risk after costs is too low",
    "risk_within_limit": "position would risk too much", "risk_within_remaining_daily_budget": "not enough daily risk budget left",
    "order_vs_liquidity": "order too large for the market's liquidity", "cash_covers_margin_and_fees": "not enough cash for margin",
    "no_leverage_cash_covers_notional": "not enough cash", "min_notional": "order too small", "position_size_positive": "size rounds to zero",
    "stop_loss_present_valid": "stop-loss is missing or invalid", "take_profit_present_valid": "take-profit is missing or invalid",
}


def risk_verdict(d, p) -> Verdict:
    """Risk Engine verdict from its RiskDecision (all hard rails already evaluated inside the engine)."""
    total, failed = len(d.checks), [c for c in d.checks if not c.passed]
    data = {"checks": total, "failed": [c.name for c in failed], "qty": d.quantity, "notional": d.notional,
            "planned_risk_usd": d.planned_risk_usd, "leverage": p.leverage, "rr_net": d.rr_net,
            "size_mult": p.risk_mult}
    if d.approved:
        bits = [f"All {total} safety checks passed", f"risking about ${d.planned_risk_usd:.2f}"]
        if p.leverage and p.leverage > 1:
            bits.append(f"at {p.leverage:g}x leverage with the stop before liquidation")
        if p.risk_mult < 1:
            bits.append(f"size cut to {int(p.risk_mult * 100)}%")
        return _v("risk", 100, [", ".join(bits) + "."], data=data)
    why = ", ".join(PLAIN_RISK.get(c.name, c.name.replace("_", " ")) for c in failed[:3]) or (d.reason or "rejected")
    return _v("risk", 100.0 * (total - len(failed)) / max(total, 1) * 0.4, [f"Blocked: {why}."], hard=True, mult=0.0, data=data)


def not_reached(reason: str) -> Verdict:
    return Verdict("risk", 0, "NEUTRAL", reason, False, 0.0, {"consulted": False})


# ---------------------------------------------------------------- inputs
def features(row, db, settings, now: datetime | None = None, llm: dict | None = None, breadth=None) -> dict:
    """Normalised feature dict for the analysts from a `signals` row (evidence_json) + system state."""
    ev = json.loads(row["evidence_json"] or "{}") if "evidence_json" in row.keys() else {}
    plan, feat = ev.get("plan") or {}, ev.get("features") or {}
    news, sent = ev.get("news") or {}, ev.get("sentiment") or {}
    reg = ev.get("regime")
    ac = row["asset_class"]
    ok = None
    if reg and settings.regime.enabled:
        from app.regime import allowed
        ok = allowed(settings.regime.matrix, ac, row["strategy"], reg)
    try:
        from app.learning.scorecard import status
        sc = status(db, row["strategy"])
        card = (db.get_state("learning_scorecard") or {}).get("strategies", {}).get(row["strategy"]) or sc
    except Exception:
        card = None
    if llm is None:
        for v in reversed(db.get_state("llm_verdicts") or []):
            if v.get("signal_id") == row["id"]:
                llm = v
    if breadth is None:
        breadth = (db.get_state("agents_breadth") or {}).get(ac)
    nv = news.get("negative_high_verified")
    return {"asset_class": ac, "strategy": row["strategy"], "rr_net": plan.get("reward_risk_net"),
            "rsi": feat.get("rsi14"), "rvol": feat.get("rvol20"), "res_dist_atr": feat.get("res_dist_atr"),
            "trend": ev.get("trend"), "regime": reg, "regime_ok": ok, "market_trend": ev.get("market_trend"),
            "breadth": breadth, "finbert": news.get("aggregate"),
            "neg_verified": len(nv) if isinstance(nv, list) else (nv or 0),
            "pos_catalysts": len(news.get("positive_catalysts") or []),
            "macro": (sent.get("components") or {}).get("macro"), "llm": llm, "scorecard": card,
            "validation_status": row["validation_status"] if "validation_status" in row.keys() else None}


def run_analysts(c: dict) -> dict[str, Verdict]:
    return {"market": A.market_analyst(c), "technical": A.technical_analyst(c), "news": A.news_analyst(c),
            "strategy": A.strategy_analyst(c)}


def persist(db, row, now: datetime, verdicts: dict[str, Verdict], cons, final: dict) -> None:
    db.conn.execute(
        "INSERT OR REPLACE INTO agent_reviews (ts, signal_id, symbol, asset_class, strategy, consensus_score, "
        "consensus_stance, size_mult, enforced, final_decision, final_by, final_reason, verdicts_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now.isoformat(), row["id"], row["symbol"], row["asset_class"], row["strategy"], cons.score, cons.stance,
         final.get("mult"), int(cons.enforced), final["decision"], final["by"], final["reason"][:300],
         json.dumps({k: v.as_dict() for k, v in verdicts.items()})))
    db.conn.commit()
