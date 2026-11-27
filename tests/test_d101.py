"""D101: Bybit maker/taker fees, maker entry that may not fill, stock slippage."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import load_settings
from app.data.models import AssetClass
from app.execution.costs import CostModel
from app.risk.engine import size_position

C = AssetClass.CRYPTO


@pytest.fixture
def S():
    return load_settings()


def test_fee_config(S):
    ex = S.execution
    assert ex.crypto_maker_fee_bps == 2.0 and ex.crypto_fee_bps == 5.5 and ex.crypto_entry_order == "limit"
    assert ex.slippage_bps_crypto_maker == 1.0 and ex.slippage_bps_crypto == 3.0
    assert ex.stock_fee_bps == 0 and ex.slippage_bps_stock == 3.0


def test_maker_entry_fill_and_fees(S):
    cm = CostModel(S)
    f = cm.buy_fill(C, 100.0, bid=99.99, ask=100.01)
    assert f.price == pytest.approx(99.99 * 1.0001) and "maker" in f.assumption
    assert cm.fee(1000, C, maker=True) == pytest.approx(0.2) and cm.fee(1000, C) == pytest.approx(0.55)
    assert cm.target_fill(C, 110, 109).price == pytest.approx(110 * 0.9999)
    assert cm.stop_fill(C, 95, 96).price == pytest.approx(95 * 0.9997)        # taker slippage on the stop
    # stocks unchanged: market orders, $0
    assert CostModel(S).buy_fill(AssetClass.STOCK, 100, 99.99, 100.01).price == pytest.approx(100.01 * 1.0003)


def test_sizing_uses_maker_entry_tp_and_taker_stop(S):
    cm = CostModel(S)
    z = size_position(cm, S, C, 100.0, 98.0, 103.0, 1000, 1000, 1000)
    loss = (100 - 98 * 0.9997) + 100 * 0.0002 + 98 * 0.9997 * 0.00055
    gain = (103 * 0.9999 - 100) - 100 * 0.0002 - 103 * 0.9999 * 0.0002
    assert z.per_unit_loss == pytest.approx(loss) and z.per_unit_gain == pytest.approx(gain)


class _DB:
    def __init__(self):
        self.st, self.ev = {}, []

    def get_state(self, k, d=None):
        return self.st.get(k, d)

    def set_state(self, k, v):
        self.st[k] = v

    def log_event(self, *a, **k):
        self.ev.append(a)


def test_limit_entry_needs_price_to_come_back(S):
    from app.execution.trader import PaperTrader
    t = PaperTrader.__new__(PaperTrader)
    t.s, t.db = S, _DB()
    p = SimpleNamespace(signal_id=7, symbol="ETH-USD")
    now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    q = lambda px: SimpleNamespace(price=px, bid=px - 0.01, ask=px + 0.01)      # noqa: E731
    assert "placed" in t._limit_gate(p, q(100.0), now)                       # limit at 99.99
    assert "waiting" in t._limit_gate(p, q(100.5), now + timedelta(minutes=5))
    assert t._limit_gate(p, q(99.98), now + timedelta(minutes=6)) is None    # traded down to the limit -> fill
    assert "already" in t._limit_gate(p, q(99.0), now + timedelta(minutes=7))
    p2 = SimpleNamespace(signal_id=8, symbol="SOL-USD")
    t._limit_gate(p2, q(50.0), now)
    assert "not filled" in t._limit_gate(p2, q(49.0), now + timedelta(minutes=16))   # TTL passed -> no trade
