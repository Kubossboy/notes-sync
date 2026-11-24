"""QUANT ENGINE - turns backtest validation into the quant score component (spec sections 5, 10).

The quant component of a signal is:
  None (signal INCOMPLETE, never executed) when the strategy has no validation, is not backtestable,
       has too little out-of-sample data, its validation is stale (> validation.max_age_days), or the
       live parameters / cost model differ from what was validated;
  0 + a plan problem (signal REJECTED) when the strategy FAILED validation (no edge after costs);
  0-100 for a VALIDATED strategy: 50 % validation strength (OOS profit factor, walk-forward
       consistency), 25 % relative strength vs the benchmark (SPY / BTC-USD), 25 % volatility vs
       round-trip cost. Expected value comes from the OOS expectancy (R) - never from in-sample.
An unvalidated statistical relationship is never presented as a predictive edge.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from app.backtest.engine import NOT_BACKTESTABLE
from app.backtest.validation import params_hash, strategy_params
from app.config import Settings
from app.data.models import AssetClass
from app.strategies.base import val

COST_KEYS = ("crypto_fee_bps", "stock_fee_bps", "slippage_bps_crypto", "slippage_bps_stock",
             "assumed_half_spread_bps_crypto", "assumed_half_spread_bps_stock", "commission_per_trade_usd")


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


class QuantEngine:
    def __init__(self, settings: Settings, db):
        self.s, self.db = settings, db
        self._cache: dict = {}

    def latest(self, ac: AssetClass, strategy: str) -> dict | None:
        key = (ac.value, strategy)
        if key not in self._cache:
            r = self.db.conn.execute("SELECT * FROM backtest_runs WHERE asset_class=? AND strategy=? "
                                     "ORDER BY id DESC LIMIT 1", (ac.value, strategy)).fetchone()
            self._cache[key] = dict(r) if r else None
        return self._cache[key]

    def status(self, ac: AssetClass, strategy: str, now: datetime | None = None) -> dict:
        """{'state': validated|failed|unavailable, 'reason', 'run'} for the live parameters."""
        if not self.s.validation.enabled:
            return {"state": "unavailable", "reason": "validation disabled in config"}
        if strategy in NOT_BACKTESTABLE:
            if strategy in self.s.validation.forward_test_strategies:
                return {"state": "forward_test", "reason": f"FORWARD PAPER TEST (not backtestable: "
                        f"{NOT_BACKTESTABLE[strategy]}) - internal $1,000 ledger is the source of truth (mirrored to Alpaca PAPER when enabled); "
                        "unvalidated, judged after >= " f"{self.s.validation.forward_test_target_trades} trades"}
            return {"state": "unavailable", "reason": f"not backtestable: {NOT_BACKTESTABLE[strategy]}"}
        r = self.latest(ac, strategy)
        if r is None:
            return {"state": "unavailable", "reason": "no backtest validation yet (run `python -m app.main backtest`)"}
        live = strategy_params(self.s, ac).get(strategy, {})
        if params_hash(live) != r["params_hash"]:
            return {"state": "unavailable", "reason": "live parameters differ from the validated ones - re-run backtest",
                    "run_id": r["id"]}
        cm = json.loads(r["cost_model_json"])
        if any(abs(float(cm.get(k, -1)) - float(getattr(self.s.execution, k))) > 1e-9 for k in COST_KEYS):
            return {"state": "unavailable", "reason": "cost model changed since validation - re-run backtest",
                    "run_id": r["id"]}
        now = now or datetime.now(timezone.utc)
        age_d = (now - datetime.fromisoformat(r["ts"])).total_seconds() / 86400
        if age_d > self.s.validation.max_age_days:
            return {"state": "unavailable", "reason": f"validation is stale ({age_d:.0f} d > "
                                                      f"{self.s.validation.max_age_days:.0f} d)", "run_id": r["id"]}
        reasons = json.loads(r["reasons_json"])
        if r["verdict"] == "failed":
            return {"state": "failed", "reason": "; ".join(reasons), "run_id": r["id"]}
        if r["verdict"] != "validated":
            return {"state": "unavailable", "reason": f"{r['verdict']}: " + "; ".join(reasons), "run_id": r["id"]}
        return {"state": "validated", "reason": reasons[0] if reasons else "validated", "run_id": r["id"],
                "metrics": json.loads(r["metrics_json"])}

    def assess(self, ctx, setup, plan, bench_ret: float | None = None) -> dict:
        st = self.status(ctx.asset_class, setup.strategy, ctx.now)
        out = {"score": None, "problems": [], "state": st["state"], "reason": st["reason"],
               "run_id": st.get("run_id")}
        if st["state"] in ("failed", "unavailable") and self.s.validation.shadow_enabled:
            from app.shadow.engine import ShadowEngine
            sh = ShadowEngine(self.s, self.db)
            if sh.status(setup.strategy, ctx.asset_class.value) == "eligible":   # D91: promoted on live evidence
                _, _, allt, ws = sh.decide(setup.strategy, ctx.asset_class.value)
                out.update(state="shadow_promoted", score=float(self.s.validation.forward_test_quant_score),
                           reason=f"PROMOTED from the shadow forward test ({allt['n']} trades; rolling PF "
                                  f"{ws['profit_factor']}, expectancy {ws['expectancy_r']} R) - backtest said: {st['reason'][:120]}",
                           problems=[], evidence={"shadow": ws, "note": "neutral quant score; still under all M5 risk limits"})
                return out
        if st["state"] == "failed":
            out["score"] = 0.0
            out["problems"] = [f"strategy failed backtest validation (run #{st['run_id']}): {st['reason'][:200]}"]
            return out
        if st["state"] == "forward_test":
            out["score"] = float(self.s.validation.forward_test_quant_score)
            out["evidence"] = {"forward_test": forward_test_stats(self.db, setup.strategy),
                               "note": "neutral quant score - no statistical evidence of an edge yet"}
            return out
        if st["state"] != "validated":
            return out
        m = st["metrics"]
        o, wf = m["out_of_sample"], m["walk_forward"]
        pf = o.get("profit_factor") or 0.0
        strength = 0.7 * _clamp((pf - 1.0) * 100) + 0.3 * 100 * (wf.get("positive_fraction") or 0.0)
        df = ctx.df
        rs = 50.0
        ap = val(ctx.last, "atr_pct")
        if bench_ret is not None and len(df) > 21 and ap:
            ret = float(df["close"].iloc[-1] / df["close"].iloc[-21] - 1) * 100
            rs = _clamp(50 + (ret - bench_ret) / (ap * math.sqrt(20)) * 50)
        vc = _clamp(((ap or 0) / (2 * ctx.cost_bps_per_side / 100) - 1) / 3 * 100) if ctx.cost_bps_per_side else 50.0
        out["score"] = round(0.5 * strength + 0.25 * rs + 0.25 * vc, 2)
        ev_r = o.get("expectancy_r")
        out["evidence"] = {"oos_trades": o["trades"], "oos_profit_factor": pf, "oos_win_rate": o.get("win_rate"),
                           "oos_max_drawdown_pct": o.get("max_drawdown_pct"), "wf_positive_fraction":
                           wf.get("positive_fraction"), "validation_strength": round(strength, 1),
                           "relative_strength": round(rs, 1), "volatility_vs_cost": round(vc, 1),
                           "expected_value_r": ev_r,
                           "expected_value_per_unit": None if ev_r is None else round(ev_r * plan.risk_per_unit, 8)}
        return out

    def summary(self) -> list[dict]:
        rows = []
        for ac in (AssetClass.CRYPTO, AssetClass.STOCK):
            for name in strategy_params(self.s, ac):
                st = self.status(ac, name)
                r = self.latest(ac, name)
                o = json.loads(r["metrics_json"]).get("out_of_sample", {}) if r else {}
                rows.append({"asset": ac.value, "strategy": name, "quant": st["state"],
                             "verdict": r["verdict"] if r else None, "run": r["id"] if r else None,
                             "validated_at": r["ts"] if r else None, "oos_trades": o.get("trades"),
                             "oos_pf": o.get("profit_factor"), "oos_exp_r": o.get("expectancy_r"),
                             "reason": st["reason"][:160]})
        return rows


def forward_test_stats(db, strategy: str | None = None) -> dict:
    """Stats of closed forward-test trades (table forward_test_trades) + currently open ones."""
    q = "SELECT * FROM forward_test_trades" + (" WHERE strategy=?" if strategy else "") + " ORDER BY closed_ts"
    rows = [dict(r) for r in db.conn.execute(q, (strategy,) if strategy else ()).fetchall()]
    open_n = db.conn.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND validation_status='forward_test'"
                             + (" AND strategy=?" if strategy else ""), (strategy,) if strategy else ()).fetchone()[0]
    pnl = [r["pnl_usd"] for r in rows]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    return {"closed_trades": len(rows), "open_trades": open_n,
            "win_rate": round(len(wins) / len(rows), 4) if rows else None,
            "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
            "net_pnl_usd": round(sum(pnl), 4), "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
            "max_drawdown_usd": round(dd, 4), "fees_usd": round(sum(r["fees_usd"] for r in rows), 4)}
