"""SQLite access layer: connection, migrations, integrity check, journal helpers."""
from __future__ import annotations

import json
import sqlite3
import traceback as tb
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None,
                                    detect_types=0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if str(self.path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = FULL")

    # ---------- lifecycle ----------
    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def migrate(self) -> list[str]:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_ts TEXT NOT NULL)")
        applied = {r["name"] for r in self.conn.execute("SELECT name FROM schema_migrations")}
        done = []
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if f.name in applied:
                continue
            sql = f.read_text(encoding="utf-8")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(sql):
                    self.conn.execute(stmt)
                self.conn.execute("INSERT INTO schema_migrations(name, applied_ts) VALUES (?,?)",
                                  (f.name, utcnow()))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            done.append(f.name)
        return done

    def integrity_ok(self) -> tuple[bool, str]:
        try:
            r = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
            fk = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.DatabaseError as e:
            return False, f"integrity_check failed: {e}"
        if r != "ok":
            return False, f"integrity_check: {r}"
        if fk:
            return False, f"foreign_key_check: {len(fk)} violations"
        return True, "ok"

    def tables(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    # ---------- journal helpers ----------
    def insert(self, table: str, row: dict[str, Any]) -> int:
        if not table.isidentifier():
            raise ValueError("invalid table name")
        cols = list(row)
        for c in cols:
            if not c.isidentifier():
                raise ValueError(f"invalid column {c!r}")
        q = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        cur = self.conn.execute(q, [row[c] for c in cols])
        return int(cur.lastrowid)

    def log_event(self, event_type: str, module: str, message: str,
                  severity: str = "info", details: dict | None = None) -> int:
        return self.insert("system_events", {
            "ts": utcnow(), "event_type": event_type, "severity": severity, "module": module,
            "message": message, "details_json": json.dumps(details, default=str) if details else None,
        })

    def log_error(self, module: str, exc: BaseException) -> int:
        return self.insert("errors", {
            "ts": utcnow(), "module": module, "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(tb.format_exception(type(exc), exc, exc.__traceback__)),
        })

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO system_state(key, value_json, updated_ts) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_ts=excluded.updated_ts",
            (key, json.dumps(value, default=str), utcnow()))

    def get_state(self, key: str, default: Any = None) -> Any:
        r = self.conn.execute("SELECT value_json FROM system_state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default


def _split_sql(sql: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies intact."""
    stmts, buf, in_trigger = [], [], False
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper.endswith("END;"):
                stmts.append("\n".join(buf)); buf = []; in_trigger = False
        elif stripped.endswith(";"):
            stmts.append("\n".join(buf)); buf = []
    if buf and "".join(buf).strip():
        stmts.append("\n".join(buf))
    return stmts
