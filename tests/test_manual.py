"""D87: owner-requested manual paper trade - soft filters waivable, hard limits never."""
from datetime import timedelta

import pytest

from app.execution.manual import manual_trade
from app.execution.trader import PaperTrader
from app.quant.engine import forward_test_stats
from app.risk.engine import MANUAL_WAIVABLE
from tests.test_forward_test import SpyBroker
from tests.test_risk_execution import NOW, FakeEngine, bar, env, proposal, add_signal, q  # noqa: F401


def _news(db):
    return db.insert("news_events", {"fetched_ts": NOW.isoformat(), "published_ts": NOW.isoformat(),
                                     "source": "cftc_press", "title": "CFTC advisory: crypto counts toward reserves",
                                     "content_hash": "h1"})


def test_manual_trade_waives_only_soft_filters_and_mirrors(env):
    broker = SpyBroker()
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine(), alpaca=broker)
    tr.quote = lambda sym, now: q()                                   # fresh reliable quote at 100
    out = manual_trade(tr, "BTC-USD", 99.0, 103.5, _news(env.db), now=NOW)
    assert out["approved"], out["reason"]
    assert any(w.startswith("strategy_validated_or_forward_test") for w in out["waived"])
    assert any(w.startswith("score_threshold") for w in out["waived"])
    assert out["planned_risk_usd"] <= 2.5 + 1e-9 and out["rr_net"] >= 2
    pos = env.db.conn.execute("SELECT * FROM positions WHERE id=?", (out["fill"]["position_id"],)).fetchone()
    assert pos["strategy"] == "manual_news" and pos["validation_status"] == "manual"
    assert out["alpaca"]["mirrored"] and broker.orders[0][2] == pytest.approx(out["fill"]["quantity"])
    tr.engine.bars = [bar(NOW + timedelta(minutes=1), 100, 100.1, 98.8, 98.9)]
    tr.run_once(NOW + timedelta(minutes=2), scan=False)               # managed by the normal loop
    assert forward_test_stats(env.db)["closed_trades"] == 0           # never counted as forward test
    assert broker.closed == ["BTC/USD"]


def test_manual_trade_still_blocked_by_hard_limits(env):
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine())
    tr.quote = lambda sym, now: q()
    nid = _news(env.db)
    bad_rr = manual_trade(tr, "BTC-USD", 99.0, 100.8, nid, now=NOW)  # R:R < 2 after costs
    assert not bad_rr["approved"] and "net_reward_risk" in bad_rr["reason"]
    env.safe.halt("test", module="test")
    assert "safe_state_normal" in manual_trade(tr, "BTC-USD", 99.0, 103.5, nid, now=NOW)["reason"]
    with pytest.raises(ValueError):
        manual_trade(tr, "BTC-USD", 99.0, 103.5, 99999, now=NOW)       # must cite real news
    with pytest.raises(ValueError):
        env.risk.evaluate(proposal(env.db, add_signal(env.db)), q(), NOW, waive=frozenset({"max_open_positions"}))
    assert MANUAL_WAIVABLE == {"strategy_validated_or_forward_test", "score_threshold"}
