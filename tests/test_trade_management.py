"""D92 trade management + D93 looser forward-test entry gates."""
import sqlite3
from datetime import timedelta

import pytest

from app.data.models import AssetClass, Candle
from app.execution.costs import CostModel
from app.execution.management import decide
from tests.test_risk_execution import NOW, add_signal, env, open_btc, proposal, q  # noqa: F401


def bars(closes, start=NOW - timedelta(hours=10), rng=0.3):
    return [Candle("BTC-USD", AssetClass.CRYPTO, "t", "15m", start + timedelta(minutes=15 * i), c, c + rng, c - rng, c, 1.0)
            for i, c in enumerate(closes)]


def pos(**kw):
    p = {"asset_class": "crypto", "entry_price": 100.0, "stop_loss": 99.0, "take_profit": 103.0, "initial_stop": 99.0,
         "initial_target": 103.0, "quantity": 2.5, "entry_fee": 0.025, "opened_ts": (NOW - timedelta(hours=8)).isoformat()}
    p.update(kw)
    return p


NONEWS = {"n": 0, "mean": None, "hard": []}


def test_breakeven_then_trail_and_never_widen(settings):
    c = CostModel(settings)
    up = [100 + i * 0.05 for i in range(40)]                 # steady uptrend to ~102
    acts = decide(pos(), bars(up), 101.1, NONEWS, c, NOW)
    st = [a for a in acts if a.kind == "stop"]
    assert st and st[0].new > 100.0 and st[0].rule == "breakeven"        # +1.1 R -> breakeven + costs
    acts = decide(pos(), bars(up), 102.0, NONEWS, c, NOW)
    st = [a for a in acts if a.kind == "stop"]
    assert st and st[0].new > 100.0
    # a stop already above any candidate is never lowered
    for px in (99.2, 100.5, 101.1, 102.0):
        for a in decide(pos(stop_loss=101.5), bars(up), px, NONEWS, c, NOW):
            assert a.kind != "stop" or a.new > 101.5


def test_db_forbids_widening_target_extension_and_adding(env):
    open_btc(env)
    pid = env.db.conn.execute("SELECT id FROM positions").fetchone()[0]
    with pytest.raises(sqlite3.DatabaseError):
        env.db.conn.execute("UPDATE positions SET stop_loss=stop_loss-1 WHERE id=?", (pid,))
    with pytest.raises(sqlite3.DatabaseError):
        env.db.conn.execute("UPDATE positions SET take_profit=take_profit+1 WHERE id=?", (pid,))
    with pytest.raises(sqlite3.DatabaseError):
        env.db.conn.execute("UPDATE positions SET quantity=quantity*2 WHERE id=?", (pid,))
    env.db.conn.execute("UPDATE positions SET stop_loss=stop_loss+0.1 WHERE id=?", (pid,))     # tightening is fine


def test_target_shrinks_on_resistance_but_never_above_original(settings):
    c = CostModel(settings)
    cl = [100 + (0.8 if i == 30 else 0) + i * 0.005 for i in range(40)]  # pivot high ~101.4 below the 103 target
    acts = decide(pos(), bars(cl), 100.3, NONEWS, c, NOW)
    tg = [a for a in acts if a.kind == "target"]
    assert tg and tg[0].new < 103.0 and tg[0].rule == "resistance"
    for a in decide(pos(take_profit=101.0), bars(cl), 100.3, NONEWS, c, NOW):
        assert a.kind != "target" or a.new < 101.0


def test_early_exits(settings):
    c = CostModel(settings)
    flat = [100.0] * 40
    hard = {"n": 1, "mean": -0.8, "hard": ["exchange_hack: X hacked"]}
    assert decide(pos(), bars(flat), 99.5, hard, c, NOW)[0].rule == "hard_negative_catalyst"   # losing: hard only
    soft = {"n": 3, "mean": -0.7, "hard": []}
    assert all(a.kind != "exit" for a in decide(pos(), bars(flat), 99.5, soft, c, NOW))       # losing + soft -> hold
    assert decide(pos(), bars(flat), 100.8, soft, c, NOW)[0].rule == "adverse_news"            # profit + strong news
    down = [101 + i * 0.02 for i in range(30)] + [101.6 - i * 0.12 for i in range(10)]
    acts = decide(pos(), bars(down), 100.6, NONEWS, c, NOW)
    assert acts and acts[0].rule == "momentum_reversal"


def test_forward_test_entry_gates_loosened_but_others_not(env):
    sid = add_signal(env.db, strategy="news_momentum", validation_status="forward_test", score=62.0, target=103.0)
    d = env.risk.evaluate(proposal(env.db, sid), q(), NOW)
    assert d.approved, d.reason                                       # score 62 >= 60, net R:R ~1.7 >= 1.5
    sid2 = add_signal(env.db, symbol="ETH-USD", score=62.0, target=103.0)    # validated strategy: old gates (70 / 2.0)
    d2 = env.risk.evaluate(proposal(env.db, sid2), q(), NOW)
    assert not d2.approved and "score_threshold" in d2.reason
    assert env.s.risk.min_reward_risk == 2.0 and env.s.scoring.min_score_to_trade == 70
