"""Versioned SQLite migrations for the shared control-plane database.

Migrations are deliberately package-owned and transactional. Runtime services
may ensure their tables exist for old fixtures, but production schema changes
must be recorded here so a database can be audited and upgraded deterministically.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .state import utcnow


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, declaration: str) -> None:
    name = declaration.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


def _worker_and_attempt_ownership(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS worker_instances(
      worker_id TEXT PRIMARY KEY,
      hostname TEXT NOT NULL,
      pid INTEGER NOT NULL,
      status TEXT NOT NULL,
      current_job_id TEXT,
      current_run_id TEXT,
      current_task_id TEXT,
      started_at TEXT NOT NULL,
      heartbeat_at TEXT NOT NULL,
      progress_seq INTEGER NOT NULL DEFAULT 0,
      checkpoint_hash TEXT,
      last_error TEXT
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS worker_instances_heartbeat_idx
      ON worker_instances(status, heartbeat_at)""")
    if "orchestrator_attempts" in {
        str(row["name"]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }:
        _add_column(conn, "orchestrator_attempts", "worker_id TEXT")
        _add_column(conn, "orchestrator_attempts", "lease_resource TEXT")
        _add_column(conn, "orchestrator_attempts", "fencing_token INTEGER")
        _add_column(conn, "orchestrator_attempts", "output_manifest_hash TEXT")


def _structured_failures(conn: sqlite3.Connection) -> None:
    tables = {str(row["name"]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "orchestrator_attempts" in tables:
        _add_column(conn, "orchestrator_attempts", "failure_payload TEXT")
    if "orchestrator_tasks" in tables:
        _add_column(conn, "orchestrator_tasks", "failure_payload TEXT")


def _effective_bindings_and_reuse(conn: sqlite3.Connection) -> None:
    tables = {str(row["name"]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "orchestrator_attempts" in tables:
        for declaration in (
            "input_artifact_manifest_hash TEXT", "capability_version_hash TEXT",
            "workflow_task_binding_hash TEXT", "workspace_manifest_hash TEXT",
            "policy_hash TEXT", "profile_hash TEXT", "effective_input_hash TEXT",
        ):
            _add_column(conn, "orchestrator_attempts", declaration)
        conn.execute("""CREATE INDEX IF NOT EXISTS orchestrator_attempts_effective_idx
          ON orchestrator_attempts(run_id,task_id,effective_input_hash,status)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_reuse_receipts(
      receipt_hash TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
      reused_attempt_id TEXT NOT NULL, source_attempt_id TEXT NOT NULL,
      effective_input_hash TEXT NOT NULL, output_manifest_hash TEXT NOT NULL,
      created_at TEXT NOT NULL
    )""")


def _search_rate_slots(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS search_rate_slots(
      scope TEXT NOT NULL, slot INTEGER NOT NULL, holder TEXT NOT NULL,
      expires_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY(scope,slot))""")


def _task_binding_generations(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS task_binding_generations(
      run_id TEXT NOT NULL, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
      status TEXT NOT NULL, effective_input_hash TEXT, prior_output_hash TEXT,
      reason TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(run_id,task_id,generation))""")


def _goal_alignment_control_plane(conn: sqlite3.Connection) -> None:
    """Persist the complete goal/deviation/repair/change audit chain.

    Payloads remain opaque and versioned while the small set of indexed
    columns makes the supervisor and Dashboard projections deterministic.
    """
    statements = """
    CREATE TABLE IF NOT EXISTS goal_contracts(
      goal_id TEXT PRIMARY KEY, version TEXT NOT NULL, contract_hash TEXT NOT NULL UNIQUE,
      scope TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS quality_observations(
      observation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT,
      goal_id TEXT NOT NULL, vector_hash TEXT NOT NULL, score REAL NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(job_id,run_id,vector_hash)
    );
    CREATE INDEX IF NOT EXISTS quality_observations_job_idx
      ON quality_observations(job_id,created_at);
    CREATE TABLE IF NOT EXISTS deviation_reports(
      deviation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT,
      goal_id TEXT NOT NULL, deviation_type TEXT NOT NULL, severity TEXT NOT NULL,
      fingerprint TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(job_id,fingerprint)
    );
    CREATE INDEX IF NOT EXISTS deviation_reports_status_idx
      ON deviation_reports(status,severity,created_at);
    CREATE TABLE IF NOT EXISTS causal_diagnoses(
      diagnosis_id TEXT PRIMARY KEY, deviation_id TEXT NOT NULL,
      cause_code TEXT NOT NULL, confidence REAL NOT NULL, payload TEXT NOT NULL,
      created_at TEXT NOT NULL, FOREIGN KEY(deviation_id) REFERENCES deviation_reports(deviation_id)
    );
    CREATE TABLE IF NOT EXISTS repair_plans(
      repair_plan_id TEXT PRIMARY KEY, deviation_id TEXT NOT NULL,
      repair_level TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(deviation_id) REFERENCES deviation_reports(deviation_id)
    );
    CREATE TABLE IF NOT EXISTS system_change_candidates(
      candidate_id TEXT PRIMARY KEY, source_deviation_id TEXT,
      target TEXT NOT NULL, risk TEXT NOT NULL, status TEXT NOT NULL,
      candidate_hash TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS validation_certificates(
      certificate_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
      phase TEXT NOT NULL, verdict TEXT NOT NULL, payload TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(candidate_id) REFERENCES system_change_candidates(candidate_id)
    );
    CREATE TABLE IF NOT EXISTS policy_promotion_receipts(
      receipt_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
      action TEXT NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(candidate_id) REFERENCES system_change_candidates(candidate_id)
    );
    """
    # ``executescript`` commits implicitly and would escape StateStore's
    # migration transaction.  Execute each DDL statement inside the caller's
    # transaction instead.
    for statement in statements.split(";"):
        if statement.strip():
            conn.execute(statement)


def _autonomous_job_campaigns(conn: sqlite3.Connection) -> None:
    statements = """
    CREATE TABLE IF NOT EXISTS autonomous_campaigns(
      campaign_id TEXT PRIMARY KEY, name TEXT NOT NULL, skill TEXT NOT NULL,
      status TEXT NOT NULL, spec_hash TEXT NOT NULL UNIQUE, max_concurrency INTEGER NOT NULL,
      max_auto_repairs_per_job INTEGER NOT NULL, payload TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS autonomous_job_items(
      item_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
      request_hash TEXT NOT NULL, status TEXT NOT NULL, job_id TEXT, run_id TEXT,
      repair_count INTEGER NOT NULL DEFAULT 0, last_audit_at TEXT, last_error TEXT,
      payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(campaign_id,request_hash),
      FOREIGN KEY(campaign_id) REFERENCES autonomous_campaigns(campaign_id)
    );
    CREATE INDEX IF NOT EXISTS autonomous_job_items_status_idx
      ON autonomous_job_items(campaign_id,status,ordinal);
    CREATE TABLE IF NOT EXISTS autonomous_supervisor_heartbeats(
      campaign_id TEXT PRIMARY KEY, supervisor_id TEXT NOT NULL, status TEXT NOT NULL,
      current_item_id TEXT, cycle INTEGER NOT NULL DEFAULT 0, last_error TEXT,
      started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
      FOREIGN KEY(campaign_id) REFERENCES autonomous_campaigns(campaign_id)
    );
    """
    for statement in statements.split(";"):
        if statement.strip():
            conn.execute(statement)


def _system_repair_agent_runs(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS system_repair_runs(
      repair_run_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE,
      source_job_id TEXT NOT NULL, source_run_id TEXT,
      status TEXT NOT NULL, model TEXT NOT NULL, sandbox_path TEXT,
      request_hash TEXT NOT NULL, patch_hash TEXT, payload TEXT NOT NULL,
      last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(candidate_id) REFERENCES system_change_candidates(candidate_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS system_repair_runs_status_idx
      ON system_repair_runs(status,updated_at)""")


def _failure_triage_agent_runs(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS failure_triage_runs(
      triage_run_id TEXT PRIMARY KEY, deviation_id TEXT NOT NULL UNIQUE,
      source_job_id TEXT NOT NULL, source_run_id TEXT, task_id TEXT,
      status TEXT NOT NULL, model TEXT NOT NULL, sandbox_path TEXT,
      dossier_hash TEXT NOT NULL, payload TEXT NOT NULL, last_error TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(deviation_id) REFERENCES deviation_reports(deviation_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS failure_triage_runs_status_idx
      ON failure_triage_runs(status,updated_at)""")


def _goal_supervision_wakeups_and_repair_receipts(conn: sqlite3.Connection) -> None:
    """Bridge observations to durable supervision and prove repair outcomes.

    A Worker observation must remain actionable after the observing process or
    an autonomous campaign exits.  Likewise, promotion is only deployment of a
    hypothesis; it is not proof that the source Job moved closer to its goal.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS goal_supervisor_wakeups(
      wakeup_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT,
      reason TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
      status TEXT NOT NULL, payload TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS goal_supervisor_wakeups_status_idx
      ON goal_supervisor_wakeups(status,job_id,created_at)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS repair_validation_receipts(
      receipt_id TEXT PRIMARY KEY, repair_run_id TEXT NOT NULL,
      job_id TEXT NOT NULL, run_id TEXT, verdict TEXT NOT NULL,
      baseline_hash TEXT NOT NULL, current_hash TEXT NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(repair_run_id,current_hash),
      FOREIGN KEY(repair_run_id) REFERENCES system_repair_runs(repair_run_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS repair_validation_receipts_job_idx
      ON repair_validation_receipts(job_id,created_at)""")


def _system_meta_supervision(conn: sqlite3.Connection) -> None:
    """Persist control-plane deviations outside ordinary Workflow lifecycles."""
    conn.execute("""CREATE TABLE IF NOT EXISTS system_meta_deviations(
      meta_deviation_id TEXT PRIMARY KEY, job_id TEXT, campaign_id TEXT,
      deviation_type TEXT NOT NULL, severity TEXT NOT NULL,
      fingerprint TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS system_meta_deviations_status_idx
      ON system_meta_deviations(status,severity,updated_at)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS control_plane_repair_jobs(
      meta_repair_id TEXT PRIMARY KEY, meta_deviation_id TEXT NOT NULL UNIQUE,
      source_triage_run_id TEXT, job_id TEXT, status TEXT NOT NULL,
      risk TEXT NOT NULL, action_graph_hash TEXT NOT NULL,
      payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(meta_deviation_id) REFERENCES system_meta_deviations(meta_deviation_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS control_plane_repair_jobs_status_idx
      ON control_plane_repair_jobs(status,updated_at)""")


def _task_repair_epochs(conn: sqlite3.Connection) -> None:
    """Separate monotonic audit ordinals from per-causal-repair budgets."""
    conn.execute("""CREATE TABLE IF NOT EXISTS task_repair_epochs(
      run_id TEXT NOT NULL, task_id TEXT NOT NULL, epoch INTEGER NOT NULL,
      base_attempt INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(run_id,task_id,epoch)
    )""")


def _goal_contract_governance_v2(conn: sqlite3.Connection) -> None:
    """Install immutable multi-contract governance and Goal amendment records."""
    from .governance_schema import install_governance_schema

    install_governance_schema(conn)


def _governance_reassessment_and_capability_assurance(
    conn: sqlite3.Connection,
) -> None:
    """Add durable invalidation, certification, and online drift records."""
    from .governance_schema import install_governance_schema

    install_governance_schema(conn)


def _system_repair_scm_publications(conn: sqlite3.Connection) -> None:
    """Persist repository Issue/commit/PR publication as a separate audit boundary."""
    conn.execute("""CREATE TABLE IF NOT EXISTS system_repair_scm_publications(
      publication_id TEXT PRIMARY KEY, repair_run_id TEXT NOT NULL UNIQUE,
      provider TEXT NOT NULL, status TEXT NOT NULL, repository TEXT,
      remote_name TEXT, base_branch TEXT, head_branch TEXT, commit_sha TEXT,
      issue_number INTEGER, issue_url TEXT, pr_number INTEGER, pr_url TEXT,
      payload TEXT NOT NULL, last_error TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(repair_run_id) REFERENCES system_repair_runs(repair_run_id)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS system_repair_scm_publications_status_idx
      ON system_repair_scm_publications(status,updated_at)""")


MIGRATIONS: tuple[Migration, ...] = (
    (1, "worker-and-attempt-ownership", _worker_and_attempt_ownership),
    (2, "structured-failure-payloads", _structured_failures),
    (3, "effective-bindings-and-reuse-receipts", _effective_bindings_and_reuse),
    (4, "global-search-rate-slots", _search_rate_slots),
    (5, "task-binding-generations", _task_binding_generations),
    (6, "goal-alignment-control-plane", _goal_alignment_control_plane),
    (7, "autonomous-job-campaigns", _autonomous_job_campaigns),
    (8, "system-repair-agent-runs", _system_repair_agent_runs),
    (9, "failure-triage-agent-runs", _failure_triage_agent_runs),
    (10, "goal-supervision-wakeups-and-repair-receipts",
     _goal_supervision_wakeups_and_repair_receipts),
    (11, "system-meta-supervision", _system_meta_supervision),
    (12, "task-repair-epochs", _task_repair_epochs),
    (13, "goal-contract-governance-v2", _goal_contract_governance_v2),
    (14, "governance-reassessment-and-capability-assurance",
     _governance_reassessment_and_capability_assurance),
    (15, "system-repair-scm-publications", _system_repair_scm_publications),
)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every missing migration and return the resulting schema version."""
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
      version INTEGER PRIMARY KEY,
      name TEXT UNIQUE NOT NULL,
      applied_at TEXT NOT NULL
    )""")
    applied = {int(row["version"]) for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, name, operation in MIGRATIONS:
        if version in applied:
            continue
        operation(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (version, name, utcnow()),
        )
    row = conn.execute("SELECT COALESCE(MAX(version),0) AS version FROM schema_migrations").fetchone()
    return int(row["version"])
