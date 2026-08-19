"""Production integration boundary for Goal-governed Jobs and releases.

The governance kernel deliberately remains reusable on its own.  This module
connects it to the live control plane through an explicit project config:

* ``disabled`` preserves legacy behavior;
* ``shadow`` binds and evaluates Jobs while keeping the existing execution path;
* ``enforced`` refuses unbound execution and unauthorized release tasks.

Only exact workflow mappings are accepted.  There is no permissive wildcard,
so enabling enforcement cannot silently apply an unrelated certification.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from lca_project.contracts.governance import JobContractBinding

from .events import EventLedger
from .goal_alignment.governance import GovernanceController, GovernanceError
from .governed_release import GovernanceMode, GovernedReleaseManager
from .release import ReleaseManager
from .proofs import ProofAuthority
from .state import StateStore


class GovernanceIntegrationError(RuntimeError):
    """Raised when an enforced production boundary is not governed."""


@dataclass(frozen=True)
class WorkflowGovernanceBinding:
    workflow: str
    goal_ref: str
    autonomy_ref: str
    assurance_ref: str
    capability_ref: str


class GovernanceRuntime:
    """Bind configured production Jobs and guard execution/release boundaries."""

    def __init__(self, root: str | Path, state: StateStore, events: EventLedger, artifacts) -> None:
        self.root = Path(root).resolve()
        self.state = state
        self.events = events
        self.config_path = self.root / "config" / "governance-v2.json"
        self.mode = GovernanceMode.DISABLED
        self._mappings: dict[str, WorkflowGovernanceBinding] = {}
        if self.config_path.is_file():
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != "governance-runtime-config-v1":
                raise GovernanceIntegrationError("unsupported governance runtime config")
            self.mode = GovernanceMode(str(value.get("mode", "disabled")))
        self.proof_authority = ProofAuthority(self.root, state, artifacts, events)
        self.controller = GovernanceController(
            state,
            require_job_exists=True,
            proof_authority=self.proof_authority,
            require_trusted_proofs=self.mode is GovernanceMode.ENFORCED,
        )
        if self.config_path.is_file():
            self._load_config()

    @property
    def enabled(self) -> bool:
        return self.mode is not GovernanceMode.DISABLED

    def _load_config(self) -> None:
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != "governance-runtime-config-v1":
            raise GovernanceIntegrationError("unsupported governance runtime config")
        configured_mode = GovernanceMode(str(value.get("mode", "disabled")))
        if configured_mode is not self.mode:
            raise GovernanceIntegrationError("governance mode changed during initialization")
        raw_bindings = value.get("bindings", [])
        if not isinstance(raw_bindings, list):
            raise GovernanceIntegrationError("governance bindings must be an array")
        for index, raw in enumerate(raw_bindings):
            if not isinstance(raw, dict):
                raise GovernanceIntegrationError(f"governance binding {index} must be an object")
            workflow = str(raw.get("workflow") or "").strip()
            contracts = raw.get("contracts")
            if not workflow or not isinstance(contracts, dict):
                raise GovernanceIntegrationError(f"governance binding {index} is incomplete")
            if workflow in self._mappings:
                raise GovernanceIntegrationError(f"duplicate governance workflow binding: {workflow}")
            documents: dict[str, dict[str, Any]] = {}
            for kind in ("goal", "autonomy", "assurance", "capability"):
                relative = contracts.get(kind)
                if not isinstance(relative, str) or not relative.strip():
                    raise GovernanceIntegrationError(
                        f"governance binding {workflow} has no {kind} contract"
                    )
                path = (self.root / relative).resolve()
                try:
                    path.relative_to(self.root)
                except ValueError as exc:
                    raise GovernanceIntegrationError(
                        f"governance contract escapes the project root: {relative}"
                    ) from exc
                if not path.is_file():
                    raise GovernanceIntegrationError(f"governance contract not found: {relative}")
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise GovernanceIntegrationError(f"governance contract is not an object: {relative}")
                documents[kind] = payload
            rows = {kind: self.controller.register_contract(payload)
                    for kind, payload in documents.items()}
            for kind, row in rows.items():
                if row["status"] == "draft":
                    role = "human_goal_owner" if kind == "goal" else "human_governance_owner"
                    self.controller.activate_initial_contract(
                        row["contract_ref"], actor="configured-governance-bootstrap",
                        actor_role=role,
                    )
                elif row["status"] not in {"active", "superseded"}:
                    raise GovernanceIntegrationError(
                        f"configured governance contract is {row['status']}: {row['contract_ref']}"
                    )
            self._mappings[workflow] = WorkflowGovernanceBinding(
                workflow=workflow,
                goal_ref=str(rows["goal"]["contract_ref"]),
                autonomy_ref=str(rows["autonomy"]["contract_ref"]),
                assurance_ref=str(rows["assurance"]["contract_ref"]),
                capability_ref=str(rows["capability"]["contract_ref"]),
            )

    def mapping_for(self, workflow: str) -> WorkflowGovernanceBinding | None:
        return self._mappings.get(workflow)

    def require_submission_mapping(self, workflow: str) -> None:
        if self.mode is GovernanceMode.ENFORCED and self.mapping_for(workflow) is None:
            raise GovernanceIntegrationError(
                f"enforced governance has no exact contract binding for workflow {workflow}"
            )

    def bind_job(self, job_id: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        workflow = str(payload.get("workflow") or "")
        mapping = self.mapping_for(workflow)
        if mapping is None:
            self.events.append("job", job_id, "governance.binding_missing", {
                "workflow": workflow, "mode": self.mode.value,
            }, actor="governance-runtime")
            if self.mode is GovernanceMode.ENFORCED:
                raise GovernanceIntegrationError(
                    f"Job {job_id} has no configured governance binding"
                )
            return None
        binding = self.controller.bind_job(JobContractBinding(
            job_id=job_id,
            goal_ref=mapping.goal_ref,
            autonomy_ref=mapping.autonomy_ref,
            assurance_ref=mapping.assurance_ref,
            capability_ref=mapping.capability_ref,
        ))
        self.events.append("job", job_id, "governance.job_bound", {
            "workflow": workflow,
            "binding_hash": binding["binding_hash"],
            "contract_refs": {
                "goal": mapping.goal_ref,
                "autonomy": mapping.autonomy_ref,
                "assurance": mapping.assurance_ref,
                "capability": mapping.capability_ref,
            },
        }, actor="governance-runtime")
        return binding

    def admit_execution(self, job_id: str) -> bool:
        """Require an immutable, non-revoked binding before enforced execution."""
        if not self.enabled:
            return True
        binding = self.controller.binding(job_id)
        reasons: list[str] = []
        if binding is None:
            reasons.append("immutable governance binding is missing")
        else:
            for key in ("goal_ref", "autonomy_ref", "assurance_ref", "capability_ref"):
                row = self.controller.contract(str(binding[key]))
                if row is None:
                    reasons.append(f"bound {key} is missing")
                elif row["status"] in {"suspended", "expired"}:
                    reasons.append(f"bound {key} is {row['status']}")
            if self.controller.capability_has_pending_invalidation(
                str(binding["capability_ref"])
            ):
                reasons.append("bound capability requires recertification after detected drift")
        allowed = not reasons
        self.events.append("job", job_id, "governance.execution_admission", {
            "mode": self.mode.value, "allowed": allowed, "reasons": reasons,
        }, actor="governance-runtime")
        if self.mode is GovernanceMode.ENFORCED and not allowed:
            raise GovernanceIntegrationError(
                f"Job {job_id} execution is not governed: " + "; ".join(reasons)
            )
        return allowed

    def evaluate_release_task(
        self,
        *,
        job_id: str,
        risk: str,
        runtime_fingerprint: Mapping[str, Any],
        input_scope: Mapping[str, Any],
        satisfied_requirements: tuple[str, ...] = (),
        requirement_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> bool:
        """Evaluate publication before a side-effecting release capability runs."""
        if not self.enabled:
            return True
        binding = self.controller.binding(job_id)
        if binding is None:
            self.admit_execution(job_id)
            return self.mode is not GovernanceMode.ENFORCED
        eligibility = self.controller.check_autonomy(
            job_id=job_id,
            action="publish",
            risk=risk,
            runtime_fingerprint=runtime_fingerprint,
            input_scope=input_scope,
            satisfied_requirements=satisfied_requirements,
            requirement_evidence=requirement_evidence,
        )
        self.events.append("job", job_id, "governance.release_evaluated", {
            "mode": self.mode.value,
            "decision": eligibility.decision.value,
            "eligibility_id": eligibility.eligibility_id,
            "reasons": list(eligibility.reasons),
        }, actor="governance-runtime")
        if self.mode is GovernanceMode.ENFORCED and not eligibility.authorized:
            raise GovernanceIntegrationError(
                "release is not authorized: " + "; ".join(eligibility.reasons)
            )
        return eligibility.authorized

    def wrap_release_manager(self, manager: ReleaseManager) -> ReleaseManager | GovernedReleaseManager:
        if not self.enabled:
            return manager
        return GovernedReleaseManager(manager, self.controller, mode=self.mode)


__all__ = [
    "GovernanceIntegrationError", "GovernanceRuntime", "WorkflowGovernanceBinding",
]
