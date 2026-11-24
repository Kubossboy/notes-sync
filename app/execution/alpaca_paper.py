"""Alpaca PAPER broker adapter (spec section 7: preferred paper provider).

* Hard-wired to https://paper-api.alpaca.markets - every URL passes assert_url_allowed,
  and the live host api.alpaca.markets is on the forbidden list.
* Order submission is OFF unless `execution.alpaca_paper_orders_enabled: true`
  (default false). When on, it only MIRRORS internal paper trades as bracket
  orders; ATLAS's own $1,000 ledger stays authoritative (the Alpaca paper account
  has its own $100k default balance).
* Read-only helpers (account, asset eligibility, order status) never place orders.
* Keys come from env / .env only and are never logged.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from app.config import Secrets, Settings
from app.core.safety import assert_url_allowed

BASE = "https://paper-api.alpaca.markets"


class AlpacaOrdersDisabled(RuntimeError):
    pass


@dataclass
class AssetInfo:
    symbol: str
    tradable: bool
    fractionable: bool
    asset_class: str
    status: str


class AlpacaPaperBroker:
    def __init__(self, settings: Settings, secrets: Secrets, session: requests.Session | None = None):
        self.s, self.secrets = settings, secrets
        self.session = session or requests.Session()
        self.timeout = settings.http.timeout_seconds

    @property
    def configured(self) -> bool:
        return self.secrets.has("alpaca_api_key_id") and self.secrets.has("alpaca_api_secret_key")

    @property
    def orders_enabled(self) -> bool:
        return bool(self.s.execution.alpaca_paper_orders_enabled) and self.configured

    def _headers(self) -> dict:
        return {"APCA-API-KEY-ID": self.secrets.alpaca_api_key_id.get_secret_value(),
                "APCA-API-SECRET-KEY": self.secrets.alpaca_api_secret_key.get_secret_value()}

    def _req(self, method: str, path: str, **kw):
        url = BASE + path
        assert url.startswith("https://paper-api.alpaca.markets/")
        assert_url_allowed(url)
        r = self.session.request(method, url, headers=self._headers(), timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise ConnectionError(f"alpaca paper {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.text else {}

    # ---------------------------------------------------------- read-only
    def asset(self, symbol: str) -> AssetInfo:
        sym = symbol.replace("-", "/") if symbol.endswith("-USD") else symbol
        j = self._req("GET", f"/v2/assets/{requests.utils.quote(sym, safe='')}")
        return AssetInfo(j.get("symbol", sym), bool(j.get("tradable")), bool(j.get("fractionable")),
                         j.get("class", "?"), j.get("status", "?"))

    def account(self) -> dict:
        """Read-only paper account summary (no account number / ids returned)."""
        j = self._req("GET", "/v2/account")
        return {k: j.get(k) for k in ("status", "currency", "cash", "equity", "buying_power", "crypto_status",
                                      "trading_blocked", "account_blocked", "pattern_day_trader")}

    def open_orders(self, symbol: str) -> list[dict]:
        return self._req("GET", "/v2/orders", params={"status": "open", "symbols": symbol, "nested": "true"})

    def order_by_client_id(self, client_order_id: str) -> dict:
        return self._req("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})

    # ---------------------------------------------------------- orders (flag-gated)
    def submit_bracket(self, symbol: str, qty: float, stop: float, target: float, client_order_id: str) -> dict:
        if not self.orders_enabled:
            raise AlpacaOrdersDisabled("execution.alpaca_paper_orders_enabled is false (or no keys)")
        body = {"symbol": symbol, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day",
                "client_order_id": client_order_id, "order_class": "bracket",
                "take_profit": {"limit_price": f"{target:.2f}"}, "stop_loss": {"stop_price": f"{stop:.2f}"}}
        return self._req("POST", "/v2/orders", json=body)

    def submit_market(self, symbol: str, qty: float, client_order_id: str, crypto: bool) -> dict:
        """Plain market buy for what a bracket can't carry (crypto, fractional shares). Exit is mirrored on close."""
        if not self.orders_enabled:
            raise AlpacaOrdersDisabled("execution.alpaca_paper_orders_enabled is false (or no keys)")
        body = {"symbol": symbol, "qty": f"{qty:.9f}".rstrip("0").rstrip("."), "side": "buy", "type": "market",
                "time_in_force": "gtc" if crypto else "day", "client_order_id": client_order_id}
        return self._req("POST", "/v2/orders", json=body)

    def replace_order(self, order_id: str, **fields) -> dict:
        """Replace a live bracket leg (stop_price / limit_price) - used only to TIGHTEN a stop or SHRINK a target."""
        if not self.orders_enabled:
            raise AlpacaOrdersDisabled("execution.alpaca_paper_orders_enabled is false (or no keys)")
        return self._req("PATCH", f"/v2/orders/{order_id}", json={k: f"{v:.2f}" for k, v in fields.items()})

    def close_position(self, symbol: str) -> dict:
        """Cancel any open legs for the symbol, then liquidate. Already-flat (404) is fine."""
        if not self.orders_enabled:
            raise AlpacaOrdersDisabled("execution.alpaca_paper_orders_enabled is false (or no keys)")
        for o in self.open_orders(symbol) or []:
            try:
                self._req("DELETE", f"/v2/orders/{o['id']}")
            except ConnectionError:
                pass
        try:
            return self._req("DELETE", f"/v2/positions/{requests.utils.quote(symbol.replace('/', ''), safe='')}")
        except ConnectionError as e:
            if "HTTP 404" in str(e):
                return {"status": "already_flat"}
            raise
