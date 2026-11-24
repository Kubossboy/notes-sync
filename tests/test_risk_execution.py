"""Milestone 5: deterministic risk engine, paper simulator, ledger, kill switch, Alpaca gate (offline)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Secrets, load_settings
from app.core.safety import SafeState
from app.data.crosscheck import CrossCheck
from app.data.models import AssetClass, Candle, Quote
from app.execution.alpaca_paper import AlpacaOrdersDisabled, AlpacaPaperBroker
from app.execution.costs import CostModel
from app.execution.simulator import DuplicateOrder, PaperSimulator
from app.execution.trader import PaperTrader
from app.portfolio.ledger import Ledger
from app.risk.engine import MarketQuote, Proposal, RiskEngine

UTC = timezone.utc
NOW = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)               # crypto (24/7)
STOCK_NOW = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)        # 11:00 ET regular session


def add_signal(db, symbol="BTC-USD", ac="crypto", entry=100.0, stop=99.0, target=103.5, status="new",
               score=75.0, data_status="live", expires=None, now=NOW, strategy="momentum_breakout"):
    sid = db.insert("signals", {
        "ts": now.isoformat(), "symbol": symbol, "asset_class": ac, "strategy": strategy, "direction": "long",
        "entry_price": entry, "stop_loss": stop, "take_profit": target, "reward_risk": 2.2,
        "data_ts": now.isoformat(), "status": status, "timeframe": "15m",
        "signal_key": f"{symbol}|{strategy}|{now.isoformat()}|{entry}|{status}|{score}",
        "expires_ts": (expires or now + timedelta(minutes=30)).isoformat(), "data_status": data_status})
    db.insert("signal_scores", {"signal_id": sid, "ts": now.isoformat(), "total_score": score, "weights_json": "{}"})
    return sid


def proposal(db, sid):
    r = db.conn.execute("SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc ON sc.signal_id=s.id "
                        "WHERE s.id=?", (sid,)).fetchone()
    return Proposal.from_signal_row(r)


def q(price=100.0, spread=0.02, verdict="agree", ts=NOW, adv=1e7):
    return MarketQuote(price, price - spread / 2, price + spread / 2, ts, verdict, adv)


@pytest.fixture
def env(settings, db):
    safe = SafeState(db)
    led = Ledger(settings, db)
    return SimpleNamespace(s=settings, db=db, safe=safe, led=led, risk=RiskEngine(settings, db, safe, led),
                           sim=PaperSimulator(settings, db, safe, led), cost=CostModel(settings))


def failed(d):
    return {c.name for c in d.failed}


# ---------------------------------------------------------------- sizing
def test_position_sizing_respects_quarter_percent_risk(env):
    sid = add_signal(env.db)
    d = env.risk.evaluate(proposal(env.db, sid), q(), NOW)
    assert d.approved, d.reason
    assert d.risk_budget_usd == pytest.approx(2.5)
    assert 2.3 < d.planned_risk_usd <= 2.5                         # sized to the budget incl. costs
    fill = d.expected_fill
    assert fill > 100.01                                            # ask + slippage
    per_unit = (fill - 99 * (1 - 0.001)) + fill * 0.001 + 99 * 0.999 * 0.001
    assert d.quantity == pytest.approx(2.5 / per_unit, abs=1e-5)
    assert d.notional <= 500 + 1e-6 and d.rr_net >= 2.0
    rows = env.db.conn.execute("SELECT COUNT(*) FROM risk_checks WHERE evaluation_id=?", (d.evaluation_id,)).fetchone()[0]
    assert rows == len(d.checks) >= 20                              # every rule journaled


def test_risk_scales_with_current_equity(env):
    # a prior losing trade lowers equity -> lower budget next time
    env.db.insert("positions", {"opened_ts": NOW.isoformat(), "closed_ts": NOW.isoformat(), "symbol": "ETH-USD",
                                "asset_class": "crypto", "direction": "long", "quantity": 1, "entry_price": 10,
                                "stop_loss": 9, "take_profit": 13, "realized_pnl_usd": -200.0, "status": "closed",
                                "trading_day_closed": "2026-11-01"})
    d = env.risk.evaluate(proposal(env.db, add_signal(env.db)), q(), NOW)
    assert d.risk_budget_usd == pytest.approx(800 * 0.0025)


def test_position_cap_when_stop_is_very_tight(env):
    # tiny stop distance -> risk sizing would exceed 50 % of equity -> capped, so risk < budget
    sid = add_signal(env.db, entry=100, stop=99.8, target=100.8)
    d = env.risk.evaluate(proposal(env.db, sid), q(spread=0.0), NOW)
    assert d.notional <= 500 + 1e-6 and d.planned_risk_usd < 2.5


# ---------------------------------------------------------------- rejections
@pytest.mark.parametrize("kw,check", [
    ({"status": "incomplete"}, "signal_complete"),
    ({"status": "rejected"}, "signal_complete"),
    ({"score": 65}, "score_threshold"),
    ({"stop": 100.5}, "stop_loss_present_valid"),
    ({"stop": None}, "stop_loss_present_valid"),
    ({"target": 99.5}, "take_profit_present_valid"),
    ({"data_status": "delayed"}, "data_live"),
    ({"expires": NOW - timedelta(minutes=1)}, "signal_not_expired"),
    ({"target": 102.5}, "net_reward_risk"),   # 2.5 R gross is < 2 R net with crypto costs
])
def test_rule_rejections(env, kw, check):
    d = env.risk.evaluate(proposal(env.db, add_signal(env.db, **kw)), q(), NOW)
    assert not d.approved and check in failed(d), d.reason


def test_unreliable_or_stale_quote_rejected(env):
    sid = add_signal(env.db)
    assert "quote_reliable" in failed(env.risk.evaluate(proposal(env.db, sid), q(verdict="divergent"), NOW))
    assert "quote_reliable" in failed(env.risk.evaluate(proposal(env.db, sid), None, NOW))
    assert "quote_fresh" in failed(env.risk.evaluate(proposal(env.db, sid), q(ts=NOW - timedelta(minutes=5)), NOW))


def test_chasing_and_price_below_stop(env):
    sid = add_signal(env.db)
    assert "no_chasing_entry_drift" in failed(env.risk.evaluate(proposal(env.db, sid), q(price=100.4), NOW))
    assert "price_not_below_stop" in failed(env.risk.evaluate(proposal(env.db, sid), q(price=98.5), NOW))


def test_safe_state_blocks(env):
    env.safe.halt("test")
    d = env.risk.evaluate(proposal(env.db, add_signal(env.db)), q(), NOW)
    assert "safe_state_normal" in failed(d)


def test_stock_session_rules(env):
    sid = add_signal(env.db, "NVDA", "stock", now=STOCK_NOW)
    p = proposal(env.db, sid)
    assert env.risk.evaluate(p, q(ts=STOCK_NOW), STOCK_NOW).approved
    pre = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)                 # 08:00 ET
    assert "stock_regular_session" in failed(env.risk.evaluate(p, q(ts=pre), pre))
    late = datetime(2026, 11, 24, 20, 50, tzinfo=UTC)               # 15:50 ET
    d = env.risk.evaluate(p, q(ts=late), late)
    assert "stock_entry_window" in failed(d)


def test_max_positions_duplicate_and_same_symbol(env):
    s1 = add_signal(env.db, "BTC-USD")
    d1 = env.risk.evaluate(proposal(env.db, s1), q(), NOW)
    env.sim.open_position(proposal(env.db, s1), d1, q(), NOW)
    # same signal again -> duplicate; same symbol -> no pyramiding
    s1b = add_signal(env.db, "BTC-USD", entry=100.001)
    assert "no_duplicate_order_for_signal" in failed(env.risk.evaluate(proposal(env.db, s1), q(), NOW))
    assert "no_existing_position_in_symbol" in failed(env.risk.evaluate(proposal(env.db, s1b), q(), NOW))
    s2 = add_signal(env.db, "ETH-USD")
    env.sim.open_position(proposal(env.db, s2), env.risk.evaluate(proposal(env.db, s2), q(), NOW), q(), NOW)
    s3 = add_signal(env.db, "SOL-USD")
    assert "max_open_positions" in failed(env.risk.evaluate(proposal(env.db, s3), q(), NOW))


def test_simulator_refuses_unapproved_and_duplicates(env):
    sid = add_signal(env.db)
    p = proposal(env.db, sid)
    bad = env.risk.evaluate(proposal(env.db, add_signal(env.db, status="incomplete")), q(), NOW)
    with pytest.raises(ValueError):
        env.sim.open_position(p, bad, q(), NOW)
    d = env.risk.evaluate(p, q(), NOW)
    env.sim.open_position(p, d, q(), NOW)
    with pytest.raises(DuplicateOrder):
        env.sim.open_position(p, d, q(), NOW)                        # replay of the same approval
    assert env.db.conn.execute("SELECT status FROM signals WHERE id=?", (sid,)).fetchone()[0] == "executed"


def test_risk_engine_failure_enters_safe_state(env, monkeypatch):
    monkeypatch.setattr(env.led, "state", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = env.risk.evaluate(proposal(env.db, add_signal(env.db)), q(), NOW)
    assert not d.approved and not env.safe.trading_allowed()
    assert "risk engine failure" in env.safe.status().reason


# ---------------------------------------------------------------- management, fills, P&L
def bar(t, o, h, l, c, complete=True, sym="BTC-USD", ac=AssetClass.CRYPTO):
    return Candle(sym, ac, "fake", "1m", t, o, h, l, c, 10.0, is_complete=complete)


def open_btc(env, **kw):
    sid = add_signal(env.db, **kw)
    p = proposal(env.db, sid)
    d = env.risk.evaluate(p, q(), NOW)
    assert d.approved, d.reason
    env.sim.open_position(p, d, q(), NOW)
    return env.led.open_positions()[0], d


def test_stop_loss_exit_loses_about_planned_risk(env):
    pos, d = open_btc(env)
    bars = [bar(NOW + timedelta(minutes=1), 100, 100.2, 99.5, 99.6),
            bar(NOW + timedelta(minutes=2), 99.6, 99.7, 98.9, 99.0, complete=False)]   # forming bar counts
    r = env.sim.manage_position(pos, bars, NOW + timedelta(minutes=2, seconds=30))
    assert r["reason"] == "stop_loss"
    assert r["exit_price"] == pytest.approx(99 * (1 - 0.001))
    assert r["pnl"] == pytest.approx(-d.planned_risk_usd, rel=0.02)    # loss ≈ planned risk incl. costs
    assert r["r_multiple"] == pytest.approx(-1, abs=0.03)


def test_gap_through_stop_fills_at_open(env):
    pos, d = open_btc(env)
    r = env.sim.manage_position(pos, [bar(NOW + timedelta(minutes=1), 97, 97.5, 96.5, 97)], NOW + timedelta(minutes=2))
    assert r["exit_price"] == pytest.approx(97 * 0.999) and r["pnl"] < -d.planned_risk_usd   # gap risk is real


def test_take_profit_and_same_bar_conflict(env):
    pos, _ = open_btc(env)
    r = env.sim.manage_position(pos, [bar(NOW + timedelta(minutes=1), 100.5, 103.6, 100.4, 103.5)], NOW + timedelta(minutes=2))
    assert r["reason"] == "take_profit" and r["exit_price"] == 103.5 and r["pnl"] > 0
    pos2, _ = open_btc(env, symbol="ETH-USD")
    r2 = env.sim.manage_position(pos2, [bar(NOW + timedelta(minutes=1), 100, 104, 98.5, 101, sym="ETH-USD")],
                                 NOW + timedelta(minutes=2))
    assert r2["reason"] == "stop_loss"                                   # conservative: stop first


def test_target_touch_without_trading_through_does_not_fill(env):
    pos, _ = open_btc(env)
    r = env.sim.manage_position(pos, [bar(NOW + timedelta(minutes=1), 100, 103.5, 99.8, 103.4)], NOW + timedelta(minutes=2))
    assert r["status"] == "open" and r["mark"] == 103.4


def test_bars_before_entry_are_ignored_and_mark_to_market(env):
    pos, _ = open_btc(env)
    r = env.sim.manage_position(pos, [bar(NOW - timedelta(minutes=5), 100, 100, 90, 95),
                                      bar(NOW + timedelta(minutes=1), 100, 100.6, 99.9, 100.5)], NOW + timedelta(minutes=2))
    assert r["status"] == "open"
    st = env.led.state(NOW + timedelta(minutes=2))
    p = st.open_positions[0]
    assert p["mark_price"] == 100.5 and p["unrealized_pnl_usd"] > 0
    assert st.equity == pytest.approx(st.cash + st.positions_value)
    assert st.equity == pytest.approx(1000 + st.realized_total + st.unrealized)


def test_time_exits(env):
    pos, _ = open_btc(env)
    later = NOW + timedelta(hours=25)
    r = env.sim.manage_position(pos, [], later, q(price=100.2, ts=later))
    assert r["reason"] == "max_hold_time"
    sid = add_signal(env.db, "NVDA", "stock", now=STOCK_NOW)
    p = proposal(env.db, sid)
    env.sim.open_position(p, env.risk.evaluate(p, q(ts=STOCK_NOW), STOCK_NOW), q(ts=STOCK_NOW), STOCK_NOW)
    spos = [x for x in env.led.open_positions() if x["symbol"] == "NVDA"][0]
    eod = datetime(2026, 11, 24, 20, 56, tzinfo=UTC)                     # 15:56 ET
    assert env.sim.manage_position(spos, [], eod, q(ts=eod))["reason"] == "eod_flatten"


def test_daily_loss_limit_and_consecutive_losses(env):
    # three stop-outs in a day -> consecutive-loss stop; losses tracked against day-start equity
    for sym in ("BTC-USD", "ETH-USD", "SOL-USD"):
        pos, _ = open_btc(env, symbol=sym)
        env.sim.manage_position(pos, [bar(NOW + timedelta(minutes=1), 99, 99, 98, 98.5, sym=sym)], NOW + timedelta(minutes=2))
    st = env.led.state(NOW + timedelta(minutes=3))
    assert st.consecutive_losses_today == 3 and 0 < st.daily_loss < 10
    d = env.risk.evaluate(proposal(env.db, add_signal(env.db, "AVAX-USD")), q(), NOW + timedelta(minutes=3))
    assert {"consecutive_losses_today"} <= failed(d)
    assert "symbol_cooldown_after_loss" in failed(env.risk.evaluate(proposal(env.db, add_signal(env.db, "BTC-USD", score=76)),
                                                                   q(), NOW + timedelta(minutes=3)))


def test_daily_loss_limit_blocks_new_entries(env):
    env.db.set_state("day_start_equity:2026-11-24", 1000.0)
    env.db.insert("positions", {"opened_ts": NOW.isoformat(), "closed_ts": NOW.isoformat(), "symbol": "X-USD",
                                "asset_class": "crypto", "direction": "long", "quantity": 1, "entry_price": 10,
                                "stop_loss": 9, "take_profit": 13, "realized_pnl_usd": -10.0, "status": "closed",
                                "trading_day_closed": "2026-11-24"})
    d = env.risk.evaluate(proposal(env.db, add_signal(env.db)), q(), NOW)
    assert {"daily_loss_limit_not_reached", "position_size_positive"} <= failed(d)   # no budget left
    # next trading day: limit resets against the new day-start equity
    nxt = NOW + timedelta(days=1)
    d2 = env.risk.evaluate(proposal(env.db, add_signal(env.db, now=nxt, expires=nxt + timedelta(minutes=5))),
                           q(ts=nxt), nxt)
    assert "daily_loss_limit_not_reached" not in failed(d2)
    assert env.db.get_state("day_start_equity:2026-11-25") == pytest.approx(990.0)


# ---------------------------------------------------------------- audit trail
def test_audit_tables_append_only(env):
    pos, _ = open_btc(env)
    with pytest.raises(Exception):
        env.db.conn.execute("DELETE FROM order_requests")
    with pytest.raises(Exception):
        env.db.conn.execute("UPDATE simulated_fills SET fill_price=1")
    env.sim.manage_position(pos, [bar(NOW + timedelta(minutes=1), 99, 99, 98, 98.5)], NOW + timedelta(minutes=2))
    with pytest.raises(Exception):
        env.db.conn.execute("UPDATE positions SET status='open' WHERE id=?", (pos["id"],))
    with pytest.raises(Exception):
        env.db.conn.execute("DELETE FROM risk_evaluations")
    with pytest.raises(Exception):   # DB guard: second open position in the same symbol
        env.db.insert("positions", {"opened_ts": NOW.isoformat(), "symbol": "ETH-USD", "asset_class": "crypto",
                                    "direction": "long", "quantity": 1, "entry_price": 1, "stop_loss": 0.9,
                                    "take_profit": 1.5, "status": "open"}) and \
            env.db.insert("positions", {"opened_ts": NOW.isoformat(), "symbol": "ETH-USD", "asset_class": "crypto",
                                        "direction": "long", "quantity": 1, "entry_price": 1, "stop_loss": 0.9,
                                        "take_profit": 1.5, "status": "open"})
    n = env.db.conn.execute("SELECT COUNT(*) FROM simulated_fills").fetchone()[0]
    assert n == 2


# ---------------------------------------------------------------- trader + kill switch
class FakeEngine:
    def __init__(self, price=100.0, bars=None, fail_candles=False):
        self.price, self.bars, self.fail = price, bars or [], fail_candles

    def get_quote_consensus(self, symbol):
        cc = CrossCheck(symbol, AssetClass.CRYPTO, "quote", {"coinbase": self.price}, self.price, verdict="agree")
        qq = Quote(symbol, AssetClass.CRYPTO, "coinbase", self.price, NOW, self.price - 0.01, self.price + 0.01)
        return cc, [qq], []

    def get_candles(self, symbol, tf, limit, crosscheck=True, now=None):
        if self.fail:
            raise RuntimeError("feed down")
        return SimpleNamespace(series=SimpleNamespace(candles=self.bars, complete=[b for b in self.bars if b.is_complete]))


def test_trader_executes_complete_signal_and_manages(env):
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine())
    sid = add_signal(env.db)
    row = env.db.conn.execute("SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc ON sc.signal_id=s.id "
                              "WHERE s.id=?", (sid,)).fetchone()
    res = tr.execute_signal(row, NOW)
    assert res["approved"] and res["fill"]["quantity"] > 0
    tr.engine.bars = [bar(NOW + timedelta(minutes=1), 100, 100.1, 98.8, 98.9)]
    rep = tr.run_once(NOW + timedelta(minutes=2), scan=False)
    assert rep["managed"][0]["reason"] == "stop_loss"
    assert rep["portfolio"]["open_positions"] == 0 and rep["portfolio"]["equity"] < 1000
    assert env.db.conn.execute("SELECT COUNT(*) FROM equity_snapshots WHERE note='cycle'").fetchone()[0] == 1


def test_trader_rejects_incomplete_signal_and_journals(env):
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine())
    sid = add_signal(env.db, status="incomplete")
    row = env.db.conn.execute("SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc ON sc.signal_id=s.id "
                              "WHERE s.id=?", (sid,)).fetchone()
    res = tr.execute_signal(row, NOW)
    assert not res["approved"] and "signal_complete" in res["reason"]
    assert env.db.conn.execute("SELECT decision FROM strategy_decisions WHERE signal_id=?", (sid,)).fetchone()[0] == "reject"
    assert tr.candidates([]) == []


def test_manage_failure_halts_new_trades(env):
    open_btc(env)
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine(fail_candles=True))
    rep = tr.run_once(NOW + timedelta(minutes=1), scan=False)
    assert "error" in rep["managed"][0] and not env.safe.trading_allowed()
    assert len(env.led.open_positions()) == 1                          # position kept, retried next cycle


def test_kill_switch_flattens(env):
    open_btc(env)
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine(price=100.3))
    out = tr.kill("operator test", flatten=True, now=NOW + timedelta(minutes=5))
    assert out["halted"] and out["closed"][0]["reason"] == "kill_switch"
    assert not env.safe.trading_allowed() and env.led.open_positions() == []


# ---------------------------------------------------------------- Alpaca paper gate
class RecSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append((method, url, kw))
        return SimpleNamespace(status_code=200, text='{"id":"x","status":"accepted"}',
                               json=lambda: {"id": "x", "status": "accepted"})


def test_alpaca_orders_disabled_by_default(settings):
    assert settings.execution.alpaca_paper_orders_enabled is False
    sess = RecSession()
    b = AlpacaPaperBroker(settings, Secrets(alpaca_api_key_id="k", alpaca_api_secret_key="s"), sess)
    with pytest.raises(AlpacaOrdersDisabled):
        b.submit_bracket("NVDA", 1, 99, 103, "c1")
    assert sess.calls == []


def test_alpaca_bracket_goes_to_paper_endpoint_only_when_enabled(tmp_path):
    s = load_settings(overrides={"paths": {"database": str(tmp_path / "x.db"), "log_dir": str(tmp_path)},
                                 "execution": {"alpaca_paper_orders_enabled": True}})
    sess = RecSession()
    b = AlpacaPaperBroker(s, Secrets(alpaca_api_key_id="k", alpaca_api_secret_key="s"), sess)
    b.submit_bracket("NVDA", 1.5, 99, 103, "c1")
    m, url, kw = sess.calls[0]
    assert m == "POST" and url == "https://paper-api.alpaca.markets/v2/orders"
    assert kw["json"]["order_class"] == "bracket" and kw["json"]["stop_loss"]["stop_price"] == "99.00"
    assert not AlpacaPaperBroker(s, Secrets(), sess).orders_enabled     # no keys -> off
