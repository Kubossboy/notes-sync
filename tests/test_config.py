import pytest
from pydantic import ValidationError

from app.config import load_secrets, load_settings


def test_defaults_match_spec():
    s = load_settings()
    assert s.execution.mode == "paper"
    assert s.execution.leverage_enabled is False and s.execution.margin_enabled is False
    assert s.portfolio.starting_equity_usd == 1000.0
    eq = s.portfolio.starting_equity_usd
    assert eq * s.risk.max_risk_per_trade_pct / 100 == pytest.approx(2.50)
    assert eq * s.risk.max_daily_loss_pct / 100 == pytest.approx(10.0)
    assert s.risk.max_open_positions == 2
    assert s.risk.min_reward_risk == 2.0
    w = s.scoring.weights
    assert (w.technical, w.momentum_volume, w.news, w.sentiment, w.quant, w.liquidity) == \
        (0.10, 0.10, 0.40, 0.15, 0.15, 0.10)   # spec start 25/20/20/10/15/10, news-driven reweight D77
    assert s.news.finbert_enabled is True


@pytest.mark.parametrize("override", [
    {"execution": {"mode": "live"}},
    {"execution": {"leverage_enabled": True}},
    {"execution": {"margin_enabled": True}},
    {"risk": {"allow_martingale": True}},
    {"risk": {"allow_averaging_down": True}},
    {"risk": {"require_stop_loss": False}},
    {"risk": {"max_risk_per_trade_pct": 5}},
    {"risk": {"max_daily_loss_pct": 0}},
    {"scoring": {"min_score_to_trade": -1}},
    {"scoring": {"weights": {"technical": 0.9}}},
    {"execution": {"provider": "alpaca_live"}},
    {"unknown_section": {}},
    {"markets": {"stocks": {"watchlist": ["AAPL", "aapl"]}}},
])
def test_unsafe_or_invalid_config_rejected(override):
    with pytest.raises(ValidationError):
        load_settings(overrides=override)


def test_config_hash_changes_with_values():
    a = load_settings()
    b = load_settings(overrides={"risk": {"max_open_positions": 1}})
    assert a.config_hash() != b.config_hash()
    assert a.config_hash() == load_settings().config_hash()


def test_settings_are_frozen():
    s = load_settings()
    with pytest.raises(ValidationError):
        s.execution.mode = "live"


def test_secrets_from_env_are_masked(tmp_path):
    sec = load_secrets({"ALPACA_API_KEY_ID": "PKTESTKEY123", "X_BEARER_TOKEN": "  "},
                       dotenv_path=tmp_path / "none.env")
    assert sec.has("alpaca_api_key_id")
    assert not sec.has("x_bearer_token")
    assert "PKTESTKEY123" not in repr(sec) and "PKTESTKEY123" not in str(sec)
    assert sec.secret_values() == ["PKTESTKEY123"]


def test_dotenv_file_read_and_env_overrides(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# c\nFINNHUB_API_KEY=fromfile\nNEWSAPI_KEY='quoted'\n")
    sec = load_secrets({"FINNHUB_API_KEY": "fromenv"}, dotenv_path=f)
    assert sec.finnhub_api_key.get_secret_value() == "fromenv"
    assert sec.newsapi_key.get_secret_value() == "quoted"

def test_scalp_profile_overlay_keeps_safety_limits():
    """D95: scalp.yaml is a named overlay; paper-only and risk caps stay in force."""
    from app.config import PROJECT_ROOT
    s = load_settings(PROJECT_ROOT / "config" / "scalp.yaml")
    assert s.execution.mode == "paper"
    assert s.execution.leverage_enabled is False and s.execution.margin_enabled is False
    assert s.execution.allow_short is False
    assert s.risk.allow_martingale is False and s.risk.allow_averaging_down is False
    assert s.risk.require_stop_loss is True and s.risk.require_take_profit is True
    assert s.risk.max_risk_per_trade_pct == 0.25
    assert s.risk.max_daily_loss_pct == 1.0
    assert s.risk.max_open_positions == 2
    assert s.risk.max_consecutive_losses_per_day == 3
    assert s.risk.min_reward_risk == 1.5          # D97 scalp overlay
    assert s.risk.lev_min_reward_risk == 1.0     # D97 scalp overlay; default profile stays 2.0 / 1.2
    assert s.risk.lev_max_daily_loss_pct == 50.0
    assert s.risk.lev_max_risk_per_trade_pct == 25.0
    assert s.scanner.stock_timeframe == "1m"
    assert s.scanner.crypto_timeframe == "5m"
    assert s.scanner.lookback_bars == 180
    assert s.risk.crypto_max_hold_hours == 2
    assert s.risk.symbol_cooldown_minutes_after_loss == 20
    assert s.scoring.min_score_to_trade == 40
    assert s.validation.forward_test_min_score == 40
    assert s.validation.paper_trade_unvalidated is True
    assert s.scanner.cap_target_by_resistance is False
    assert s.scanner.max_stop_atr == 6.0
    assert s.risk.lev_tp_atr == 1.5
    assert s.config_hash() != load_settings().config_hash()

def test_default_profile_still_blocks_low_scores_and_keeps_resistance_cap():
    s = load_settings()
    assert s.scoring.min_score_to_trade == 70
    assert s.validation.paper_trade_unvalidated is False
    assert s.scanner.cap_target_by_resistance is True
    assert s.scanner.max_stop_atr == 3.0
    assert s.risk.min_reward_risk == 2.0 and s.risk.lev_min_reward_risk == 1.2
