"""ATLAS CORE bootstrap: wires config, logging, database and safety together."""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import PROJECT_ROOT, Secrets, Settings, load_secrets, load_settings
from app.core.logging_setup import get_logger, setup_logging
from app.core.safety import SafeState, assert_paper_only
from app.database.db import Database, utcnow

VERSION = "0.7.2-m7"


@dataclass
class AtlasContext:
    settings: Settings
    secrets: Secrets
    db: Database
    safe_state: SafeState

    def close(self) -> None:
        self.db.close()


def bootstrap(settings: Settings | None = None, secrets: Secrets | None = None,
              console_log: bool = True) -> AtlasContext:
    settings = settings or load_settings()
    secrets = secrets or load_secrets()
    setup_logging(settings.log_dir, secrets.secret_values(), console=console_log)
    log = get_logger("core")
    assert_paper_only(settings)  # raises before anything else happens

    db = Database(settings.db_path)
    applied = db.migrate()
    safe = SafeState(db)
    ok, detail = db.integrity_ok()
    if not ok:
        safe.halt(f"database integrity failure: {detail}", module="database")

    last_hash = db.get_state("config_hash")
    cfg_hash = settings.config_hash()
    if last_hash != cfg_hash:
        db.insert("config_snapshots", {"ts": utcnow(), "config_hash": cfg_hash,
                                       "config_json": json.dumps(settings.model_dump(mode="json"))})
        db.set_state("config_hash", cfg_hash)
        if last_hash is not None:
            db.log_event("config_changed", "config", f"config hash {last_hash} -> {cfg_hash}",
                         severity="warning")
    db.log_event("startup", "core", f"ATLAS {VERSION} started (paper-only)",
                 details={"migrations_applied": applied, "config_hash": cfg_hash,
                          "db": str(settings.db_path), "integrity": detail})
    log.info("ATLAS %s ready | db=%s | config=%s | integrity=%s | safe_state=%s",
             VERSION, settings.db_path.relative_to(PROJECT_ROOT) if settings.db_path.is_relative_to(PROJECT_ROOT) else settings.db_path,
             cfg_hash, detail, "HALTED" if not safe.trading_allowed() else "normal")
    return AtlasContext(settings, secrets, db, safe)
