"""D104 learned parameters: a small, bounded grid per strategy, versioned in var/params.json (state branch).

Learning may ONLY move the keys in GRID, one grid step at a time. Hard safety rails are not in the grid and
can never be changed by learning: min R:R (1.2+), every risk limit, leverage caps, kill switch, fee model.
Any value in the file that is not an allowed grid value is ignored (logged by the caller)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

GRID: dict[str, list[float]] = {
    "swing.rsi_max": [3.0, 4.0, 5.0, 6.0, 7.0],                 # RSI(2) dip threshold (overnight dip-buy)
    "swing.atr_mult": [1.5, 2.0, 2.5, 3.0],                     # its stop, x average daily range
    "risk.atr_stop_mult": [1.25, 1.5, 1.75, 2.0],               # crypto ATR stop
    "risk.atr_target_mult": [1.75, 2.0, 2.5, 3.0],              # crypto ATR target
    "scanner.crypto_min_rvol": [0.0, 1.0, 1.2, 1.5, 2.0],       # crypto volume-ratio floor
}
GROUP = {"swing.rsi_max": "rsi2_swing", "swing.atr_mult": "rsi2_swing", "risk.atr_stop_mult": "crypto",
         "risk.atr_target_mult": "crypto", "scanner.crypto_min_rvol": "crypto"}
LABEL = {"swing.rsi_max": "RSI threshold", "swing.atr_mult": "stop distance (x daily range)",
         "risk.atr_stop_mult": "stop distance (x ATR)", "risk.atr_target_mult": "target distance (x ATR)",
         "scanner.crypto_min_rvol": "minimum volume ratio"}
MIN_RR = 1.2                       # hard rail: target/stop distance ratio never below this


def path() -> Path | None:
    p = os.environ.get("ATLAS_PARAMS")
    return Path(p) if p else None


def load(p: Path | None = None) -> dict:
    p = p or path()
    if p and p.exists():
        try:
            d = json.loads(p.read_text())
            if isinstance(d, dict):
                return {"version": int(d.get("version", 0)), "params": dict(d.get("params") or {}),
                        "history": list(d.get("history") or []), "last_run": d.get("last_run")}
        except (ValueError, TypeError):
            pass
    return {"version": 0, "params": {}, "history": []}


def save(doc: dict, p: Path | None = None) -> None:
    p = p or path()
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=1, default=str) + "\n")


def valid(params: dict, base: dict | None = None) -> dict:
    """Only grid keys with grid values; crypto target/stop ratio >= MIN_RR (else both dropped)."""
    out = {k: float(v) for k, v in params.items() if k in GRID and isinstance(v, (int, float)) and float(v) in GRID[k]}
    base = base or {}
    sl = out.get("risk.atr_stop_mult", base.get("risk.atr_stop_mult", 1.5))
    tp = out.get("risk.atr_target_mult", base.get("risk.atr_target_mult", 2.0))
    if tp / sl < MIN_RR:
        out.pop("risk.atr_stop_mult", None)
        out.pop("risk.atr_target_mult", None)
    return out


def as_overrides(params: dict) -> dict:
    nested: dict = {}
    for k, v in params.items():
        sec, key = k.split(".", 1)
        nested.setdefault(sec, {})[key] = v
    return nested


def current(settings, key: str) -> float:
    sec, k = key.split(".", 1)
    return float(getattr(getattr(settings, sec), k))


def neighbours(key: str, value: float) -> list[float]:
    g = GRID[key]
    if value not in g:
        return []
    i = g.index(value)
    return [g[j] for j in (i - 1, i + 1) if 0 <= j < len(g)]


def record(doc: dict, kind: str, key: str, old: float, new: float, reason: str, evidence: dict | None = None,
           now: datetime | None = None) -> dict:
    """Append a version (param_change / param_revert) and set the value."""
    doc["version"] = int(doc.get("version", 0)) + 1
    doc["params"][key] = new
    doc["history"].append({"version": doc["version"], "ts": (now or datetime.now(timezone.utc)).isoformat(),
                           "kind": kind, "key": key, "group": GROUP[key], "old": old, "new": new,
                           "reason": reason, "evidence": evidence or {}})
    return doc


def sentence(h: dict) -> str:
    verb = "Raised" if h["new"] > h["old"] else "Lowered"
    who = "Overnight dip-buy (RSI-2)" if h["group"] == "rsi2_swing" else "crypto strategies"
    pre = "Reverted: " if h["kind"] == "param_revert" else ""
    return f"{pre}{verb} {LABEL[h['key']]} {h['old']:g}→{h['new']:g} for {who} because {h['reason']}"
