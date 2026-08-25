"""Stable mechanism families for execution and self-repair failures.

Raw exception text and Agent cause codes remain valuable evidence, but they are
too high-cardinality to drive recurrence analysis or repair policy.  This
module adds a deterministic, versioned family without discarding the raw facts.
"""
from __future__ import annotations

from typing import Any


TAXONOMY_VERSION = "failure-mechanism-taxonomy-v1"


def classify_failure(*, task_id: str, failure_code: str,
                     payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    text = " ".join((
        str(task_id or ""), str(failure_code or ""),
        str(payload.get("category") or ""), str(payload.get("scope") or ""),
        str(payload.get("message") or ""),
    )).lower()
    rules = (
        ("research_contract", ("research_plan", "translation", "query_builder")),
        ("evidence_fetch", ("payload_not_fetched", "fetch_failed", "http", "download")),
        ("evidence_extraction", ("extraction", "extractor", "document_route", "pdf")),
        ("table_contract", ("table_collect", "table_population", "table_verify", "table contract")),
        ("content_semantics", ("content_compose", "content_closure", "semantic", "near-duplicate")),
        ("editorial_patch", ("editorial", "preservation token", "duplication patch")),
        ("terminal_semantics", ("maturity", "candidate_eligible", "false_pass", "terminal")),
        ("repair_governance", ("system_repair", "causal_input", "proof_contract", "canary")),
        ("runtime_protocol", ("capability_process", "worker_runtime", "schema", "protocol")),
        ("infrastructure", ("temporary_io", "lease", "timeout", "network")),
    )
    for family, tokens in rules:
        if any(token in text for token in tokens):
            return family
    return "unknown"


def taxonomy_record(*, task_id: str, failure_code: str,
                    payload: dict[str, Any] | None = None) -> dict[str, str]:
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "mechanism_family": classify_failure(
            task_id=task_id, failure_code=failure_code, payload=payload,
        ),
    }
