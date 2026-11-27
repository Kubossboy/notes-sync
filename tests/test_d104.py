"""D104 adaptive learning: reviews, scorecard (pause / half size / resume), bounded weekly tuning + auto-revert."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import load_settings
from app.learning import params as LP
from app.learning import review, scorecard, tuner

UTC = timezone.utc
NOW = datetime(2026, 11, 29, 3, 0, tzinfo=UTC)


def pos(db, r, i, strategy="momentum_breakout", exit_reason="stop_loss", exit_price=None, table="positions", **kw):
    t0 = NOW - timedelta(hours=100 - i)
    row = {"opened_ts": t0.isoformat(), "closed_ts": (t0 + timedelta(hours=1)).isoformat(), "symbol": f"S{i}-USD",
           "asset_class": "crypto", "strategy": strategy, "entry_price": 100.0, "stop_loss": 99.0, "take_profit": 102,
           "exit_price": exit_price or (100 + r), "status": "closed", "exit_reason": exit_reason, "r_multiple": r,
           "initial_stop": 99.0, "quantity": 1}
    if table == "positions":
        row.update(direction="long", fees_usd=0, slippage_usd=0, leverage=1, realized_pnl_usd=r)
    else:
        sid = db.insert("signals", {"ts": row["opened_ts"], "symbol": row["symbol"], "asset_class": "crypto",
                                    "strategy": strategy, "direction": "long", "status": "executed",
                                    "signal_key": f"t{i}{strategy}"})
        row.update(pnl_usd=r, signal_id=sid, timeframe="15m", validation_status="failed", planned_risk_usd=1.0,
                   entry_fee_usd=0.0)
    row.update(kw)
    return db.insert(table, row)


def test_param_grid_and_hard_rails(tmp_path, monkeypatch):
    assert LP.valid({"swing.rsi_max": 4, "swing.atr_mult": 1.7, "risk.max_risk_per_trade_pct": 5,
                     "risk.lev_max_leverage": 200, "risk.min_reward_risk": 0.5}) == {"swing.rsi_max": 4.0}
    assert LP.valid({"risk.atr_stop_mult": 2.0, "risk.atr_target_mult": 2.0}) == {}        # R:R rail 1.2
    assert LP.neighbours("swing.rsi_max", 5.0) == [4.0, 6.0] and LP.neighbours("swing.rsi_max", 7.0) == [6.0]
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"version": 3, "params": {"swing.rsi_max": 4, "risk.max_daily_loss_pct": 50}}))
    monkeypatch.setenv("ATLAS_PARAMS", str(f))
    s = load_settings()
    assert s.swing.rsi_max == 4
    monkeypatch.delenv("ATLAS_PARAMS")
    assert s.risk.max_daily_loss_pct == load_settings().risk.max_daily_loss_pct            # never learned


def test_decide_needs_in_sample_and_out_of_sample_proof():
    st = lambda n, r: {"n": n, "avg_r": r, "win": 0.6}
    cur = {"is": st(100, 0.10), "oos": st(40, 0.10)}
    good = {("swing.rsi_max", 4.0): {"is": st(90, 0.15), "oos": st(35, 0.20)}}
    assert tuner.decide(cur, good, 30, 0.02)[:2] == ("swing.rsi_max", 4.0)
    assert tuner.decide(cur, {("k", 1.0): {"is": st(90, 0.15), "oos": st(10, 0.5)}}, 30, 0.02)[0] is None   # too few
    assert tuner.decide(cur, {("k", 1.0): {"is": st(90, 0.15), "oos": st(35, 0.11)}}, 30, 0.02)[0] is None  # no OOS gain
    assert tuner.decide(cur, {("k", 1.0): {"is": st(90, 0.05), "oos": st(35, 0.50)}}, 30, 0.02)[0] is None  # IS worse


def test_review_tags_causes_and_lessons(settings, db):
    pos(db, -1.6, 1, exit_price=98.4)                                                    # gapped through 99
    pos(db, -1.0, 2, opened_ts=(NOW - timedelta(minutes=30)).isoformat(), closed_ts=(NOW - timedelta(minutes=25)).isoformat())
    pos(db, 2.0, 3, exit_reason="take_profit")
    pos(db, -1.0, 4, table="shadow_positions", strategy="sr_breakout")
    out = review.review_closed(db, settings, NOW)
    by = {(r["ledger"], r["position_id"]): r for r in out}
    assert "gap" in by[("main", 1)]["causes"] and "noise" in by[("main", 2)]["causes"]
    assert by[("main", 3)]["outcome"] == "win" and by[("main", 3)]["causes"] == ""
    assert ("shadow", 1) in by and review.review_closed(db, settings, NOW) == []            # once per trade

    class FakeLLM:
        def _chat(self, *a, **k):
            return json.dumps({"lesson": "Avoid entries right before the weekend gap."})
    assert review.add_lessons(db, FakeLLM(), 1) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM trade_reviews WHERE lesson IS NOT NULL").fetchone()[0] == 1


def test_scorecard_pauses_halves_and_resumes(settings, db):
    cfg = settings.learning
    for i in range(6):
        pos(db, -1.0, i)
    card = scorecard.update(db, cfg, NOW)
    assert card["strategies"]["momentum_breakout"]["state"] == "paused"
    assert "6 losses in a row" in db.conn.execute("SELECT detail FROM learning_events").fetchone()[0]
    # tracked in shadow after the pause; profitable -> re-enabled and judged fresh
    for i in range(5):
        pos(db, 1.0, 50 + i, exit_reason="take_profit", table="shadow_positions",
            closed_ts=(NOW + timedelta(hours=i + 1)).isoformat())
    card = scorecard.update(db, cfg, NOW + timedelta(hours=6))
    assert card["strategies"]["momentum_breakout"]["state"] == "active"
    # slightly losing over >= min_trades -> half size (never above normal)
    for i in range(8):
        pos(db, 0.5 if i % 2 else -0.7, 60 + i, strategy="sr_breakout",
            closed_ts=(NOW + timedelta(hours=7 + i)).isoformat())
    card = scorecard.update(db, cfg, NOW + timedelta(hours=20))
    assert card["strategies"]["sr_breakout"]["state"] == "reduced" and card["strategies"]["sr_breakout"]["mult"] == 0.5


def test_paused_strategy_is_rejected_and_reduced_size_shrinks_risk(settings, db):
    from app.core.safety import SafeState
    from app.execution.trader import PaperTrader
    from tests.test_risk_execution import NOW as T, add_signal, proposal, q
    s = load_settings(overrides={"paths": {"database": settings.paths.database}, "execution": {"paper_leverage": 1,
                                 "crypto_entry_order": "market"}, "llm": {"enabled": False}})
    t = PaperTrader(s, db, SafeState(db), engine=None)
    t.quote = lambda sym, now: q()
    db.set_state(scorecard.KEY, {"strategies": {"momentum_breakout": {"state": "paused", "why": "6 losses in a row"}}})
    sid = add_signal(db)
    row = db.conn.execute("SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc ON sc.signal_id=s.id "
                          "WHERE s.id=?", (sid,)).fetchone()
    r = t.execute_signal(row, T)
    assert not r["approved"] and r["reason"].startswith("learning: strategy paused")
    import dataclasses
    full = t.risk.evaluate(proposal(db, sid), q(), T)
    half = t.risk.evaluate(dataclasses.replace(proposal(db, sid), risk_mult=0.5), q(), T)
    big = t.risk.evaluate(dataclasses.replace(proposal(db, sid), risk_mult=5.0), q(), T)
    assert half.planned_risk_usd == pytest.approx(full.planned_risk_usd / 2, rel=0.05)
    assert big.planned_risk_usd <= full.planned_risk_usd + 1e-9                          # can never grow


def test_weekly_adopts_one_small_step_then_auto_reverts(settings, db, tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_PARAMS", str(tmp_path / "params.json"))
    st = lambda n, r: {"n": n, "avg_r": r, "win": 0.6}
    crypto = {"current": {"is": st(60, 0.2), "oos": st(25, 0.2)}, "base": {"risk.atr_target_mult": 2.0},
              "_cands": {("risk.atr_target_mult", 2.5): {"is": st(60, 0.3), "oos": st(25, 0.3)}},
              "candidates": {}}
    rep = tuner.weekly(settings, db, None, NOW, groups=("crypto",), crypto_result=crypto)
    doc = LP.load()
    assert rep["groups"]["crypto"]["decision"] == "changed" and doc["version"] == 1
    assert doc["params"] == {"risk.atr_target_mult": 2.5} and "out-of-sample" in doc["history"][0]["reason"]
    assert LP.sentence(doc["history"][0]).startswith("Raised target distance (x ATR) 2→2.5 for crypto strategies")
    for i in range(10):                                                    # live trades after the change: bad
        pos(db, -0.8, i, closed_ts=(NOW + timedelta(days=1, hours=i)).isoformat())
    rep = tuner.weekly(settings, db, None, NOW + timedelta(days=7), groups=("crypto",), crypto_result=crypto)
    doc = LP.load()
    assert rep["reverts"] and doc["params"] == {"risk.atr_target_mult": 2.0} and doc["version"] == 2
    assert doc["history"][-1]["kind"] == "param_revert" and "worse" in doc["history"][-1]["reason"]
    assert "Reverted" in LP.sentence(doc["history"][-1])


def test_weekly_no_change_when_backtest_unavailable(settings, db, tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_PARAMS", str(tmp_path / "p.json"))
    rep = tuner.weekly(settings, db, None, NOW, groups=("rsi2_swing",), swing_data=({}, None))
    assert "no change" in rep["groups"]["rsi2_swing"]["decision"] and LP.load()["params"] == {}


def test_dashboard_learning_card(settings, db, tmp_path, monkeypatch):
    from app.dashboard.live import learning_view
    monkeypatch.setenv("ATLAS_PARAMS", str(tmp_path / "p.json"))
    doc = LP.record(LP.load(), "param_change", "swing.rsi_max", 5.0, 4.0, "it did better out-of-sample", now=NOW)
    LP.save(doc)
    for i in range(6):
        pos(db, -1.0, i)
    scorecard.update(db, settings.learning, NOW)
    review.review_closed(db, settings, NOW)
    v = learning_view(db)
    assert v["changes"][0]["text"].startswith("Lowered RSI threshold 5→4 for Overnight dip-buy")
    assert v["events"][0]["text"].startswith("Paused momentum_breakout after 6 losses in a row")
    assert v["reviewed"] == 6 and v["causes"]
