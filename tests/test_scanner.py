from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.core.safety import SafeState
from app.data.engine import MarketDataEngine
from app.data.models import AssetClass, Candle
from app.data.orderbook import BookSnapshot
from app.scanners.scanner import Scanner
from tests.fakes import FakeProvider

UTC = timezone.utc
START = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
N = 200


def breakout_candles(symbol="BTC-USD", fake_close_bump=1.0, seed=11):
    r = np.random.default_rng(seed)
    closes = list(100 + np.cumsum(r.normal(0.06, 0.5, N - 2)))
    closes.append(max(closes) + fake_close_bump)      # breakout bar (last complete)
    closes.append(closes[-1])                         # forming bar
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        v = 6000.0 if i == N - 2 else 1000.0
        out.append(Candle(symbol, AssetClass.CRYPTO, "fake", "15m", START + timedelta(minutes=15 * i),
                          o, max(o, c) + 0.3, min(o, c) - 0.3, c, v, is_complete=i < N - 1))
    return out


NOW = START + timedelta(minutes=15 * (N - 1), seconds=30)


def make(settings, db, monkeypatch, candles=None, book=True):
    cs = candles or breakout_candles()
    provs = {n: FakeProvider(n, cs, price=cs[-1].close) for n in ("coinbase", "kraken", "binance")}
    provs["yahoo"] = FakeProvider("yahoo", [])
    eng = MarketDataEngine(settings, db, SafeState(db), providers=provs)
    sc = Scanner(settings, eng)
    mid = cs[-2].close
    snap = BookSnapshot("BTC-USD", "coinbase", NOW, [(mid - 0.001, 5e6 / mid)], [(mid + 0.001, 5e6 / mid)])
    monkeypatch.setattr(sc, "_book", lambda sym, pref: snap if book else None)
    return sc, eng


def test_crypto_scan_produces_journaled_signal(settings, db, monkeypatch):
    sc, _ = make(settings, db, monkeypatch)
    rep = sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)
    sym = rep["symbols"][0]
    assert sym["data_status"] == "live" and "skipped" not in sym
    names = [s["strategy"] for s in sym["signals"]]
    assert "momentum_breakout" in names, sym["no_setup"]
    sig = next(s for s in sym["signals"] if s["strategy"] == "momentum_breakout")
    assert sig["persisted"] and sig["status"] in ("rejected", "incomplete")
    assert sig["stop"] < sig["entry"] < sig["target"]
    row = db.conn.execute("SELECT * FROM signals WHERE id=?", (sig["signal_id"],)).fetchone()
    assert row["signal_key"].startswith("BTC-USD|momentum_breakout|15m|")
    assert row["data_ts"] == (START + timedelta(minutes=15 * (N - 2))).isoformat()   # last COMPLETE bar
    sc_row = db.conn.execute("SELECT * FROM signal_scores WHERE signal_id=?", (sig["signal_id"],)).fetchone()
    assert sc_row["news"] is None and sc_row["available_weight"] == pytest.approx(0.30)
    assert db.conn.execute("SELECT COUNT(*) FROM strategy_decisions WHERE signal_id=?",
                           (sig["signal_id"],)).fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM orderbook_snapshots").fetchone()[0] == 1  # snapshot journaled
    assert db.conn.execute("SELECT summary_json FROM scan_runs WHERE id=?", (rep["run_id"],)).fetchone()[0]


def test_incomplete_when_costs_allow_but_modules_missing(tmp_path, db, monkeypatch):
    from app.config import load_settings
    s = load_settings(overrides={"execution": {"crypto_fee_bps": 0, "slippage_bps_crypto": 0},
                                 "paths": {"log_dir": str(tmp_path)}})
    sc, _ = make(s, db, monkeypatch)
    rep = sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)
    sig = next(x for x in rep["symbols"][0]["signals"] if x["strategy"] == "momentum_breakout")
    assert sig["status"] == "incomplete" and sig["rr_net"] >= s.risk.lev_min_reward_risk, sig   # D99 ATR exits (100x)
    assert sig["score"] < 60        # cannot reach a tradable score without news/sentiment/quant


def test_rescan_same_bar_is_not_duplicated(settings, db, monkeypatch):
    sc, _ = make(settings, db, monkeypatch)
    sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)
    n1 = db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    rep = sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)
    assert db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == n1 > 0
    assert all(s.get("duplicate") for s in rep["symbols"][0]["signals"])


def test_research_mode_persists_nothing(settings, db, monkeypatch):
    sc, _ = make(settings, db, monkeypatch)
    rep = sc.run(AssetClass.CRYPTO, ["BTC-USD"], "research", now=NOW)
    assert rep["signals"] >= 1
    assert db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


def test_stale_data_skipped_in_live_mode(settings, db, monkeypatch):
    sc, _ = make(settings, db, monkeypatch)
    rep = sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW + timedelta(hours=3))
    assert "not tradable" in rep["symbols"][0]["skipped"]


def test_safe_state_blocks_live_scan(settings, db, monkeypatch):
    sc, eng = make(settings, db, monkeypatch)
    eng.safe.halt("test")
    rep = sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)
    assert rep["symbols"][0]["skipped"] == "SAFE STATE active"


def test_missing_book_rejects_crypto_signals(settings, db, monkeypatch):
    sc, _ = make(settings, db, monkeypatch, book=False)
    rep = sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)
    for s in rep["symbols"][0]["signals"]:
        assert s["status"] == "rejected" and "no order book" in s["reject_reason"]


def test_strategy_exception_is_journaled_not_fatal(settings, db, monkeypatch):
    from app.strategies import crypto as C
    def boom(ctx, p):
        raise ZeroDivisionError("bug")
    monkeypatch.setitem(C.CRYPTO_STRATEGIES, "volume_expansion", boom)
    sc, _ = make(settings, db, monkeypatch)
    rep = sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)
    assert rep["symbols"][0]["no_setup"]["volume_expansion"].startswith("error")
    assert db.conn.execute("SELECT COUNT(*) FROM errors WHERE module='strategy.volume_expansion'").fetchone()[0] == 1


def test_disabled_strategy(settings, db, monkeypatch):
    sc, _ = make(settings, db, monkeypatch)
    from app.data.models import AssetClass as A
    rep = Scanner(settings, sc.engine).run(A.STOCK, ["AAPL"], "research", now=NOW)
    assert rep["symbols"][0]["skipped"] == "no valid data"


def test_scan_with_news_engine_vetoes_long_on_verified_negative_catalyst(settings, db, monkeypatch):
    from app.config import Secrets
    from app.intelligence.engine import NewsEngine
    from app.intelligence.sources import FeedSource
    from tests.fakes import FakeResp
    from tests.test_news_engine import FakeSec, RouteSession, rss
    sc, eng = make(settings, db, monkeypatch)
    feed = FeedSource("kraken_status", "https://st.test/rss", 1, "crypto", "exchange")
    ne = NewsEngine(settings, db, Secrets(), eng.safe, RouteSession({"https://st.test/rss": FakeResp(
        200, text=rss(("Bitcoin withdrawals halted after exchange hacked, funds drained", NOW - timedelta(hours=1))))}),
        [feed], FakeSec())
    ne.ingest(NOW)
    sc.news = ne
    rep = sc.run(AssetClass.CRYPTO, ["BTC-USD"], "live", now=NOW)
    sym = rep["symbols"][0]
    assert sym["news"]["negative_high_verified"] == 1 and sym["sentiment"]["composite"] <= 0
    sig = next(s for s in sym["signals"] if s["strategy"] == "momentum_breakout")
    assert sig["status"] == "rejected" and "verified high-impact negative catalyst" in sig["reject_reason"]
    row = db.conn.execute("SELECT news, sentiment FROM signal_scores WHERE signal_id=?", (sig["signal_id"],)).fetchone()
    assert row["news"] is not None and row["sentiment"] is not None
