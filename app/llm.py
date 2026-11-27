"""D102: local Qwen2.5-3B-Instruct (Q4_K_M GGUF on llama.cpp's llama-server, CPU) for two narrow jobs:
1) a short plain-English summary of the most important news (once per run), 2) a final APPROVE / VETO check of the
top 1-3 candidates right before entry. It can only BLOCK an entry, never add one or loosen a rule.
Hard time budget per run (system_state, survives the per-cycle processes). Any timeout / load failure / bad output
-> no-LLM behaviour (the entry proceeds as before) and a logged event. FinBERT stays the fast scorer."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from app.core.logging_setup import get_logger

log = get_logger("llm")
BUDGET_KEY, SUMMARY_KEY, LOG_KEY = "llm_budget", "llm_news_summary", "llm_verdicts"

VETO_SCHEMA = {"type": "object", "properties": {
    "decision": {"type": "string", "enum": ["APPROVE", "VETO"]},
    "reason": {"type": "string", "maxLength": 200}}, "required": ["decision", "reason"]}

SYSTEM_CHECK = ("You are a cautious risk checker for a paper-trading bot that only buys (long). The setup already "
                "passed every rule, including the market-regime filter, so never veto because of the regime. VETO only "
                "for a concrete reason in the headlines: clearly negative news for THIS asset (hack, outage, lawsuit, "
                "delisting, fraud, big downgrade) or news that the move is already over. Otherwise APPROVE. Answer JSON: {\"decision\": \"APPROVE\"|\"VETO\", \"reason\": one short plain-English sentence}.")
SYSTEM_SUMMARY = ("Summarise the most important market news for a beginner in at most 4 short bullet points "
                  "(\"- \"), plain English, no jargon, mention the ticker in each point. No advice.")


def tidy_summary(txt: str | None, max_points: int = 4) -> str | None:
    """Keep up to 4 complete '- ' bullet points (drops a point cut off by the token limit)."""
    if not txt:
        return None
    pts = [ln.strip() for ln in txt.splitlines() if ln.strip().startswith(("-", "*", "•"))]
    pts = ["- " + p.lstrip("-*• ").strip() for p in pts]
    pts = [p for p in pts if p.rstrip()[-1:] in ".!?)%\"'" and len(p) > 6][:max_points]
    return "\n".join(pts) or None


def _run_id() -> str:
    return os.environ.get("GITHUB_RUN_ID") or datetime.now(timezone.utc).strftime("local-%Y%m%d%H")


class LocalLLM:
    def __init__(self, settings, db):
        self.cfg, self.db = settings.llm, db

    # ------------------------------------------------------------ budget
    def _budget(self) -> dict:
        b = self.db.get_state(BUDGET_KEY) or {}
        if b.get("run") != _run_id():
            b = {"run": _run_id(), "spent": 0.0, "calls": 0, "fallbacks": 0, "timings": []}
        return b

    def remaining(self) -> float:
        return max(0.0, self.cfg.budget_s - self._budget()["spent"])

    def _account(self, secs: float, what: str, ok: bool):
        b = self._budget()
        b["spent"] = round(b["spent"] + secs, 2)
        b["calls"] += 1
        b["fallbacks"] += 0 if ok else 1
        b["timings"] = (b["timings"] + [{"what": what, "s": round(secs, 2), "ok": ok}])[-20:]
        self.db.set_state(BUDGET_KEY, b)

    def _fallback(self, what: str, why: str, secs: float = 0.0):
        self._account(secs, what, False)
        self.db.log_event("llm_fallback", "llm", f"{what}: {why}"[:300], "warning")
        log.warning("LLM fallback (%s): %s", what, why)

    # ------------------------------------------------------------ transport
    def ready(self) -> bool:
        if not self.cfg.enabled:
            return False
        try:
            r = requests.get(self.cfg.url + "/health", timeout=1.0)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _chat(self, system: str, user: str, max_tokens: int, what: str, schema: dict | None = None) -> str | None:
        if not self.cfg.enabled:
            return None
        left = self.remaining()
        if left < self.cfg.min_call_s:
            self._fallback(what, f"time budget used up ({self.cfg.budget_s:g}s per run)")
            return None
        if not self.ready():
            self._fallback(what, "model server not ready (loading or not started)")
            return None
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens, "temperature": 0.1, "cache_prompt": True}
        if schema:
            body["response_format"] = {"type": "json_object", "schema": schema}
        t0 = time.monotonic()
        try:
            r = requests.post(self.cfg.url + "/v1/chat/completions", json=body,
                              timeout=min(self.cfg.call_timeout_s, left))
            r.raise_for_status()
            txt = r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            self._fallback(what, f"{type(e).__name__}: {e}", time.monotonic() - t0)
            return None
        self._account(time.monotonic() - t0, what, True)
        return txt

    # ------------------------------------------------------------ job 1: news summary
    def summarize_news(self, now: datetime) -> dict | None:
        cur = self.db.get_state(SUMMARY_KEY)
        if cur and cur.get("run") == _run_id():
            return cur                                   # once per run
        since = (now - timedelta(hours=self.cfg.summary_hours)).isoformat()
        rows = self.db.conn.execute(
            "SELECT title, tickers, sentiment, impact FROM news_events WHERE COALESCE(published_ts, fetched_ts) >= ? "
            "AND tickers != '' ORDER BY ABS(COALESCE(sentiment, 0)) DESC, id DESC LIMIT ?",
            (since, self.cfg.summary_headlines)).fetchall()
        if not rows:
            return cur
        lines = [f"[{(r['tickers'] or '').strip(',')}] {r['title']} (FinBERT {r['sentiment'] or 0:+.2f})" for r in rows]
        txt = self._chat(SYSTEM_SUMMARY, "Headlines:\n" + "\n".join(lines), self.cfg.summary_max_tokens, "summary")
        txt = tidy_summary(txt)
        if not txt:
            return cur
        out = {"run": _run_id(), "ts": now.isoformat(), "text": txt.strip()[:1200], "headlines": len(rows),
               "model": self.cfg.model_name}
        self.db.set_state(SUMMARY_KEY, out)
        return out

    # ------------------------------------------------------------ job 2: final check
    def check(self, sig: dict, regime: dict | None, headlines: list[dict]) -> dict:
        """Returns {"decision": APPROVE|VETO|SKIPPED, "reason": str}. SKIPPED = no-LLM fallback (entry proceeds)."""
        hl = "\n".join(f"- {h['title']} (FinBERT {h.get('sentiment') or 0:+.2f})" for h in headlines[:6]) or "- none"
        user = (f"Asset: {sig['symbol']} ({sig['asset_class']}), strategy: {sig['strategy']}\n"
                f"Entry {sig['entry']:.6g}, stop-loss {sig['stop']:.6g}, take-profit {sig['target']:.6g}, "
                f"net reward/risk {sig.get('rr') or 0:.2f}\n"
                f"Market regime: {regime.get('text') if regime else 'unknown'}\n"
                f"FinBERT news score for the asset: {sig.get('finbert') if sig.get('finbert') is not None else 'n/a'}\n"
                f"Recent headlines:\n{hl}")
        txt = self._chat(SYSTEM_CHECK, user, self.cfg.check_max_tokens, f"check {sig['symbol']}", VETO_SCHEMA)
        if txt is None:
            return {"decision": "SKIPPED", "reason": "AI check unavailable, normal rules apply"}
        try:
            j = json.loads(txt)
            dec = str(j["decision"]).upper()
            reason = " ".join(str(j["reason"]).split())[:200]
            if dec not in ("APPROVE", "VETO") or not reason:
                raise ValueError("bad fields")
        except Exception as e:
            self._fallback(f"check {sig['symbol']}", f"bad output ({type(e).__name__}): {txt[:120]!r}")
            return {"decision": "SKIPPED", "reason": "AI check gave an unreadable answer, normal rules apply"}
        return {"decision": dec, "reason": reason}

    def record(self, sig: dict, verdict: dict, now: datetime):
        lg = (self.db.get_state(LOG_KEY) or [])[-29:]
        lg.append({"ts": now.isoformat(), "symbol": sig["symbol"], "strategy": sig["strategy"],
                   "signal_id": sig["signal_id"], **verdict})
        self.db.set_state(LOG_KEY, lg)
        self.db.log_event("llm_" + verdict["decision"].lower(), "llm",
                          f"{sig['symbol']}: {verdict['decision']} - {verdict['reason']}"[:300])


# ---------------------------------------------------------------- D104: one-sentence lesson for a closed trade
SYSTEM_LESSON = ("You review one closed paper trade of a trading bot. Write ONE short plain-English sentence (max 25 "
                 "words) with the most useful lesson, based only on the facts given. No advice to change risk limits.")
LESSON_SCHEMA = {"type": "object", "properties": {"lesson": {"type": "string"}}, "required": ["lesson"]}


def lesson(llm: "LocalLLM", facts: str) -> str | None:
    txt = llm._chat(SYSTEM_LESSON, facts, 60, "lesson", LESSON_SCHEMA)
    if not txt:
        return None
    try:
        s = " ".join(str(json.loads(txt)["lesson"]).split())[:220]
        return s or None
    except Exception as e:
        llm._fallback("lesson", f"bad output ({type(e).__name__})")
        return None
