"""Compile a compound Triage result into independently authorized actions."""
from __future__ import annotations

from typing import Any

from .store import digest


AUTOMATIC_AUTHORITIES = {"automatic", "automatic_analysis_and_validation"}


def compile_action_graph(triage_run_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Preserve every Triage action instead of collapsing to one scalar route."""
    raw_actions = list(result.get("actions") or [])
    graph_seed = {
        "triage_run_id": triage_run_id,
        "recovery_task": result.get("recovery_task"),
        "actions": raw_actions,
        "risk": result.get("risk"),
        "proof_contract": result.get("proof_contract") or [],
    }
    graph_id = "crg_" + digest(graph_seed)[:32]
    actions: list[dict[str, Any]] = []
    automatic_ids: list[str] = []
    for ordinal, raw in enumerate(raw_actions):
        authority = str(raw.get("authority") or "operator")
        action_id = "cra_" + digest({
            "graph_id": graph_id, "ordinal": ordinal, "action": raw,
        })[:32]
        automatic = authority in AUTOMATIC_AUTHORITIES
        action = {
            "action_id": action_id,
            "kind": str(raw.get("kind") or "request_operator"),
            "target": str(raw.get("target") or ""),
            "authority": authority,
            "risk": str(result.get("risk") or "high"),
            "status": "ready" if automatic else "awaiting_authority",
            "dependencies": ([] if automatic else list(automatic_ids)),
            "proof_contract": list(result.get("proof_contract") or []),
        }
        actions.append(action)
        if automatic:
            automatic_ids.append(action_id)
    return {
        "schema_version": "control-repair-action-graph-v1",
        "graph_id": graph_id,
        "source_triage_run_id": triage_run_id,
        "recovery_task": str(result.get("recovery_task") or ""),
        "actions": actions,
    }


def runnable_automatic_actions(graph: dict[str, Any]) -> list[dict[str, Any]]:
    completed = {
        str(item["action_id"]) for item in graph.get("actions") or []
        if item.get("status") == "completed"
    }
    return [
        item for item in graph.get("actions") or []
        if item.get("status") == "ready"
        and item.get("authority") in AUTOMATIC_AUTHORITIES
        and set(item.get("dependencies") or []) <= completed
    ]
