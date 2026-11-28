"""D105 multi-agent team: analysts' verdicts, consensus, and the Risk Engine's final word."""
import dataclasses
import json
from datetime import timedelta

import pytest

from app.agents import analysts as A
from app.agents import team as TM
from app.agents.consensus import WEIGHTS, combine, final_call
from app.config import load_settings
from app.core.safety import SafeState
from app.database.retention import prune
from app.execution.trader import PaperTrader
from app.learning import scorecard
from tests.test_risk_execution import NOW, add_signal, proposal, q

GOOD = {"asset_class": "crypto", "strategy": "momentum_breakout", "rr_net": 2.2, "rsi": 60, "rvol": 1.8,
        "res_dist_atr": 3.0, "trend": "up", "regime": {"trend": "bull", "vol": "high", "text": "Rising, high volatility"},
        "regime_ok": True, "market_trend": "up", "breadth": 0.7, "finbert": 0.3, "neg_verified": 0, "pos_catalysts": 0,
        "macro": 0.1, "llm": {"decision": "APPROVE", "reason": "ok"}, "validation_status": "validated",
        "scorecard": {"state": "active", "mult": 1.0, "n": 12, "avg_r": 0.2, "streak": 0}}


def verdicts(c):
    return {"market": A.market_analyst(c), "technical": A.technical_analyst(c), "news": A.news_analyst(c),
            "strategy": A.strategy_analyst(c)}


def mk(settings, db, **over):
    s = load_settings(overrides={"paths": {"database": settings.paths.database},
                                 "execution": {"paper_leverage": 1, "crypto_entry_order": "market"},
                                 "llm": {"enabled": False}, **over})
    t = PaperTrader(s, db, SafeState(db), engine=None)
    t.quote = lambda sym, now: q()
    return t


def row(db, sid):
    return db.conn.execute("SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc ON sc.signal_id=s.id "
                           "WHERE s.id=?", (sid,)).fetchone()


# ---------------------------------------------------------------- analysts
def test_every_verdict_is_structured_and_plain():
    for k, v in verdicts(GOOD).items():
        assert 0 <= v.score <= 100 and v.stance in ("APPROVE", "NEUTRAL", "REJECT") and v.reason and v.agent == k
        assert 0 <= v.mult <= 1
    assert all(v.stance == "APPROVE" for v in verdicts(GOOD).values())


@pytest.mark.parametrize("patch,agent", [
    ({"regime_ok": False}, "market"), ({"res_dist_atr": 0.1}, "technical"), ({"rr_net": 0.5}, "technical"),
    ({"llm": {"decision": "VETO", "reason": "exchange hack"}}, "news"), ({"neg_verified": 1}, "news"),
    ({"finbert": -0.5}, "news"), ({"scorecard": {"state": "paused", "why": "6 losses in a row"}}, "strategy")])
def test_clear_rejects_are_hard(patch, agent):
    v = verdicts(GOOD | patch)[agent]
    assert v.hard and v.stance == "REJECT" and v.score <= 15


def test_reduced_strategy_halves_size_and_dip_buy_ignores_trend():
    v = A.strategy_analyst(GOOD | {"scorecard": {"state": "reduced", "mult": 0.5, "why": "slightly losing"}})
    assert v.mult == 0.5 and v.stance == "NEUTRAL"
    dip = {"strategy": "rsi2_swing", "trend": "down", "rsi": 3, "rr_net": 2.0}
    assert A.technical_analyst(dip).score == A.technical_analyst(dip | {"trend": "up"}).score


# ---------------------------------------------------------------- consensus
def test_consensus_weights_and_stance():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0) and WEIGHTS["technical"] == max(WEIGHTS.values())
    c = combine(verdicts(GOOD))
    assert c.stance == "APPROVE" and c.blocked_by is None and c.mult == 1.0
    w = {k: A._v(k, sc, ["x"]) for k, sc in {"technical": 100, "market": 0, "strategy": 0, "news": 0}.items()}
    assert combine(w).score == 35 and combine({**w, "market": A._v("market", 100, ["x"])}).score == 60


def test_any_hard_reject_from_a_non_risk_analyst_blocks_even_in_advisory_mode():
    for patch in ({"regime_ok": False}, {"llm": {"decision": "VETO", "reason": "hack"}},
                  {"scorecard": {"state": "paused"}}):
        c = combine(verdicts(GOOD | patch), enforce=False)
        assert c.blocked_by in ("market", "news", "strategy") and c.mult == 0 and c.stance == "REJECT"


def test_score_only_gates_when_enforced_and_only_downsizes_or_blocks():
    weak = verdicts(GOOD | {"rr_net": 1.0, "rvol": 0.5, "trend": "range", "regime": None, "regime_ok": None,
                            "market_trend": None, "breadth": None, "finbert": None, "llm": None,
                            "validation_status": "forward_test", "scorecard": None})
    adv, enf = combine(weak, enforce=False), combine(weak, enforce=True)
    assert adv.blocked_by is None and adv.mult == 1.0                      # advisory: shown, not acted on
    assert enf.score < 60 and enf.blocked_by is None
    assert combine(weak, enforce=True, block_below=100).blocked_by == "consensus"
    assert combine(weak, enforce=False, block_below=100).blocked_by is None
    assert combine(weak, enforce=True, block_below=0, downsize_below=100, downsize_mult=0.5).mult == 0.5


def test_analysts_can_never_raise_size():
    v = verdicts(GOOD)
    v["strategy"] = dataclasses.replace(v["strategy"], mult=5.0)         # a buggy / hostile analyst
    assert combine(v).mult == 1.0
    v["strategy"] = dataclasses.replace(v["strategy"], mult=0.25)
    assert combine(v).mult == 0.25


# ---------------------------------------------------------------- Risk Engine has the last word
def test_risk_veto_always_wins_in_final_call():
    cons = combine(verdicts(GOOD))
    rej = A.Verdict("risk", 20, "REJECT", "Blocked: x.", True, 0.0)
    ok = A.Verdict("risk", 100, "APPROVE", "All checks passed.")
    assert final_call(cons, rej)["decision"] == "NO_TRADE" and final_call(cons, rej)["by"] == "risk"
    assert final_call(cons, ok)["decision"] == "TRADE"
    assert final_call(cons, None)["decision"] == "NO_TRADE"                # never consulted -> never trades
    blocked = combine(verdicts(GOOD | {"regime_ok": False}))
    assert final_call(blocked, ok)["decision"] == "NO_TRADE"               # an approving Risk Engine cannot resurrect it


def test_end_to_end_risk_veto_beats_unanimous_analysts(settings, db):
    t = mk(settings, db)
    t.quote = lambda sym, now: q(verdict="disagree")                         # unreliable price: a hard rail must refuse
    sid = add_signal(db)
    r = t.execute_signal(row(db, sid), NOW)
    assert not r["approved"] and "quote_reliable" in r["reason"]
    assert db.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    rv = db.conn.execute("SELECT * FROM agent_reviews WHERE signal_id=?", (sid,)).fetchone()
    v = json.loads(rv["verdicts_json"])
    assert rv["final_decision"] == "NO_TRADE" and rv["final_by"] == "risk"
    assert v["risk"]["stance"] == "REJECT" and set(v) == {"market", "technical", "news", "strategy", "risk"}
    assert "price quote unreliable" in v["risk"]["reason"]


def test_end_to_end_trade_passes_only_through_risk_and_rails_untouched(settings, db):
    t = mk(settings, db)
    seen = []
    real = t.risk.evaluate
    t.risk.evaluate = lambda p, quote, now=None, marks=None, waive=frozenset(): (seen.append((p, waive)),
                                                                                  real(p, quote, now, marks, waive))[1]
    db.set_state(scorecard.KEY, {"strategies": {"momentum_breakout": {"state": "reduced", "mult": 0.5, "why": "x"}}})
    sid = add_signal(db)
    base = proposal(db, sid)
    r = t.execute_signal(row(db, sid), NOW)
    assert r["approved"], r["reason"]
    (p, waive), = seen
    assert waive == frozenset()                                              # analysts never waive a check
    assert (p.stop, p.target, p.entry, p.leverage, p.symbol) == (base.stop, base.target, base.entry, base.leverage,
                                                                  base.symbol)
    assert p.risk_mult == 0.5 <= 1.0                                         # only ever smaller
    rv = db.conn.execute("SELECT * FROM agent_reviews WHERE signal_id=?", (sid,)).fetchone()
    assert rv["final_decision"] == "TRADE" and rv["final_by"] == "risk" and rv["size_mult"] == 0.5


def test_analyst_block_skips_risk_engine_and_is_recorded(settings, db):
    t = mk(settings, db)
    called = []
    t.risk.evaluate = lambda *a, **k: called.append(1)
    db.set_state(scorecard.KEY, {"strategies": {"momentum_breakout": {"state": "paused", "why": "6 losses in a row"}}})
    sid = add_signal(db)
    r = t.execute_signal(row(db, sid), NOW)
    assert not r["approved"] and not called
    rv = db.conn.execute("SELECT * FROM agent_reviews WHERE signal_id=?", (sid,)).fetchone()
    assert rv["final_decision"] == "NO_TRADE" and rv["final_by"] == "strategy"
    assert json.loads(rv["verdicts_json"])["risk"]["data"] == {"consulted": False}


def test_team_failure_never_opens_a_door(settings, db, monkeypatch):
    t = mk(settings, db)
    t.quote = lambda sym, now: q(verdict="disagree")
    monkeypatch.setattr(TM, "run_analysts", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    sid = add_signal(db)
    r = t.execute_signal(row(db, sid), NOW)
    assert not r["approved"] and "quote_reliable" in r["reason"]           # Risk Engine still decides alone


def test_disabled_team_changes_nothing(settings, db):
    t = mk(settings, db, agents={"enabled": False})
    sid = add_signal(db)
    assert t.execute_signal(row(db, sid), NOW)["approved"]
    assert db.conn.execute("SELECT COUNT(*) FROM agent_reviews").fetchone()[0] == 0


# ---------------------------------------------------------------- persistence + pruning + dashboard
def test_reviews_are_pruned_with_the_rest(settings, db):
    t = mk(settings, db)
    sid = add_signal(db)
    t.execute_signal(row(db, sid), NOW)
    assert prune(db, now=NOW + timedelta(days=1))["agent_reviews_deleted"] == 0
    assert prune(db, now=NOW + timedelta(days=15))["agent_reviews_deleted"] == 1


def test_dashboard_analyst_team_view(settings, db, tmp_path):
    from app.dashboard.live import agents_view
    t = mk(settings, db)
    t.execute_signal(row(db, add_signal(db)), NOW)
    v = agents_view(db, settings, NOW + timedelta(hours=1))
    assert v["enabled"] and v["items"] and v["items"][0]["final"]["decision"] == "TRADE"
    names = [a["label"] for a in v["items"][0]["agents"]]
    assert names == ["Market Analyst", "Technical Analyst", "News Analyst", "Strategy Analyst", "Risk Engine"]
    assert v["items"][0]["agents"][-1]["stance"] == "APPROVE"
