"""Milestone 5 live tests (pytest -m live): real quotes / bars through the risk engine and the
internal paper simulator, on a TEMP database. The test signal is synthetic (clearly labelled);
prices, spreads and bars are real. No orders are sent anywhere."""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import load_secrets
from app.core.bootstrap import bootstrap
from app.data.engine import MarketDataEngine
from app.execution.alpaca_paper import AlpacaOrdersDisabled, AlpacaPaperBroker
from app.execution.trader import PaperTrader
from app.risk.engine import Proposal

pytestmark = pytest.mark.live
REAL = load_secrets()


def test_real_paper_cycle_btc(settings):
    ctx = bootstrap(settings, REAL, console_log=False)
    try:
        eng = MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state)
        tr = PaperTrader(ctx.settings, ctx.db, ctx.safe_state, eng)
        now = datetime.now(timezone.utc)
        q = tr.quote("BTC-USD", now)
        assert q is not None and q.bid and q.ask and q.verdict in ("agree", "outlier_excluded", "single_source")
        px = q.price
        sid = ctx.db.insert("signals", {
            "ts": now.isoformat(), "symbol": "BTC-USD", "asset_class": "crypto", "strategy": "live_test_synthetic",
            "direction": "long", "entry_price": px, "stop_loss": px * 0.99, "take_profit": px * 1.04,
            "data_ts": now.isoformat(), "status": "new", "timeframe": "15m", "signal_key": f"livetest|{now}",
            "expires_ts": (now + timedelta(minutes=10)).isoformat(), "data_status": "live"})
        ctx.db.insert("signal_scores", {"signal_id": sid, "ts": now.isoformat(), "total_score": 75, "weights_json": "{}"})
        row = ctx.db.conn.execute("SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc "
                                  "ON sc.signal_id=s.id WHERE s.id=?", (sid,)).fetchone()
        res = tr.execute_signal(row, datetime.now(timezone.utc))
        assert res["approved"], res["reason"]
        assert res["planned_risk_usd"] <= 2.5 and res["rr_net"] >= 2
        f = res["fill"]
        assert f["fill_price"] >= q.ask                                     # paid the ask + slippage
        rep = tr.run_once(datetime.now(timezone.utc), scan=False)          # manage with real 1m bars
        assert rep["managed"] and "error" not in rep["managed"][0], rep["managed"]
        port = rep["portfolio"]
        assert port["open_positions"] in (0, 1) and abs(port["equity"] - 1000) < 5
        out = tr.kill("live test end", flatten=True)
        assert not ctx.safe_state.trading_allowed()
        assert all(c["reason"] == "kill_switch" for c in out["closed"])
        assert abs(out["portfolio"]["equity"] - 1000) < 5 and out["portfolio"]["open_positions"] == 0
        n = ctx.db.conn.execute("SELECT COUNT(*) FROM simulated_fills").fetchone()[0]
        assert n == 2
    finally:
        ctx.close()


@pytest.mark.skipif(not (REAL.has("alpaca_api_key_id") and REAL.has("alpaca_api_secret_key")),
                    reason="no Alpaca paper keys")
def test_alpaca_paper_read_only_eligibility(settings):
    b = AlpacaPaperBroker(settings, REAL)
    nvda = b.asset("NVDA")
    assert nvda.tradable and nvda.asset_class == "us_equity"
    assert b.orders_enabled is False
    with pytest.raises(AlpacaOrdersDisabled):
        b.submit_bracket("NVDA", 1, 1, 2, "never-sent")
