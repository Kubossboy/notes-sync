"""D102: local LLM (news summary + APPROVE/VETO final check) with hard budget and no-LLM fallback."""
import json
from datetime import datetime, timezone

import pytest
import requests

import app.llm as L
from app.config import load_settings
from app.dashboard.plain import explain

NOW = datetime(2026, 11, 27, 20, tzinfo=timezone.utc)
SIG = {"signal_id": 5, "symbol": "SOL-USD", "asset_class": "crypto", "strategy": "sr_breakout", "entry": 140.0,
       "stop": 137.9, "target": 142.8, "rr": 1.25, "finbert": -0.4}


class _DB:
    def __init__(self):
        self.st, self.ev = {}, []

    def get_state(self, k, d=None):
        return self.st.get(k, d)

    def set_state(self, k, v):
        self.st[k] = json.loads(json.dumps(v))

    def log_event(self, et, mod, msg, sev="info", details=None):
        self.ev.append((et, msg))


class _Resp:
    def __init__(self, code=200, content=""):
        self.status_code, self._c = code, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


def _llm(monkeypatch, answer=None, health=200, exc=None, **cfg):
    s = load_settings(overrides={"llm": {"enabled": True, **cfg}})
    monkeypatch.setattr(L.requests, "get", lambda *a, **k: _Resp(health))

    def post(*a, **k):
        if exc:
            raise exc
        return _Resp(200, answer)
    monkeypatch.setattr(L.requests, "post", post)
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    return L.LocalLLM(s, _DB())


def test_veto_and_approve(monkeypatch):
    llm = _llm(monkeypatch, json.dumps({"decision": "VETO", "reason": "Exchange hack reported for this coin."}))
    assert llm.check(SIG, {"text": "x"}, [{"title": "hack", "sentiment": -0.9}]) == {
        "decision": "VETO", "reason": "Exchange hack reported for this coin."}
    llm = _llm(monkeypatch, json.dumps({"decision": "approve", "reason": "No negative news."}))
    assert llm.check(SIG, None, [])["decision"] == "APPROVE"


@pytest.mark.parametrize("kw", [dict(answer="not json"), dict(answer=json.dumps({"decision": "BUY MORE", "reason": "x"})),
                                dict(health=503), dict(exc=requests.Timeout("slow"))])
def test_any_failure_falls_back_to_no_llm(monkeypatch, kw):
    llm = _llm(monkeypatch, **kw)
    v = llm.check(SIG, None, [])
    assert v["decision"] == "SKIPPED"
    assert any(e[0] == "llm_fallback" for e in llm.db.ev)


def test_hard_budget(monkeypatch):
    llm = _llm(monkeypatch, json.dumps({"decision": "APPROVE", "reason": "ok"}), budget_s=10, min_call_s=4)
    llm.db.st[L.BUDGET_KEY] = {"run": "42", "spent": 7.0, "calls": 3, "fallbacks": 0, "timings": []}
    assert llm.check(SIG, None, [])["decision"] == "SKIPPED"            # 3 s left < 4 s -> no call
    llm.db.st[L.BUDGET_KEY]["run"] = "41"                               # new run -> fresh budget
    assert llm.check(SIG, None, [])["decision"] == "APPROVE"


def test_disabled_is_a_no_op(monkeypatch):
    llm = _llm(monkeypatch, "x", enabled=False)
    assert llm.check(SIG, None, [])["decision"] == "SKIPPED" and llm.db.ev == []


def test_summary_tidy_and_once_per_run():
    assert L.tidy_summary("Here:\n- NVDA fell 2% on a downgrade.\n- BTC rose.\n- AAPL cut off mid") == \
        "- NVDA fell 2% on a downgrade.\n- BTC rose."
    assert L.tidy_summary("no bullets") is None


def test_plain_english_veto():
    assert explain("llm veto: Exchange hack reported.") == "Blocked by the AI final check: Exchange hack reported."


def test_default_config():
    c = load_settings().llm
    assert c.enabled and c.budget_s == 60 and c.max_candidates == 3 and c.url.startswith("http://127.0.0.1")
