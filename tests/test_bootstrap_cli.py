import json

import pytest

from app import main as cli
from app.core.bootstrap import bootstrap
from app.core.environment import inspect_environment
from app.config import PROJECT_ROOT, Secrets


def test_bootstrap_creates_db_snapshot_and_startup_event(settings):
    ctx = bootstrap(settings, Secrets(), console_log=False)
    try:
        assert settings.db_path.exists()
        n = ctx.db.conn.execute("SELECT COUNT(*) FROM config_snapshots").fetchone()[0]
        assert n == 1
        assert ctx.db.conn.execute(
            "SELECT COUNT(*) FROM system_events WHERE event_type='startup'").fetchone()[0] == 1
        assert ctx.safe_state.trading_allowed()
    finally:
        ctx.close()
    ctx2 = bootstrap(settings, Secrets(), console_log=False)  # same config: no new snapshot
    try:
        assert ctx2.db.conn.execute("SELECT COUNT(*) FROM config_snapshots").fetchone()[0] == 1
    finally:
        ctx2.close()


def test_config_change_is_journaled(settings, tmp_path):
    from app.config import load_settings
    bootstrap(settings, Secrets(), console_log=False).close()
    changed = load_settings(overrides={"paths": {"database": str(settings.db_path),
                                                 "log_dir": str(settings.log_dir)},
                                       "app": {"environment": "test"},
                                       "risk": {"max_open_positions": 1}})
    ctx = bootstrap(changed, Secrets(), console_log=False)
    try:
        assert ctx.db.conn.execute(
            "SELECT COUNT(*) FROM system_events WHERE event_type='config_changed'").fetchone()[0] == 1
    finally:
        ctx.close()


def test_corrupt_database_enters_safe_state_or_fails_loudly(settings):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.db_path.write_bytes(b"this is not a sqlite database" * 100)
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        bootstrap(settings, Secrets(), console_log=False)


def test_environment_inspection():
    env = inspect_environment(PROJECT_ROOT)
    assert env["python"].startswith("3.")
    assert env["disk_free_gb"] > 0 and env["project_root_writable"]


def test_cli_status_and_halt(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli, "bootstrap", lambda console_log=True: bootstrap(settings, Secrets(), console_log=False))
    assert cli.main(["halt", "manual", "test"]) == 0
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["execution_mode"] == "paper" and st["safe_state"]["halted"] is True
    assert st["risk"]["max_risk_per_trade_usd"] == 2.5 and st["risk"]["max_daily_loss_usd"] == 10.0
    assert cli.main(["resume", "--by", "test"]) == 0
    capsys.readouterr()
    cli.main(["status"])
    assert json.loads(capsys.readouterr().out)["safe_state"]["halted"] is False
