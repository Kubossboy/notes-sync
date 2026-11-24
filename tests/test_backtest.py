"""Milestone 6: backtest engine, validation protocol and quant hookup."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import NOT_BACKTESTABLE, Backtester, Candidate, Trade, metrics, variants_for
from app.backtest.validation import _persist, params_hash, strategy_params, verdict
from app.data.models import AssetClass
from app.quant.engine import COST_KEYS, QuantEngine
from app.technical.indicators import compute_all
from tests.test_scanner import NOW, make

UTC = timezone.utc


def walk(n=900, seed=3, tf_min=15, start=datetime(2026, 1, 5, tzinfo=UTC)):
    r = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(r.normal(0, 0.004, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + r.uniform(0, 0.003, n))
    lo = np.minimum(o, c) * (1 - r.uniform(0, 0.003, n))
    idx = pd.DatetimeIndex([start + timedelta(minutes=tf_min * i) for i in range(n)], name="t")
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": r.uniform(500, 1500, n)}, index=idx)


def frame(rows, start=datetime(2026, 3, 2, 0, 0, tzinfo=UTC), tf_min=15):
    idx = pd.DatetimeIndex([start + timedelta(minutes=tf_min * i) for i in range(len(rows))], name="t")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1000.0, trend="up")


# ------------------------------------------------------------------ causality (no look-ahead)
@pytest.mark.parametrize("k", [120, 333, 600, 899])
def test_indicators_are_causal(k):
    raw = walk()
    full = compute_all(raw, AssetClass.CRYPTO).iloc[:k]
    part = compute_all(raw.iloc[:k], AssetClass.CRYPTO)
    for col in part.columns:
        a, b = full[col], part[col]
        if a.dtype.kind in "fc":
            assert np.allclose(a.to_numpy(float), b.to_numpy(float), equal_nan=True, rtol=1e-9, atol=1e-12), col
        else:
            assert (a.isna() == b.isna()).all() and (a[a.notna()].astype(str) == b[b.notna()].astype(str)).all(), col


def test_candidates_only_see_the_past(settings):
    """Changing bars AFTER bar i must not change the candidates generated at or before bar i."""
    bt = Backtester(settings)
    raw = walk(700, seed=5)
    params = strategy_params(settings, AssetClass.CRYPTO)
    st = {n: {"default": params[n]} for n in ("momentum_breakout", "trend_continuation", "volume_expansion")}
    base = bt.candidates(bt.prepare(raw, AssetClass.CRYPTO), "X", AssetClass.CRYPTO, "15m", st, warmup=250,
                         diag_min_rr=1.0)
    cut = 500
    alt = raw.copy()
    alt.iloc[cut + 1:, :4] *= 1.3                      # wild future
    other = bt.candidates(bt.prepare(alt, AssetClass.CRYPTO), "X", AssetClass.CRYPTO, "15m", st, warmup=250,
                          diag_min_rr=1.0)
    key = lambda cs: [(c.strategy, c.bar_i, round(c.entry, 9), round(c.stop, 9)) for c in cs if c.bar_i <= cut]
    assert key(base) == key(other)


def test_variants_for_moves_thresholds_in_the_right_direction():
    v = variants_for({"donchian": 48.0, "min_rvol": 1.5, "rsi_max": 80.0, "rsi_min": 40.0, "target_r": 2.5,
                      "bars": 2.0, "min_touches": 2.0, "or_minutes": 15.0, "flag": True})
    lo, st = v["loose"], v["strict"]
    assert lo["donchian"] == 38 and st["donchian"] == 58               # shorter channel = more breakouts
    assert lo["min_rvol"] == pytest.approx(1.2) and st["min_rvol"] == pytest.approx(1.8)
    assert lo["rsi_max"] == pytest.approx(96) and st["rsi_max"] == pytest.approx(64)
    assert lo["rsi_min"] == pytest.approx(32) and st["rsi_min"] == pytest.approx(48)
    assert lo["bars"] == 1 and st["bars"] == 3                          # integer params move by >= 1
    assert lo["min_touches"] == 2 and st["min_touches"] == 3            # floor: a level needs 2 touches
    assert lo["target_r"] == st["target_r"] == 2.5 and lo["or_minutes"] == 15.0 and st["flag"] is True


# ------------------------------------------------------------------ fills / exits (same cost model as live)
def test_exit_stop_first_when_both_hit(settings):
    bt = Backtester(settings)
    ind = frame([(100, 100.5, 99.5, 100), (100, 101, 99.8, 100.5), (100.5, 106, 97, 100)])
    c = Candidate("X", "s", "default", 0, ind.index[0], 100, 98, 104)
    fill, k, x, why = bt.exit_path(ind, c, AssetClass.CRYPTO, 900)
    assert fill.price == pytest.approx(bt.costs.buy_fill(AssetClass.CRYPTO, 100).price)
    assert (k, why) == (2, "stop_loss") and x.price <= 98


def test_gap_through_stop_fills_at_open_and_gap_entry_rejected(settings):
    bt = Backtester(settings)
    ind = frame([(100, 100.5, 99.5, 100), (100, 101, 99.8, 100.5), (96, 96.5, 95, 96)])
    c = Candidate("X", "s", "default", 0, ind.index[0], 100, 98, 104)
    _, _, x, why = bt.exit_path(ind, c, AssetClass.CRYPTO, 900)
    assert why == "stop_loss" and x.price <= 96          # gap: worse than the stop
    ind2 = frame([(100, 100.5, 99.5, 100), (97.5, 98, 97, 97.5)])
    assert bt.exit_path(ind2, c, AssetClass.CRYPTO, 900) is None          # opens below the stop
    ind3 = frame([(100, 100.5, 99.5, 100), (101, 101.5, 100.8, 101)])
    assert bt.exit_path(ind3, c, AssetClass.CRYPTO, 900) is None          # chasing: 0.5 R > max drift


def test_target_and_crypto_max_hold(settings):
    bt = Backtester(settings)
    ind = frame([(100, 100.5, 99.5, 100), (100, 101, 99.8, 100.5), (100.5, 104.5, 100, 104)])
    c = Candidate("X", "s", "default", 0, ind.index[0], 100, 98, 104)
    _, k, x, why = bt.exit_path(ind, c, AssetClass.CRYPTO, 900)
    assert why == "take_profit" and x.price <= 104
    flat = frame([(100, 100.3, 99.7, 100)] * 200)
    _, k, _, why = bt.exit_path(flat, c, AssetClass.CRYPTO, 900)
    assert why == "max_hold_time" and (flat.index[k] + timedelta(minutes=15) - flat.index[1]) >= timedelta(hours=24)


def test_stock_eod_flatten_and_entry_window(settings):
    bt = Backtester(settings)
    start = datetime(2026, 3, 3, 14, 30, tzinfo=UTC)        # 09:30 ET (EST)
    rows = [(100, 100.3, 99.8, 100)] * 78
    ind = frame(rows, start=start, tf_min=5)
    c = Candidate("X", "s", "default", 10, ind.index[10], 100, 99, 102.5)
    _, k, _, why = bt.exit_path(ind, c, AssetClass.STOCK, 300)
    assert why == "eod_flatten" and ind.index[k] + timedelta(minutes=5) >= datetime(2026, 3, 3, 20, 55, tzinfo=UTC)
    late = Candidate("X", "s", "default", 74, ind.index[74], 100, 99, 102.5)   # entry 15:50 ET
    assert bt.exit_path(ind, late, AssetClass.STOCK, 300) is None


# ------------------------------------------------------------------ portfolio replay
def test_simulate_portfolio_rules(settings):
    bt = Backtester(settings)
    rows = [(100, 100.3, 99.7, 100)] * 10 + [(100, 100.2, 96, 97)] + [(100, 100.3, 99.7, 100)] * 60
    data = {s: frame(rows) for s in ("A", "B", "C")}
    t = data["A"].index
    cands = [Candidate("A", "s", "default", 5, t[5], 100, 98, 106),
             Candidate("A", "s", "default", 6, t[6], 100, 98, 106),      # same symbol already open
             Candidate("B", "s", "default", 6, t[6], 100, 98, 106),
             Candidate("C", "s", "default", 7, t[7], 100, 98, 106),      # max 2 open positions
             Candidate("A", "s", "default", 12, t[12], 100, 98, 106)]    # 45 min after the loss at bar 10: cooldown
    tr = bt.simulate(data, cands, AssetClass.CRYPTO, 900)
    assert [(x.symbol, x.entry_ts) for x in tr] == [("A", t[6]), ("B", t[7])]
    assert all(x.exit_reason == "stop_loss" and x.pnl < 0 for x in tr)
    risk = settings.portfolio.starting_equity_usd * settings.risk.max_risk_per_trade_pct / 100
    assert all(x.planned_risk <= risk + 1e-9 for x in tr)
    cands.append(Candidate("A", "s", "default", 16, t[16], 100, 98, 106))   # 90 min after loss: allowed
    assert any(x.entry_ts == t[17] for x in bt.simulate(data, cands, AssetClass.CRYPTO, 900))


def test_simulate_rejects_low_net_rr(settings):
    bt = Backtester(settings)
    data = {"A": frame([(100, 100.3, 99.7, 100)] * 30)}
    c = Candidate("A", "s", "default", 5, data["A"].index[5], 100, 98, 103)   # 1.5 R gross -> < 2 net
    assert bt.simulate(data, [c], AssetClass.CRYPTO, 900) == []


def _trade(pnl, day, r=None):
    t = pd.Timestamp(datetime(2026, 1, day, 12, tzinfo=UTC))
    return Trade("A", "s", "default", t, t, t + timedelta(hours=1), 100, 101, 99, 103, 1, 2.5, 0.2, pnl,
                 r if r is not None else pnl / 2.5, "x", 1000)


def test_metrics_math():
    m = metrics([_trade(5, 1), _trade(-2.5, 2), _trade(-2.5, 3), _trade(10, 4)])
    assert m["trades"] == 4 and m["win_rate"] == 0.5
    assert m["profit_factor"] == pytest.approx(3.0)
    assert m["expectancy_usd"] == pytest.approx(2.5) and m["expectancy_r"] == pytest.approx(1.0)
    assert m["net_return_pct"] == pytest.approx(1.0)
    assert m["max_drawdown_pct"] == pytest.approx(5 / 1005 * 100, rel=1e-3)
    assert metrics([]) == {"trades": 0}


# ------------------------------------------------------------------ verdict protocol
def _res(oos_n=40, pf=1.6, exp=0.2, dd=2.0, folds=(1, 1, 1, -1, 1), stress_pf=1.2, loose=1.2, strict=1.1):
    m = lambda n, p, e=0.1, d=1.0: {"trades": n, "profit_factor": p, "expectancy_r": e, "max_drawdown_pct": d}
    fl = [{"test": {"trades": 5, "net_pnl_usd": x}} for x in folds]
    pos = sum(x > 0 for x in folds)
    return {"out_of_sample": m(oos_n, pf, exp, dd), "stress_oos": m(oos_n, stress_pf),
            "sensitivity_oos": {"loose": m(30, loose), "strict": m(30, strict)},
            "walk_forward": {"folds": fl, "folds_with_trades": len(folds), "positive_folds": pos,
                             "positive_fraction": pos / len(folds)}}


def test_verdicts(settings):
    assert verdict(settings, _res())[0] == "validated"
    assert verdict(settings, _res(oos_n=12))[0] == "insufficient_data"
    v, why = verdict(settings, _res(pf=1.1))
    assert v == "failed" and any("profit factor" in w for w in why)
    assert verdict(settings, _res(exp=-0.01))[0] == "failed"
    assert verdict(settings, _res(dd=6))[0] == "failed"
    assert verdict(settings, _res(folds=(1, -1, -1, -1, 1)))[0] == "failed"
    assert verdict(settings, _res(stress_pf=0.95))[0] == "failed"
    v, why = verdict(settings, _res(loose=0.8))
    assert v == "failed" and any("fragile" in w for w in why)
    assert settings.validation.auto_deploy_params is False


# ------------------------------------------------------------------ persistence + quant hookup
def _cost(settings):
    return {k: getattr(settings.execution, k) for k in COST_KEYS}


def _store(settings, db, name, verdict_, reasons, params=None, ts=None, res=None):
    res = dict(res or _res(), verdict=verdict_, reasons=reasons, split_ts="2026-06-01T00:00:00+00:00")
    t0 = pd.Timestamp("2025-01-01", tz="UTC")
    rid = _persist(db, "b1", AssetClass.CRYPTO, name, "coinbase", "15m", ["BTC-USD"], t0, t0, res,
                   {"out_of_sample": [_trade(5, 2)]},
                   params if params is not None else strategy_params(settings, AssetClass.CRYPTO)[name],
                   _cost(settings), "h", "test")
    if ts:
        db.conn.execute("DROP TRIGGER trg_no_update_backtest_runs")
        db.conn.execute("UPDATE backtest_runs SET ts=? WHERE id=?", (ts, rid))
    return rid


def test_backtest_tables_append_only(settings, db):
    import sqlite3
    rid = _store(settings, db, "momentum_breakout", "failed", ["x"])
    assert db.conn.execute("SELECT COUNT(*) FROM backtest_trades WHERE run_id=?", (rid,)).fetchone()[0] == 1
    for sql in ("DELETE FROM backtest_runs", "UPDATE backtest_runs SET verdict='validated'",
                "DELETE FROM backtest_trades", "UPDATE backtest_trades SET pnl_usd=1"):
        with pytest.raises(sqlite3.DatabaseError):
            db.conn.execute(sql)


def test_quant_states(settings, db):
    q = QuantEngine(settings, db)
    ac = AssetClass.CRYPTO
    assert q.status(ac, "momentum_breakout")["state"] == "unavailable"
    assert "not backtestable" in q.status(ac, "orderbook_imbalance")["reason"]
    _store(settings, db, "momentum_breakout", "failed", ["OOS profit factor 0.8 < 1.3"])
    _store(settings, db, "volume_expansion", "validated", ["ok"])
    _store(settings, db, "trend_continuation", "validated", ["ok"], params={"changed": 1})
    _store(settings, db, "sr_breakout", "validated", ["ok"], ts="2026-01-01T00:00:00+00:00")
    _store(settings, db, "volatility_expansion", "insufficient_data", ["only 5 trades"])
    q = QuantEngine(settings, db)
    assert q.status(ac, "momentum_breakout")["state"] == "failed"
    assert q.status(ac, "volume_expansion")["state"] == "validated"
    assert "parameters differ" in q.status(ac, "trend_continuation")["reason"]
    assert "stale" in q.status(ac, "sr_breakout")["reason"]
    assert q.status(ac, "volatility_expansion")["state"] == "unavailable"
    rows = {r["strategy"]: r for r in q.summary() if r["asset"] == "crypto"}
    assert rows["momentum_breakout"]["verdict"] == "failed" and rows["liquidity_change"]["verdict"] is None


def test_failed_validation_rejects_signal(settings, db, monkeypatch):
    _store(settings, db, "momentum_breakout", "failed", ["OOS profit factor 0.8 < 1.3"])
    sc, _ = make(settings, db, monkeypatch)
    sig = next(s for s in sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)["symbols"][0]["signals"]
               if s["strategy"] == "momentum_breakout")
    assert sig["status"] == "rejected" and "failed backtest validation" in sig["reject_reason"]
    ev = json.loads(db.conn.execute("SELECT evidence_json FROM signals WHERE id=?", (sig["signal_id"],)).fetchone()[0])
    assert ev["quant"]["state"] == "failed" and ev["components"]["quant"] == 0.0


def test_validated_strategy_gets_quant_score(settings, db, monkeypatch):
    _store(settings, db, "momentum_breakout", "validated", ["ok"])
    sc, _ = make(settings, db, monkeypatch)
    sig = next(s for s in sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)["symbols"][0]["signals"]
               if s["strategy"] == "momentum_breakout")
    ev = json.loads(db.conn.execute("SELECT evidence_json FROM signals WHERE id=?", (sig["signal_id"],)).fetchone()[0])
    assert ev["quant"]["state"] == "validated" and 0 <= ev["components"]["quant"] <= 100
    assert "quant" not in ev["missing_components"] and ev["quant"]["evidence"]["expected_value_r"] == 0.2
    assert "failed backtest" not in (sig["reject_reason"] or "")


def test_unvalidated_strategy_stays_incomplete(settings, db, monkeypatch):
    sc, _ = make(settings, db, monkeypatch)
    sig = next(s for s in sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)["symbols"][0]["signals"]
               if s["strategy"] == "momentum_breakout")
    ev = json.loads(db.conn.execute("SELECT evidence_json FROM signals WHERE id=?", (sig["signal_id"],)).fetchone()[0])
    assert ev["components"]["quant"] is None and "no backtest validation" in ev["missing_components"]["quant"]
    assert sig["status"] in ("rejected", "incomplete")


def test_not_backtestable_list():
    assert set(NOT_BACKTESTABLE) == {"news_momentum", "orderbook_imbalance", "liquidity_change"}
