"""X (Twitter) API v2 recent search - ONLY active when X_BEARER_TOKEN is configured.

Without a token this module reports needs_credentials and returns nothing;
ATLAS never claims to monitor X without a working API (spec section 3).
Posts are tier 4 (social): never verified on their own, and a positive social
score can never override negative fundamentals or structure (enforced in
app.sentiment.aggregate).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import requests

from app.config import Secrets
from app.core.safety import assert_url_allowed
from app.intelligence.feeds import RawItem, parse_date

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"


@dataclass
class XStatus:
    enabled: bool
    detail: str


class XSource:
    def __init__(self, secrets: Secrets, session: requests.Session, timeout: float = 10):
        self.secrets, self.session, self.timeout = secrets, session, timeout

    def status(self) -> XStatus:
        if not self.secrets.has("x_bearer_token"):
            return XStatus(False, "needs_credentials: X_BEARER_TOKEN not set")
        return XStatus(True, "configured")

    def search(self, query: str, max_results: int = 25) -> list[RawItem]:
        if not self.status().enabled:
            return []
        assert_url_allowed(SEARCH_URL)
        r = self.session.get(SEARCH_URL, timeout=self.timeout, params={
            "query": f"{query} -is:retweet lang:en", "max_results": max(10, min(max_results, 100)),
            "tweet.fields": "created_at,public_metrics,author_id"},
            headers={"Authorization": f"Bearer {self.secrets.x_bearer_token.get_secret_value()}"})
        if r.status_code != 200:
            raise ConnectionError(f"X API HTTP {r.status_code}")
        out = []
        for t in r.json().get("data", []) or []:
            out.append(RawItem("x_api_v2", t.get("text", "")[:280], f"https://x.com/i/web/status/{t.get('id')}",
                               parse_date(t.get("created_at")), "", t.get("id"),
                               {"metrics": t.get("public_metrics"), "author_id": t.get("author_id")}))
        return out
