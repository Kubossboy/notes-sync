"""Configuration system for ATLAS.

- Non-secret settings come from config/default.yaml (optionally overlaid by a
  second YAML file passed via ATLAS_CONFIG).
- Secrets come ONLY from environment variables (or a git-ignored .env file) and
  are held as pydantic SecretStr so they never appear in repr/logs.
- Safety invariants (paper-only, no leverage, no margin, no martingale ...) are
  enforced by validators: an unsafe config cannot be loaded at all.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"

# Endpoints that must never be used. Checked by app.core.safety as well.
FORBIDDEN_LIVE_ENDPOINTS = (
    "api.alpaca.markets",          # Alpaca LIVE trading (paper is paper-api.alpaca.markets)
    "api.coinbase.com/api/v3/brokerage/orders",
    "api.binance.com/api/v3/order",
    "api.kraken.com/0/private",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSection(_Strict):
    name: str = "ATLAS Trading AI"
    environment: Literal["dev", "test", "paper"] = "dev"
    timezone_user: str = "Europe/Prague"
    timezone_market: str = "America/New_York"


class PathsSection(_Strict):
    database: str = "var/atlas.sqlite3"
    log_dir: str = "logs"

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else PROJECT_ROOT / path


class ExecutionSection(_Strict):
    mode: Literal["paper"] = "paper"
    provider: Literal["internal_sim", "alpaca_paper"] = "internal_sim"
    leverage_enabled: Literal[False] = False
    margin_enabled: Literal[False] = False
    allow_short: bool = False
    commission_per_trade_usd: float = Field(0.0, ge=0)
    stock_fee_bps: float = Field(0.0, ge=0)
    crypto_fee_bps: float = Field(5.5, ge=0)   # Bybit USDT-perp taker; yaml may override
    slippage_bps_stock: float = Field(5.0, ge=0)
    slippage_bps_crypto: float = Field(3.0, ge=0)
    # --- paper simulator (milestone 5)
    fractional_stocks: bool = True                  # fractional shares (Alpaca supports them); False -> whole shares
    stock_qty_step: float = Field(0.001, gt=0)
    crypto_qty_step: float = Field(0.000001, gt=0)
    min_notional_usd: float = Field(5.0, gt=0)
    assumed_half_spread_bps_stock: float = Field(2.0, ge=0)   # used when no bid/ask is available
    assumed_half_spread_bps_crypto: float = Field(2.0, ge=0)
    max_order_pct_bar_dollar_volume: float = Field(1.0, gt=0, le=100)
    manage_timeframe: Literal["1m", "5m"] = "1m"
    max_quote_age_seconds: float = Field(90, gt=0)
    # Alpaca PAPER order mirroring (paper-api.alpaca.markets only). OFF by default; the
    # internal $1,000 ledger stays authoritative either way.
    alpaca_paper_orders_enabled: bool = False
    paper_leverage: int = Field(100, ge=1, le=100)  # D94: SIMULATED isolated leverage, internal main ledger only
    maintenance_margin_pct: float = Field(0.5, ge=0, lt=1)
    trade_management_enabled: bool = True           # D92: tighten-only SL, reduce-only TP, early exits


class PortfolioSection(_Strict):
    starting_equity_usd: float = Field(1000.0, gt=0)
    base_currency: Literal["USD"] = "USD"


class RiskSection(_Strict):
    max_risk_per_trade_pct: float = Field(0.25, gt=0, le=1.0)
    max_daily_loss_pct: float = Field(1.0, gt=0, le=5.0)
    max_open_positions: int = Field(2, ge=1, le=10)
    min_reward_risk: float = Field(2.0, ge=1.0)
    require_stop_loss: Literal[True] = True
    require_take_profit: Literal[True] = True
    allow_averaging_down: Literal[False] = False
    allow_martingale: Literal[False] = False
    require_complete_signal: Literal[True] = True     # 'incomplete' signals are never executed
    max_position_pct_equity: float = Field(50.0, gt=0, le=100)
    max_entry_drift_r: float = Field(0.25, ge=0, le=1)   # price ran > 0.25 R past entry -> no chasing
    max_consecutive_losses_per_day: int = Field(3, ge=1)  # anti revenge trading
    symbol_cooldown_minutes_after_loss: float = Field(60, ge=0)
    stock_no_new_entries_minutes_before_close: float = Field(15, ge=0)
    stock_flatten_minutes_before_close: float = Field(5, ge=1)
    stock_overnight_holding: Literal[False] = False       # day trading: flat at the close
    crypto_max_hold_hours: float = Field(24, gt=0)
    # D94 caps for leveraged paper trading (base limit x leverage, capped)
    lev_max_risk_per_trade_pct: float = Field(25.0, gt=0, le=25.0)    # 0.25 % x 100 = 25 % ($250 on $1,000)
    lev_max_daily_loss_pct: float = Field(50.0, gt=0, le=50.0)        # 1 % x 100 = 100 % -> capped at 50 %
    lev_max_drawdown_pct: float = Field(50.0, gt=0, le=50.0)          # no new trades below 50 % of start equity
    lev_min_reward_risk: float = Field(1.2, ge=1.0)                   # default 1.2; D97 paper-scalp overlay may set 1.0
    lev_tp_atr: float = Field(1.25, gt=0, le=1.5)                     # TP <= fill + 1.25 x ATR14(15m)
    lev_sl_liq_fraction: float = Field(0.6, gt=0, le=0.8)             # SL at most 60 % of the way to liquidation
    # D99: exits from ATR (scan timeframe), leverage adapts so the SL stays <= lev_sl_liq_fraction of liquidation
    exit_mode: Literal["atr", "scalp"] = "atr"                        # "scalp" = old D94 100x clamp (SL ~0.3 %)
    atr_stop_mult: float = Field(1.5, gt=0, le=5)
    atr_target_mult: float = Field(2.0, gt=0, le=10)


class Weights(_Strict):
    technical: float = 0.10          # news-driven reweight (D77): technicals = entry confirmation only
    momentum_volume: float = 0.10
    news: float = 0.40
    sentiment: float = 0.15
    quant: float = 0.15
    liquidity: float = 0.10

    @model_validator(mode="after")
    def _sum_to_one(self) -> "Weights":
        vals = list(self.model_dump().values())
        if any(v < 0 for v in vals):
            raise ValueError("scoring weights must be non-negative")
        if abs(sum(vals) - 1.0) > 1e-9:
            raise ValueError(f"scoring weights must sum to 1.0 (got {sum(vals):.4f})")
        return self


class ScoringSection(_Strict):
    min_score_to_trade: int = Field(70, ge=0, le=100)  # default 70; D96 allows paper-scalp overlays below the old spec floor of 60
    weights: Weights = Weights()


class MarketList(_Strict):
    enabled: bool = True
    watchlist: list[str] = []

    @field_validator("watchlist")
    @classmethod
    def _upper_unique(cls, v: list[str]) -> list[str]:
        out = [s.strip().upper() for s in v if s.strip()]
        if len(out) != len(set(out)):
            raise ValueError("duplicate symbols in watchlist")
        return out


class MarketsSection(_Strict):
    stocks: MarketList = MarketList()
    crypto: MarketList = MarketList()


class DataSection(_Strict):
    stock_sources: list[Literal["yahoo", "alpaca"]] = ["yahoo", "alpaca"]
    crypto_sources: list[Literal["coinbase", "kraken", "binance"]] = ["coinbase", "kraken", "binance"]
    include_prepost: bool = True
    core_symbols: list[str] = []      # bad data on these halts everything; empty = every symbol halts
    live_max_age_bars: float = Field(2.0, gt=0)      # last complete bar closed <= N bars ago -> live
    live_grace_seconds: float = Field(60, ge=0)
    stale_after_bars: float = Field(5.0, gt=0)       # older than N bars while market open -> stale
    crosscheck_tolerance_bps_crypto: float = Field(50, gt=0)
    crosscheck_tolerance_bps_stock: float = Field(100, gt=0)
    crosscheck_halt_bps: float = Field(300, gt=0)    # sources disagree this much -> SAFE STATE
    max_bar_change_pct: float = Field(20, gt=0)      # single-bar move above this -> warning
    min_sources_crypto: int = Field(2, ge=1)
    max_quote_spread_bps: float = Field(200, gt=0)   # wider quotes (e.g. IEX after hours) excluded from consensus         # consensus needs this many agreeing sources
    halt_on_invalid_data: Literal[True] = True       # spec section 15: invalid data -> safe state

    @model_validator(mode="after")
    def _check(self) -> "DataSection":
        if not self.stock_sources or not self.crypto_sources:
            raise ValueError("at least one stock and one crypto source required")
        if self.stale_after_bars < self.live_max_age_bars:
            raise ValueError("stale_after_bars must be >= live_max_age_bars")
        if self.crosscheck_halt_bps <= max(self.crosscheck_tolerance_bps_crypto,
                                           self.crosscheck_tolerance_bps_stock):
            raise ValueError("crosscheck_halt_bps must exceed the tolerances")
        return self


class StrategyToggle(_Strict):
    enabled: bool = True
    params: dict[str, float] = {}


class ScannerSection(_Strict):
    stock_timeframe: Literal["1m", "5m", "15m", "1h"] = "5m"
    crypto_timeframe: Literal["1m", "5m", "15m", "1h"] = "15m"
    lookback_bars: int = Field(400, ge=60, le=1000)
    stock_batch_size: int = Field(0, ge=0)    # 0 = whole watchlist every cycle
    crypto_batch_size: int = Field(0, ge=0)
    stocks_only_in_market_hours: bool = False
    signal_ttl_bars: int = Field(3, ge=1)
    stock_min_avg_dollar_volume: float = Field(2_000_000, ge=0)   # per bar, last 20 regular bars
    stock_min_price: float = Field(5.0, gt=0)
    crypto_max_spread_bps: float = Field(10.0, gt=0)
    crypto_min_depth_usd_05: float = Field(50_000, ge=0)        # each side within 0.5% of mid
    stock_strategies: dict[str, StrategyToggle] = {}
    crypto_strategies: dict[str, StrategyToggle] = {}
    cap_target_by_resistance: bool = True   # D96: scalp overlay may disable; nearby resistance was collapsing net R:R
    max_stop_atr: float = Field(3.0, gt=0, le=10)  # reject a setup whose stop is wider than this many ATR
    # D99: resistance caps the target unless the bar is a momentum breakout (close > prior N-bar high, rvol >= x)
    momentum_lookback: int = Field(20, ge=2)
    momentum_rvol: float = Field(1.5, ge=0)
    stock_min_rvol: float = Field(1.2, ge=0)        # D99: stocks need volume confirmation (rvol20)
    crypto_min_rvol: float = Field(0.0, ge=0)


class NewsSection(_Strict):
    enabled: bool = True
    refresh_minutes: float = Field(15, gt=0)          # scanner re-ingests when older than this
    max_health_age_minutes: float = Field(60, gt=0)   # older -> news component unavailable
    min_source_success_ratio: float = Field(0.5, gt=0, le=1)
    window_hours: float = Field(24, gt=0)
    half_life_hours: float = Field(6, gt=0)
    catalyst_hours: float = Field(20, gt=0)           # freshness for news-driven momentum (flat window)
    catalyst_session_aware: bool = True               # stocks: also fresh until the close of the first
                                                      # regular session after publication (after-close 8-Ks/earnings)
    catalyst_max_hours: float = Field(96, gt=0)       # hard cap (covers Friday after-close -> Monday)
    filings_lookback_days: int = Field(7, ge=1, le=60)
    form4_per_ticker: int = Field(5, ge=0, le=20)
    sec_max_tickers: int = Field(0, ge=0)          # 0 = all watchlist stocks per ingest
    story_similarity: float = Field(0.45, gt=0, lt=1)
    min_tag_confidence: float = Field(0.6, gt=0, le=1)  # summary-only mentions (0.45) don't count
    no_news_score: float = Field(40, ge=0, le=100)    # verified "no catalyst" is weaker than neutral
    halt_if_all_sources_fail: Literal[True] = True
    finbert_enabled: bool = True                      # local ProsusAI/finbert; silent fallback to lexicon+rules


class ValidationSection(_Strict):
    """Milestone 6: backtest validation gates the quant score component (spec sections 10 and 14)."""
    enabled: bool = True
    history_days_crypto: int = Field(730, ge=60)
    history_days_stock: int = Field(730, ge=60)
    oos_fraction: float = Field(0.4, gt=0.1, lt=0.9)        # last 40 % of the period is out-of-sample
    walk_forward_blocks: int = Field(6, ge=3, le=24)
    wf_min_train_trades: int = Field(10, ge=1)
    min_oos_trades: int = Field(30, ge=10)
    min_profit_factor: float = Field(1.3, ge=1.0)           # spec section 14 target
    max_drawdown_pct: float = Field(5.0, gt=0)              # spec section 14 target
    min_wf_positive_fraction: float = Field(0.6, gt=0, le=1)
    stress_cost_multiplier: float = Field(1.5, ge=1.0)
    max_age_days: float = Field(30, gt=0)                   # older validation -> quant unavailable
    auto_deploy_params: Literal[False] = False              # walk-forward winners are never auto-deployed
    # strategies that cannot be backtested (news-driven) may run a FORWARD PAPER TEST on the internal
    # ledger only (mirrored to Alpaca PAPER only, D85); technical strategies that FAILED validation stay rejected
    forward_test_strategies: list[str] = ["news_momentum"]
    forward_test_quant_score: float = Field(50.0, ge=0, le=100)   # neutral: no evidence either way yet
    forward_test_target_trades: int = Field(30, ge=10)
    # D93: moderately looser ENTRY gates for forward-test strategies (news_momentum) on the main paper ledger only.
    forward_test_min_score: int = Field(60, ge=0, le=100)           # default 60; D96 allows paper-scalp overlays below the old floor
    forward_test_min_reward_risk: float = Field(1.5, ge=1.5)        # was risk.min_reward_risk 2.0 (net of costs)
    # Shadow forward test (D91): evidence-based promotion / demotion only - never parameter optimisation
    # D96: when True (scalp overlay only), failed/unavailable strategies may paper-trade on the internal ledger
    paper_trade_unvalidated: bool = False
    shadow_enabled: bool = True
    shadow_min_trades: int = Field(30, ge=30)            # promotion needs >= 30 closed shadow trades
    shadow_window: int = Field(30, ge=30)                # rolling window for promotion / demotion stats
    shadow_promote_pf: float = Field(1.3, ge=1.3)        # promotion: PF >= 1.3, expectancy > 0 after costs
    shadow_demote_pf: float = Field(1.0, ge=1.0)         # demotion: rolling PF < 1.0 -> back to shadow


class HttpSection(_Strict):
    timeout_seconds: float = Field(10, gt=0, le=60)
    user_agent: str = "ATLAS-Trading-AI/0.1"


class Secrets(BaseModel):
    """Secrets from environment only. Never serialised, never logged."""

    model_config = ConfigDict(frozen=True)
    contact_email: str | None = None
    alpaca_api_key_id: SecretStr | None = None
    alpaca_api_secret_key: SecretStr | None = None
    x_bearer_token: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    newsapi_key: SecretStr | None = None


    def has(self, name: str) -> bool:
        v = getattr(self, name)
        if v is None:
            return False
        raw = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
        return bool(raw.strip())

    def secret_values(self) -> list[str]:
        out = []
        for f in ("alpaca_api_key_id", "alpaca_api_secret_key", "x_bearer_token",
                  "finnhub_api_key", "newsapi_key"):
            v = getattr(self, f)
            if v is not None and v.get_secret_value():
                out.append(v.get_secret_value())
        return out


_ENV_TO_SECRET = {
    "ATLAS_CONTACT_EMAIL": "contact_email",
    "ALPACA_API_KEY_ID": "alpaca_api_key_id",
    "ALPACA_API_SECRET_KEY": "alpaca_api_secret_key",
    "X_BEARER_TOKEN": "x_bearer_token",
    "FINNHUB_API_KEY": "finnhub_api_key",
    "NEWSAPI_KEY": "newsapi_key",
}


class Settings(_Strict):
    app: AppSection = AppSection()
    paths: PathsSection = PathsSection()
    execution: ExecutionSection = ExecutionSection()
    portfolio: PortfolioSection = PortfolioSection()
    risk: RiskSection = RiskSection()
    scoring: ScoringSection = ScoringSection()
    markets: MarketsSection = MarketsSection()
    data: DataSection = DataSection()
    scanner: ScannerSection = ScannerSection()
    news: NewsSection = NewsSection()
    validation: ValidationSection = ValidationSection()
    http: HttpSection = HttpSection()

    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    @property
    def db_path(self) -> Path:
        return self.paths.resolve(self.paths.database)

    @property
    def log_dir(self) -> Path:
        return self.paths.resolve(self.paths.log_dir)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE, '#' comments). Does not override real env."""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def load_settings(path: str | Path | None = None, overrides: dict | None = None) -> Settings:
    raw: dict = {}
    if DEFAULT_CONFIG_PATH.exists():
        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    extra = path or os.environ.get("ATLAS_CONFIG")
    if extra:
        raw = _deep_merge(raw, yaml.safe_load(Path(extra).read_text(encoding="utf-8")) or {})
    if overrides:
        raw = _deep_merge(raw, overrides)
    return Settings.model_validate(raw)


def load_secrets(env: dict[str, str] | None = None, dotenv_path: Path | None = None) -> Secrets:
    source = dict(_read_dotenv(dotenv_path or PROJECT_ROOT / ".env"))
    source.update(os.environ if env is None else env)
    vals = {}
    for env_name, field in _ENV_TO_SECRET.items():
        v = source.get(env_name, "").strip()
        if v:
            vals[field] = v
    return Secrets(**vals)
