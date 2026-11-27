"""D103: Connors RSI(2) overnight swing (the only stock strategy allowed to hold overnight; unlevered)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.safety import SafeState
from app.data.models import AssetClass, Candle
from app.execution.trader import PaperTrader
from app.risk.engine import MarketQuote, Proposal
from app.strategies import swing as SW

UTC = timezone.utc
TUE_WIN = datetime(2026, 11, 24, 20, 50, tzinfo=UTC)        # 15:50 New York (close 16:00)
WED_OPEN = datetime(2026, 11, 25, 14, 30, tzinfo=UTC)       # 09:30 New York


def bars(n=260, end=TUE_WIN, drop=True, last_complete=False):
    """Steady uptrend; the last two days drop hard -> RSI(2) deeply oversold but still above SMA200."""
    out, px = [], 100.0
    day0 = datetime(end.year, end.month, end.day, 5, tzinfo=UTC)
    days = [day0 - timedelta(days=n - 1 - i) for i in range(n)]
    for i, t in enumerate(days):
        px *= 0.97 if (drop and i >= n - 2) else (1.003 if i % 3 else 0.999)
        out.append(Candle("AAA", AssetClass.STOCK, "fake", "1d", t, px * 1.002, px * 1.01, px * 0.99, px, 1e6,
                          is_complete=(i < n - 1) or last_complete))
    return out


class FakeProvider:
    def __init__(self, data):
        self.data = data

    def fetch_candles(self, sym, tf, limit=100, now=None):
        return self.data[sym]


def trader(settings, db, data, regime=("bull", "high"), max_positions=2):
    m = settings.markets
    s = settings.model_copy(update={
        "markets": m.model_copy(update={"stocks": m.stocks.model_copy(update={"watchlist": list(data)})}),
        "swing": settings.swing.model_copy(update={"max_positions": max_positions})})
    eng = SimpleNamespace(sources_for=lambda ac: ["fake"], providers={"fake": FakeProvider(data)})
    scn = SimpleNamespace(_current_regime=lambda ac, now: {"trend": regime[0], "vol": regime[1]})
    t = PaperTrader(s, db, SafeState(db), eng, scanner=scn)
    t.quote = lambda sym, now: MarketQuote(data[sym][-1].close, data[sym][-1].close * 0.9999,
                                           data[sym][-1].close * 1.0001, now, "agree", 5e6)
    return t


def test_setup_matches_backtest_rules(settings):
    df = SW.daily_frame(bars())
    p = SW.setup(df, settings.swing)
    a = float(SW.atr(df, 10).iloc[-1])
    assert p and p["rsi2"] < 5 and p["entry"] == pytest.approx(df["close"].iloc[-1])
    assert p["stop"] == pytest.approx(p["entry"] - 2 * a)
    assert "above its 200-day average" in p["why"] and "overnight" in p["why"]
    assert SW.setup(SW.daily_frame(bars(drop=False)), settings.swing) is None
    assert "not oversold" in SW.why_not(SW.daily_frame(bars(drop=False)), settings.swing)


def test_window_is_1545_to_1559_new_york(settings):
    c = settings.swing
    assert not SW.in_window(datetime(2026, 11, 24, 20, 44, tzinfo=UTC), c)
    assert SW.in_window(datetime(2026, 11, 24, 20, 45, tzinfo=UTC), c)
    assert SW.in_window(TUE_WIN, c)
    assert not SW.in_window(datetime(2026, 11, 24, 20, 59, 30, tzinfo=UTC), c)
    assert not SW.in_window(datetime(2026, 11, 28, 20, 50, tzinfo=UTC), c)       # Saturday


def test_exit_rules(settings):
    c = settings.swing
    df = SW.daily_frame(bars())
    opened = TUE_WIN
    assert SW.exit_reason(df, opened, df["close"].iloc[-1] * 1.2, c) is None        # never on the entry day
    nxt = SW.daily_frame(bars(end=datetime(2026, 11, 25, 20, 50, tzinfo=UTC)))
    assert SW.exit_reason(nxt, opened, nxt["close"].iloc[-1] * 1.10, c) == "rsi2_sma5_exit"
    assert SW.exit_reason(nxt, opened, nxt["close"].iloc[-1] * 0.95, c) is None
    late = SW.daily_frame(bars(end=datetime(2026, 12, 8, 20, 50, tzinfo=UTC)))
    assert SW.exit_reason(late, opened, late["close"].iloc[-1] * 0.9, c) == "max_hold_days"


def test_regime_gate_fails_closed(settings, db):
    t = trader(settings, db, {"AAA": bars()}, regime=("bull", "low"))
    assert t.swing_scan(TUE_WIN) == []
    assert db.get_state("swing_scan")["skipped"].startswith("regime bull/low")
    assert db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


def test_scan_once_per_day_then_unlevered_entry_held_overnight_gap_fills_at_open(settings, db):
    t = trader(settings, db, {"AAA": bars(), "BBB": bars(drop=False)}, regime=("sideways", "high"))
    assert t.swing_scan(datetime(2026, 11, 24, 20, 30, tzinfo=UTC)) == []          # before 15:45: nothing
    ids = t.swing_scan(TUE_WIN)
    assert ids and t.swing_scan(TUE_WIN + timedelta(minutes=1)) == ids              # once per day
    rows = t.candidates(ids)
    assert [r["symbol"] for r in rows] == ["AAA"] and rows[0]["validation_status"] == "forward_test"
    res = t.execute_signal(rows[0], TUE_WIN)
    assert res["approved"], res["reason"]
    pos = t.ledger.open_positions()[0]
    assert pos["leverage"] == 1 and pos["strategy"] == "rsi2_swing"
    assert res["planned_risk_usd"] <= settings.portfolio.starting_equity_usd * settings.risk.max_risk_per_trade_pct / 100 + 1e-6
    # at the close: an intraday stock would be flattened; the swing stays open
    c = pos["entry_price"]
    late = [Candle("AAA", AssetClass.STOCK, "x", "1m", TUE_WIN + timedelta(minutes=i + 1), c, c, c, c, 1, True, "regular")
            for i in range(9)]
    r = t.sim.manage_position(pos, late, datetime(2026, 11, 24, 21, 0, tzinfo=UTC), None)
    assert r["status"] == "open"
    # overnight: market closed -> not even fetched
    assert t.manage(datetime(2026, 11, 25, 2, 0, tzinfo=UTC))[0]["note"].startswith("held overnight")
    # next morning gaps below the stop: pre-market bars ignored, filled at the regular open (minus slippage)
    pos = t.ledger.open_positions()[0]
    gap = pos["stop_loss"] * 0.95
    pre = Candle("AAA", AssetClass.STOCK, "x", "1m", WED_OPEN - timedelta(hours=2), c, c, gap * 0.9, c, 1, True, "pre")
    opn = Candle("AAA", AssetClass.STOCK, "x", "1m", WED_OPEN, gap, gap, gap * 0.99, gap, 1, True, "regular")
    t.engine.get_candles = lambda *a, **k: SimpleNamespace(series=SimpleNamespace(candles=[pre, opn]))
    t.quote = lambda sym, now: None
    out = t.manage(WED_OPEN + timedelta(minutes=2))
    assert out[0]["reason"] == "stop_loss"
    assert out[0]["exit_price"] == pytest.approx(gap * (1 - t.sim.costs.slip(AssetClass.STOCK)))
    assert out[0]["exit_price"] < pos["stop_loss"]                                   # gap risk is real


def test_entry_window_and_slot_limit(settings, db):
    t = trader(settings, db, {"AAA": bars()}, max_positions=1)
    ids = t.swing_scan(TUE_WIN)
    row = t.candidates(ids)[0]
    p = Proposal.from_signal_row(row)
    d = t.risk.evaluate(p, t.quote("AAA", TUE_WIN), datetime(2026, 11, 24, 20, 59, 40, tzinfo=UTC))
    assert not d.approved and "swing_entry_window" in d.reason
    intraday = Proposal(None, "AAA", AssetClass.STOCK, "momentum_breakout", "long", 1, 0.9, 1.3, 80, "new",
                        None, "live", "validated")
    d2 = t.risk.evaluate(intraday, t.quote("AAA", TUE_WIN), TUE_WIN)
    assert "stock_entry_window" in d2.reason                                      # intraday rule unchanged
    db.insert("positions", {"opened_ts": TUE_WIN.isoformat(), "symbol": "ZZZ", "asset_class": "stock",
                            "strategy": "rsi2_swing", "direction": "long", "quantity": 1, "entry_price": 10,
                            "stop_loss": 9, "take_profit": 12, "fees_usd": 0, "slippage_usd": 0, "status": "open",
                            "leverage": 1})
    assert t.execute_signal(row, TUE_WIN)["reason"] == "max overnight positions"


def test_config_and_plain_english(settings):
    from app.dashboard.plain import exit_reason, explain, strategy
    assert settings.swing.enabled and settings.swing.leverage == 1.0
    assert settings.regime.matrix["stock"]["rsi2_swing"] == ["bull/high", "sideways/high"]
    assert settings.risk.stock_overnight_holding is False                          # global invariant kept
    assert strategy("rsi2_swing").startswith("Overnight dip-buy")
    assert "5-day average" in exit_reason("rsi2_sma5_exit") and "10 trading days" in exit_reason("max_hold_days")
    assert "last 15 minutes" in explain("risk: swing_entry_window (minutes to close)")
    with pytest.raises(Exception):
        type(settings.swing)(leverage=2.0)


def test_ai_veto_blocks_swing_entry(settings, db):
    t = trader(settings, db, {"AAA": bars()})
    t.scanner.run = lambda ac, symbols=None, mode=None: {"run_id": None, "signals": 0}
    t._llm_check = lambda row, now: {"decision": "VETO", "reason": "Fraud probe announced."}
    rep = t.run_once(TUE_WIN)
    ex = [e for e in rep["executions"] if e["symbol"] == "AAA"]
    assert ex and not ex[0]["approved"] and ex[0]["reason"].startswith("AI veto")
    assert t.ledger.open_positions() == []
