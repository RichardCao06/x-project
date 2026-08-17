"""SQLite-backed control-plane state.

The store intentionally keeps opaque JSON payloads alongside stable indexed
columns.  This lets workflow/domain schemas evolve without ad-hoc migrations
while retaining transactional auditability.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class StateStore:
    """Small transactional repository for all kernel entities."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 30000")
            self._local.connection = connection
        return connection

    def initialize(self) -> None:
        # sqlite3.executescript manages its own transaction boundaries, so it
        # must not be nested in ``transaction()``.
        self._connection().executescript(
            """
                CREATE TABLE IF NOT EXISTS programs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS industries (
                    id TEXT PRIMARY KEY, program_id TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(program_id) REFERENCES programs(id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, program_id TEXT, industry_id TEXT, workflow_id TEXT,
                    status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL, payload TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT, FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    digest TEXT PRIMARY KEY, media_type TEXT NOT NULL, size INTEGER NOT NULL,
                    uri TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_edges (
                    parent_digest TEXT NOT NULL, child_digest TEXT NOT NULL, relation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(parent_digest, child_digest, relation),
                    FOREIGN KEY(parent_digest) REFERENCES artifacts(digest),
                    FOREIGN KEY(child_digest) REFERENCES artifacts(digest)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL,
                    payload TEXT NOT NULL, trace_id TEXT, actor TEXT, occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_aggregate_idx ON events(aggregate_type, aggregate_id, sequence);
                CREATE TABLE IF NOT EXISTS gate_results (
                    id TEXT PRIMARY KEY, run_id TEXT, gate_name TEXT NOT NULL, verdict TEXT NOT NULL,
                    evidence_digest TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY, run_id TEXT, decision_type TEXT NOT NULL, verdict TEXT NOT NULL,
                    rationale TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exceptions (
                    id TEXT PRIMARY KEY, run_id TEXT, error_code TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, opened_at TEXT NOT NULL, resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS leases (
                    resource TEXT PRIMARY KEY, holder TEXT NOT NULL, fencing_token INTEGER NOT NULL,
                    expires_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budgets (
                    scope TEXT PRIMARY KEY, limit_units INTEGER NOT NULL, reserved_units INTEGER NOT NULL DEFAULT 0,
                    consumed_units INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, units INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, settled_at TEXT,
                    FOREIGN KEY(scope) REFERENCES budgets(scope)
                );
                CREATE TABLE IF NOT EXISTS releases (
                    id TEXT PRIMARY KEY, program_id TEXT, status TEXT NOT NULL, manifest_digest TEXT,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL, applied_at TEXT
                );
            """
        )
        # Import lazily to avoid a module cycle while keeping the database
        # upgrade attached to the single StateStore construction boundary.
        from .migrations import migrate
        with self.transaction() as conn:
            migrate(conn)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    @staticmethod
    def _encode(payload: dict[str, Any] | None) -> str:
        return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("payload", "metadata"):
            if key in result:
                result[key] = json.loads(result[key])
        return result

    def upsert_entity(self, table: str, entity_id: str, status: str, payload: dict[str, Any], **links: str | None) -> None:
        allowed = {"programs": (), "industries": ("program_id",), "jobs": ("program_id", "industry_id", "workflow_id")}
        if table not in allowed or any(key not in allowed[table] for key in links):
            raise ValueError(f"unsupported entity table or links: {table}")
        now = utcnow()
        columns = ["id", *allowed[table], "status", "payload", "created_at", "updated_at"]
        values = [entity_id, *(links.get(key) for key in allowed[table]), status, self._encode(payload), now, now]
        updates = [f"{name}=excluded.{name}" for name in [*allowed[table], "status", "payload", "updated_at"]]
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(id) DO UPDATE SET {','.join(updates)}"
        with self.transaction() as conn:
            conn.execute(sql, values)

    def create_run(self, run_id: str, job_id: str, payload: dict[str, Any], attempt: int = 1, status: str = "running") -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO runs(id,job_id,attempt,status,payload,started_at) VALUES(?,?,?,?,?,?)", (run_id, job_id, attempt, status, self._encode(payload), utcnow()))

    def finish_run(self, run_id: str, status: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE runs SET status=?, finished_at=? WHERE id=?", (status, utcnow(), run_id))

    def get(self, table: str, entity_id: str) -> dict[str, Any] | None:
        if table not in {"programs", "industries", "jobs", "runs", "artifacts", "gate_results", "decisions", "exceptions", "releases"}:
            raise ValueError(f"unsupported table: {table}")
        return self._decode(self._connection().execute(f"SELECT * FROM {table} WHERE id=?" if table != "artifacts" else "SELECT * FROM artifacts WHERE digest=?", (entity_id,)).fetchone())

    def record_gate(self, gate_id: str, verdict: str, gate_name: str, payload: dict[str, Any], run_id: str | None = None, evidence_digest: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO gate_results VALUES(?,?,?,?,?,?,?)", (gate_id, run_id, gate_name, verdict, evidence_digest, self._encode(payload), utcnow()))

    def record_decision(self, decision_id: str, decision_type: str, verdict: str, payload: dict[str, Any], run_id: str | None = None, rationale: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)", (decision_id, run_id, decision_type, verdict, rationale, self._encode(payload), utcnow()))

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None
