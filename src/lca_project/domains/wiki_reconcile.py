"""Reconcile frozen Wiki artifacts into persistent workflow task state."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from lca_project.kernel.orchestrator import PersistentOrchestrator


class WikiReconcileError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


ARTIFACTS: dict[str, tuple[str, ...]] = {
    "plan": ("manifest.json",),
    "prepare": ("prepared.json", "validation.json"),
    "research_ready": ("research-ready.json", "nomination-runtime-v2/nomination-invocation.json",
                       "nomination-runtime-v2/nomination-events.jsonl", "nomination-runtime-v2/wiki-usage-v1.json"),
    "verify": ("verified.json", "verify-output.json"),
    "freeze": ("frozen.json",),
    "content_blueprint": ("content-blueprint.json",),
    "content_compose": ("content-enriched.json",),
    "editorial_review": ("editorial-loop/editorial-review.json",),
    "draft_content_gate": ("draft-content-gate.json",),
    "draft_apply": ("content-apply-report.json",),
    "table_collect": ("table-data/collection.json",),
    "table_verify": ("table-data/source-verdict.json",),
    "table_population_gate": ("table-data/table-population-gate.json",),
    "table_apply": ("table-data/table-apply-report.json",),
    "preview": ("preview-report.json",),
    "release_gate": ("quality-gate.json", "gate-report.json"),
    "reviewed_apply": ("reviewed-apply-report.json",),
    "publish": ("publish-report.json",),
}


def _paths_for(task_id: str, batch: Path) -> tuple[Path, ...]:
    paths = tuple(batch / item for item in ARTIFACTS[task_id])
    if task_id != "verify" or not (batch / "verified.json").is_file():
        return paths
    verified = json.loads((batch / "verified.json").read_text(encoding="utf-8"))
    attestations = verified.get("runtime_attestations") or []
    if len(attestations) != 1:
        raise WikiReconcileError("verify requires exactly one runtime attestation for a single-node job")
    runtime = attestations[0]
    declared = []
    for key in ("invocation", "events", "stderr", "usage", "verdicts"):
        value = runtime.get(key) or {}
        path = Path(str(value.get("path", ""))).resolve()
        try:
            path.relative_to(batch)
        except ValueError as exc:
            raise WikiReconcileError(f"verify runtime path escapes batch: {key}") from exc
        if value.get("sha256") != _digest(path):
            raise WikiReconcileError(f"verify runtime hash drift: {key}")
        declared.append(path)
    return (*paths, *declared)


def reconcile_wiki_run(root: str | Path, run_id: str, batch_dir: str | Path) -> dict[str, Any]:
    """Admit only present, non-empty, hash-bound artifacts in dependency order."""
    root, batch = Path(root).resolve(), Path(batch_dir).resolve()
    try:
        batch.relative_to(root)
    except ValueError as exc:
        raise WikiReconcileError("batch directory must be inside the project root") from exc
    journal_path = batch / "journal.json"
    if not journal_path.is_file():
        raise WikiReconcileError("journal.json is missing")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if not isinstance(journal.get("state"), str):
        raise WikiReconcileError("invalid Wiki journal")
    orchestrator = PersistentOrchestrator(root)
    admitted: list[dict[str, Any]] = []
    while True:
        ready = orchestrator.ready(run_id)
        progressed = False
        for task in ready:
            relatives = ARTIFACTS.get(task.task_id)
            if not relatives:
                continue
            paths = _paths_for(task.task_id, batch)
            if not all(path.is_file() and path.stat().st_size > 0 for path in paths):
                continue
            if task.task_id in {"draft_content_gate", "table_population_gate", "release_gate"}:
                documents = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
                words = {str(doc.get(key, "")).upper() for doc in documents
                         for key in ("decision", "status", "result")}
                if not words.intersection({"GO", "PASS", "PASSED", "OK"}):
                    continue
            attempt_id, inputs = orchestrator.claim(run_id, task.task_id)
            receipt = {
                "protocol": "wiki-artifact-admission-v1", "task_id": task.task_id,
                "journal_state": journal["state"], "dependency_hashes": list(inputs),
                "artifacts": [{"path": str(path.relative_to(root)), "sha256": _digest(path),
                               "bytes": path.stat().st_size} for path in paths],
            }
            output_hash = orchestrator.complete(attempt_id, receipt)
            admitted.append({"task_id": task.task_id, "output_hash": output_hash})
            progressed = True
        if not progressed:
            break
    return {"run_id": run_id, "journal_state": journal["state"], "admitted": admitted,
            "tasks": [{"task_id": item.task_id, "status": item.status,
                       "output_hash": item.output_hash} for item in orchestrator.tasks(run_id)]}
