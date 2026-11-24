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
    crypto_fee_bps: float = Field(60.0, ge=0)
    slippage_bps_stock: float = Field(5.0, ge=0)
    slippage_bps_crypto: float = Field(10.0, ge=0)


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


class Weights(_Strict):
    technical: float = 0.25
    momentum_volume: float = 0.20
    news: float = 0.20
    sentiment: float = 0.10
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
    min_score_to_trade: int = Field(70, ge=60, le=100)  # spec: below 60 = never trade
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
