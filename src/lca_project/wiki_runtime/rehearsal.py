"""Offline Phase-2 rehearsal using only frozen vendored Wiki fixtures."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from pathlib import Path
from typing import Any
import uuid

from lca_project.domains.wiki import AdapterResult, WikiAdapter
from lca_project.domains.wiki_workspace import WikiWorkspaceBuilder
from .runtime import WikiRuntime, WikiStage


class WikiRehearsalError(RuntimeError):
    pass


@dataclass(frozen=True)
class CohortResult:
    industry: str
    nodes: tuple[str, ...]
    manifest: str
    prepared: str
    validation_verdict: str
    run_ids: tuple[str, ...]


def _require_ok(result: AdapterResult, label: str) -> None:
    if not result.ok:
        raise WikiRehearsalError(f"{label} failed ({result.returncode}): {result.stderr[-1000:]}")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WikiRehearsalError(f"expected object: {path}")
    return value


def _stable(value: Any, workspace: Path) -> Any:
    """Remove run-time paths/timestamps before CAS and idempotency checks."""
    if isinstance(value, dict):
        return {key: ("<normalized>" if key in {"created_at", "updated_at"} else _stable(item, workspace))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_stable(item, workspace) for item in value]
    if isinstance(value, str):
        return value.replace(str(workspace), "$WORKSPACE")
    return value


class WikiPhase2Rehearsal:
    """Run plan/prepare/validate and freeze each node into the Kernel.

    This deliberately stops before research-ready: the fixture does not carry
    a new nomination invocation or fresh external evidence, so proceeding to
    reviewed/publish would manufacture authorization.
    """

    COHORTS = (("oil_refining", ("A017",), "golden"),
               ("ict_equipment", ("P031", "P003"), "history"))

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.runtime = WikiRuntime(self.project_root)

    def run(self, *, workspace: str | Path | None = None) -> dict[str, Any]:
        root = Path(workspace).resolve() if workspace else (
            self.project_root / "var" / "workspaces" / f"wiki-phase2-{uuid.uuid4().hex}"
        )
        built = WikiWorkspaceBuilder().build(root)
        WikiWorkspaceBuilder().verify(built.root)
        adapter = WikiAdapter()
        cohorts: list[CohortResult] = []
        for industry, node_ids, label in self.COHORTS:
            batch = built.root / "runs" / "wiki-batches" / industry / f"phase2-{label}"
            planned = adapter.plan(industry, list(node_ids), workspace=built.root, output=batch, dry_run=False,
                                   batch_id=f"phase2-{label}")
            _require_ok(planned, f"{industry} plan")
            manifest_path = batch / "manifest.json"
            manifest = _read(manifest_path)
            prepared_result = adapter.command("prepare", manifest_path, workspace=built.root, dry_run=False)
            _require_ok(prepared_result, f"{industry} prepare")
            prepared_path = batch / "prepared.json"
            prepared = _read(prepared_path)
            validated = adapter.command("validate", prepared_path, workspace=built.root, dry_run=False)
            _require_ok(validated, f"{industry} validate")
            validation = json.loads(validated.stdout)
            if validation.get("verdict") != "PASS":
                raise WikiRehearsalError(f"{industry} validation did not PASS")
            node_rows = {str(item.get("node_id")): item for item in manifest.get("nodes", [])}
            stable_manifest = _stable(manifest, built.root)
            stable_prepared = _stable(prepared, built.root)
            stable_validation = _stable(validation, built.root)
            run_ids: list[str] = []
            for node_id in node_ids:
                node_ref = f"{industry}::{node_id}"
                dossier = {"node_identity": {"node_id": node_ref}, "manifest_node": node_rows.get(node_id, {}),
                           "fixture_only": True, "source_checkout_access": False}
                run = self.runtime.start(node_id=node_ref, dossier=dossier, policy_version="wiki-production-v3",
                                         idempotency_key=f"phase2-rehearsal-v3:{node_ref}",
                                         batch_id=f"phase2-{label}", work_kind="repair" if node_id != "P003" else "nomination")
                run, plan_hashes = self.runtime.advance(run.run_id, WikiStage.PLAN, {
                    "node_identity": {"node_id": node_ref}, "outputs": [{"manifest": stable_manifest,
                    "workspace_manifest": _stable(_read(built.manifest), built.root)}]
                })
                run, _ = self.runtime.advance(run.run_id, WikiStage.PREPARED, {
                    "node_identity": {"node_id": node_ref}, "input_hashes": list(plan_hashes),
                    "outputs": [{"prepared": stable_prepared, "validation": stable_validation}]
                })
                run_ids.append(run.run_id)
            cohorts.append(CohortResult(industry, node_ids, str(manifest_path), str(prepared_path), "PASS", tuple(run_ids)))
        report = {"protocol": "wiki-phase2-rehearsal-v1", "workspace": str(built.root),
                  "workspace_files": built.files, "source_checkout_access": False,
                  "stopped_at": "prepared", "publish_authorized": False,
                  "reason": "fresh nomination/evidence/verify attestation is intentionally absent",
                  "cohorts": [asdict(item) for item in cohorts]}
        # The workspace is an execution detail, not part of the semantic
        # evidence.  Normalize it before CAS so an exact retry deduplicates to
        # the same artifact instead of manufacturing a new report hash.
        report = _stable(report, built.root)
        artifact = self.runtime.artifacts.put_json(report, metadata={"schema": "wiki-phase2-rehearsal-v1"})
        self.runtime.events.append("wiki_rehearsal", artifact.digest, "wiki.rehearsal.completed",
                                   {"report_hash": artifact.digest, "publish_authorized": False},
                                   actor="wiki-phase2-rehearsal", event_id=f"wiki-rehearsal:{artifact.digest}")
        return {**report, "report_hash": artifact.digest}
