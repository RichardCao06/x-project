"""SQLite schema owned by Goal Contract governance v2."""
from __future__ import annotations

import sqlite3


GOVERNANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS governance_contracts(
  contract_ref TEXT PRIMARY KEY,
  contract_kind TEXT NOT NULL,
  contract_id TEXT NOT NULL,
  version TEXT NOT NULL,
  contract_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  activated_at TEXT,
  superseded_by TEXT,
  UNIQUE(contract_kind,contract_id,version)
);
CREATE INDEX IF NOT EXISTS governance_contracts_status_idx
  ON governance_contracts(contract_kind,status,contract_id,version);
CREATE TABLE IF NOT EXISTS contract_lifecycle_events(
  event_id TEXT PRIMARY KEY,
  contract_ref TEXT NOT NULL,
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  actor TEXT NOT NULL,
  actor_role TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(contract_ref) REFERENCES governance_contracts(contract_ref)
);
CREATE INDEX IF NOT EXISTS contract_lifecycle_events_ref_idx
  ON contract_lifecycle_events(contract_ref,created_at);
CREATE TABLE IF NOT EXISTS goal_change_proposals(
  proposal_id TEXT PRIMARY KEY,
  from_ref TEXT NOT NULL,
  to_ref TEXT NOT NULL,
  change_class TEXT NOT NULL,
  risk TEXT NOT NULL,
  status TEXT NOT NULL,
  proposal_hash TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(from_ref) REFERENCES governance_contracts(contract_ref),
  FOREIGN KEY(to_ref) REFERENCES governance_contracts(contract_ref)
);
CREATE TABLE IF NOT EXISTS governance_approvals(
  approval_id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  actor_role TEXT NOT NULL,
  decision TEXT NOT NULL,
  rationale TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(proposal_id,actor,decision),
  FOREIGN KEY(proposal_id) REFERENCES goal_change_proposals(proposal_id)
);
CREATE TABLE IF NOT EXISTS job_contract_bindings(
  job_id TEXT PRIMARY KEY,
  binding_hash TEXT NOT NULL UNIQUE,
  goal_ref TEXT NOT NULL,
  autonomy_ref TEXT NOT NULL,
  assurance_ref TEXT NOT NULL,
  capability_ref TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(goal_ref) REFERENCES governance_contracts(contract_ref),
  FOREIGN KEY(autonomy_ref) REFERENCES governance_contracts(contract_ref),
  FOREIGN KEY(assurance_ref) REFERENCES governance_contracts(contract_ref),
  FOREIGN KEY(capability_ref) REFERENCES governance_contracts(contract_ref)
);
CREATE TABLE IF NOT EXISTS alignment_assessments(
  assessment_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  binding_hash TEXT NOT NULL,
  verdict TEXT NOT NULL,
  terminal_state TEXT NOT NULL,
  assessment_hash TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(job_id) REFERENCES job_contract_bindings(job_id)
);
CREATE INDEX IF NOT EXISTS alignment_assessments_job_idx
  ON alignment_assessments(job_id,created_at);
CREATE TABLE IF NOT EXISTS autonomy_eligibility_assessments(
  eligibility_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  binding_hash TEXT NOT NULL,
  action TEXT NOT NULL,
  decision TEXT NOT NULL,
  eligibility_hash TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(job_id) REFERENCES job_contract_bindings(job_id)
);
CREATE INDEX IF NOT EXISTS autonomy_eligibility_job_idx
  ON autonomy_eligibility_assessments(job_id,action,created_at);
"""


def install_governance_schema(conn: sqlite3.Connection) -> None:
    """Install all governance tables inside the caller's transaction."""
    for statement in GOVERNANCE_SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
