"""D94 simulated isolated leverage on the main paper ledger."""
from datetime import timedelta

import pytest

from app.config import load_settings
from app.core.safety import SafeState
from app.database.db import Database
from app.execution.simulator import PaperSimulator
from app.portfolio.ledger import Ledger
from app.risk import leverage as LV
from app.risk.engine import RiskEngine
from tests.test_risk_execution import NOW, add_signal, bar, proposal, q


@pytest.fixture
def lenv(tmp_path):
    s = load_settings(overrides={"paths": {"database": str(tmp_path / "t.sqlite3"), "log_dir": str(tmp_path / "l")},
                                 "app": {"environment": "test"}, "execution": {"paper_leverage": 100}})
    db = Database(tmp_path / "d.sqlite3")
    db.migrate()
    safe, led = SafeState(db), Ledger(s, db)
    yield s, db, RiskEngine(s, db, safe, led), PaperSimulator(s, db, safe, led), led
    db.close()


def test_levels_and_limits(lenv):
    s = lenv[0]
    assert LV.liq_price(100, 100, 0.5) == pytest.approx(99.5)
    assert LV.risk_pct(s) == 25.0 and LV.daily_pct(s) == 50.0
    st, tg = LV.scalp_levels(s, 100.0, 99.0, 103.0, 0.2)
    assert tg == pytest.approx(100.25) and 99.7 <= st < 100 and st > 99.5        # tight, well before liquidation


def test_stop_beyond_liquidation_is_skipped(lenv):
    s, db, risk, sim, led = lenv
    sid = add_signal(db, stop=99.0, target=103.0)
    p = proposal(db, sid)
    p.leverage = 100.0
    d = risk.evaluate(p, q(), NOW)
    assert not d.approved and "stop_before_liquidation" in d.reason


def test_leveraged_trade_margin_fees_and_liquidation_never_negative(lenv):
    s, db, risk, sim, led = lenv
    sid = add_signal(db, entry=100.0, stop=99.7, target=101.5)
    p = proposal(db, sid)
    p.leverage = 100.0
    d = risk.evaluate(p, q(99.95), NOW)
    assert d.approved, d.reason
    assert d.planned_risk_usd <= 250 + 1e-6 and d.notional / 100 <= 500 + 1e-6    # risk <= 25 %, margin <= 50 %
    sim.open_position(p, d, q(99.95), NOW)
    pos = led.open_positions()[0]
    assert pos["leverage"] == 100 and pos["liquidation_price"] == pytest.approx(pos["entry_price"] * 0.995)
    st = led.state(NOW)
    assert st.equity == pytest.approx(1000 - pos["fees_usd"], abs=1e-6)               # fee on full notional
    r = sim.manage_position(pos, [bar(NOW + timedelta(minutes=1), 98.0, 98.1, 97.0, 97.5)], NOW + timedelta(minutes=2))
    assert r["reason"] == "liquidation"
    st = led.state(NOW + timedelta(minutes=3))
    assert st.equity >= 0 and st.equity == pytest.approx(1000 - pos["margin_usd"] - pos["fees_usd"]
                                                         - db.conn.execute("SELECT fee_usd FROM simulated_fills ORDER BY id DESC").fetchone()[0], abs=1e-4)
