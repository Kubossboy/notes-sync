"""ATLAS Trading AI command-line entry point.

Usage:  python -m app.main <command>
  init        create/migrate the database and record a startup event
  env         inspect the host environment (JSON)
  probe       check every free data source / integration and store results
  status      show configuration, safe state, credentials and last probe results
  halt REASON put the system into SAFE STATE (no new simulated trades)
  resume      clear SAFE STATE (explicit human action, journaled)
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import PROJECT_ROOT
from app.core.bootstrap import VERSION, bootstrap
from app.core.environment import inspect_environment
from app.integrations.registry import INTEGRATIONS, probe_all


def cmd_status(ctx) -> dict:
    s, db = ctx.settings, ctx.db
    st = ctx.safe_state.status()
    rows = db.conn.execute(
        "SELECT integration, category, status, latency_ms, detail, ts FROM integration_health "
        "WHERE id IN (SELECT MAX(id) FROM integration_health GROUP BY integration) ORDER BY category, integration"
    ).fetchall()
    eq = s.portfolio.starting_equity_usd
    return {
        "version": VERSION,
        "execution_mode": s.execution.mode,
        "provider": s.execution.provider,
        "safe_state": {"halted": st.halted, "reason": st.reason, "since": st.since},
        "portfolio_start_usd": eq,
        "risk": {
            "max_risk_per_trade_usd": round(eq * s.risk.max_risk_per_trade_pct / 100, 2),
            "max_daily_loss_usd": round(eq * s.risk.max_daily_loss_pct / 100, 2),
            "max_open_positions": s.risk.max_open_positions,
            "min_reward_risk": s.risk.min_reward_risk,
            "leverage": s.execution.leverage_enabled,
        },
        "config_hash": s.config_hash(),
        "credentials_present": {i.name: all(ctx.secrets.has(r) for r in i.requires)
                                for i in INTEGRATIONS if i.requires},
        "contact_email_set": ctx.secrets.has("contact_email"),
        "db_tables": len(db.tables()),
        "last_probes": [dict(r) for r in rows],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atlas", description="ATLAS Trading AI (paper trading only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init"); sub.add_parser("env"); sub.add_parser("status")
    pr = sub.add_parser("probe"); pr.add_argument("--only", nargs="*")
    h = sub.add_parser("halt"); h.add_argument("reason", nargs="+")
    r = sub.add_parser("resume"); r.add_argument("--by", default="operator"); r.add_argument("--note", default="")
    a = p.parse_args(argv)

    if a.cmd == "env":
        print(json.dumps(inspect_environment(PROJECT_ROOT), indent=2))
        return 0

    ctx = bootstrap(console_log=a.cmd != "status")
    try:
        if a.cmd == "init":
            print(json.dumps({"db": str(ctx.settings.db_path), "tables": ctx.db.tables(),
                              "integrity": ctx.db.integrity_ok()[1]}, indent=2))
        elif a.cmd == "probe":
            res = probe_all(ctx.settings, ctx.secrets, ctx.db, a.only)
            for x in res:
                lat = f"{x.latency_ms:.0f}ms" if x.latency_ms is not None else "-"
                print(f"{x.status:18} {x.integration:26} {x.category:20} {lat:>7}  {x.detail}")
            essential_down = [x for x in res if x.category.startswith("market_data") and x.status != "ok"]
            ok = sum(x.status == "ok" for x in res)
            print(f"\n{ok}/{len(res)} integrations ok")
            return 1 if essential_down and len(essential_down) == sum(
                x.category.startswith("market_data") for x in res) else 0
        elif a.cmd == "status":
            print(json.dumps(cmd_status(ctx), indent=2, default=str))
        elif a.cmd == "halt":
            ctx.safe_state.halt(" ".join(a.reason), module="operator")
            print("SAFE STATE: halted")
        elif a.cmd == "resume":
            ctx.safe_state.resume(a.by, a.note)
            print("SAFE STATE: cleared")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
