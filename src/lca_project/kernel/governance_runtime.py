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
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from lca_project.contracts.governance import (
    ContractKind, ContractRef, JobContractBinding, payload_digest,
)

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
        configured = self._mappings.get(workflow)
        if configured is None:
            return None
        return WorkflowGovernanceBinding(
            workflow=workflow,
            goal_ref=self._resolve_active_ref(configured.goal_ref),
            autonomy_ref=self._resolve_active_ref(configured.autonomy_ref),
            assurance_ref=self._resolve_active_ref(configured.assurance_ref),
            capability_ref=self._resolve_active_ref(configured.capability_ref),
        )

    def _resolve_active_ref(self, configured_ref: str) -> str:
        """Follow only an authorized same-contract supersession chain."""
        configured = ContractRef.parse(configured_ref)
        current = configured_ref
        seen: set[str] = set()
        while True:
            if current in seen:
                raise GovernanceIntegrationError(
                    f"governance contract supersession cycle: {configured_ref}"
                )
            seen.add(current)
            row = self.controller.contract(current)
            if row is None:
                raise GovernanceIntegrationError(f"governance contract is missing: {current}")
            if row["status"] != "superseded":
                return current
            replacement = str(row.get("superseded_by") or "").strip()
            if not replacement:
                raise GovernanceIntegrationError(
                    f"superseded governance contract has no replacement: {current}"
                )
            parsed = ContractRef.parse(replacement)
            if parsed.kind is not configured.kind or parsed.contract_id != configured.contract_id:
                raise GovernanceIntegrationError(
                    f"governance contract replacement changed identity: {current} -> {replacement}"
                )
            current = replacement

    @staticmethod
    def _hash_inventory(entries: Mapping[str, str]) -> str:
        return payload_digest(dict(sorted(entries.items())))

    def _runtime_fingerprint(self, workflow: str) -> dict[str, str]:
        """Derive the running combination from trusted, versioned repository assets."""
        workflow_path = self.root / "workflows" / f"{workflow}.json"
        if not workflow_path.is_file():
            raise GovernanceIntegrationError(f"workflow definition is missing: {workflow}")
        workflow_document = json.loads(workflow_path.read_text(encoding="utf-8"))
        if not isinstance(workflow_document, dict):
            raise GovernanceIntegrationError(f"workflow definition is invalid: {workflow}")

        route_matches: list[tuple[Path, dict[str, Any]]] = []
        for route_path in sorted((self.root / "skills").glob("*/skill.manifest.json")):
            route = json.loads(route_path.read_text(encoding="utf-8"))
            if isinstance(route, dict) and route.get("workflow") == f"workflow://{workflow}":
                route_matches.append((route_path, route))
        if len(route_matches) != 1:
            raise GovernanceIntegrationError(
                f"workflow {workflow} must have exactly one versioned Skill route"
            )
        route_path, route = route_matches[0]
        policy_name = str(route.get("policy") or "")
        policy_path = self.root / "policies" / f"{policy_name}.json"
        if not policy_path.is_file():
            raise GovernanceIntegrationError(
                f"production policy is missing for workflow {workflow}: {policy_name}"
            )

        agent_definitions: dict[str, str] = {}
        prompt_files: dict[str, str] = {
            str(route_path.relative_to(self.root)): hashlib.sha256(
                route_path.read_bytes()
            ).hexdigest(),
            str(policy_path.relative_to(self.root)): hashlib.sha256(
                policy_path.read_bytes()
            ).hexdigest(),
        }
        for definition_path in sorted((self.root / "agents").glob("*/agent.json")):
            definition = json.loads(definition_path.read_text(encoding="utf-8"))
            relative = str(definition_path.relative_to(self.root))
            agent_definitions[relative] = hashlib.sha256(definition_path.read_bytes()).hexdigest()
            prompt_name = (
                str(definition.get("prompt") or "")
                if isinstance(definition, dict) else ""
            )
            prompt_path = definition_path.parent / prompt_name
            if not prompt_name or not prompt_path.is_file():
                raise GovernanceIntegrationError(f"Agent prompt is missing: {relative}")
            prompt_files[str(prompt_path.relative_to(self.root))] = hashlib.sha256(
                prompt_path.read_bytes()
            ).hexdigest()

        capability_ids = {
            str(item.get("capability") or "")
            for item in workflow_document.get("steps", [])
            if isinstance(item, dict) and item.get("capability")
        }
        tool_files = {
            str(workflow_path.relative_to(self.root)): hashlib.sha256(
                workflow_path.read_bytes()
            ).hexdigest()
        }
        for capability_id in sorted(capability_ids):
            manifests = sorted((self.root / "capabilities").glob(f"{capability_id}@*.json"))
            if len(manifests) != 1:
                raise GovernanceIntegrationError(
                    f"workflow capability {capability_id} must resolve to exactly one manifest"
                )
            manifest = manifests[0]
            tool_files[str(manifest.relative_to(self.root))] = hashlib.sha256(
                manifest.read_bytes()
            ).hexdigest()
        return {
            "model": "agent-set@sha256:" + self._hash_inventory(agent_definitions),
            "prompt": "prompt-set@sha256:" + self._hash_inventory(prompt_files),
            "toolset": "capability-set@sha256:" + self._hash_inventory(tool_files),
            "workflow": workflow,
        }

    def _release_context(
        self, *, payload: Mapping[str, Any], binding: Mapping[str, Any]
    ) -> dict[str, Any]:
        workflow = str(payload.get("workflow") or "")
        capability = self.controller.contract(str(binding["capability_ref"]))
        if capability is None:
            raise GovernanceIntegrationError("bound Capability Envelope is missing")
        request = ((payload.get("scope") or {}).get("request") or {})
        if not isinstance(request, Mapping):
            raise GovernanceIntegrationError("governed Job has no structured Skill request")
        allowed_scope = capability["payload"].get("input_scope") or {}
        if not isinstance(allowed_scope, Mapping):
            raise GovernanceIntegrationError("Capability Envelope input_scope is invalid")
        input_scope = {key: request[key] for key in allowed_scope if key in request}
        body = {
            "schema_version": "governed-release-context-v1",
            "binding_hash": str(binding["binding_hash"]),
            "runtime_fingerprint": self._runtime_fingerprint(workflow),
            "input_scope": input_scope,
        }
        return {**body, "context_hash": payload_digest(body)}

    def release_context(self, job_id: str) -> dict[str, Any]:
        """Return the controller-owned release inputs, rejecting persisted drift."""
        job = self.state.get("jobs", job_id)
        binding = self.controller.binding(job_id)
        if job is None or binding is None:
            raise GovernanceIntegrationError(f"Job has no governed release context: {job_id}")
        expected = self._release_context(payload=job["payload"], binding=binding)
        stored = ((job["payload"].get("governance") or {}).get("release_context"))
        if stored != expected:
            raise GovernanceIntegrationError(
                f"Job {job_id} frozen release context is missing or has drifted"
            )
        return expected

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
        requested_binding = JobContractBinding(
            job_id=job_id,
            goal_ref=mapping.goal_ref,
            autonomy_ref=mapping.autonomy_ref,
            assurance_ref=mapping.assurance_ref,
            capability_ref=mapping.capability_ref,
        )
        release_context = self._release_context(
            payload=payload, binding=requested_binding.asdict()
        )
        binding = self.controller.bind_job(requested_binding)
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
        return {**binding, "release_context": release_context}

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
        runtime_fingerprint: Mapping[str, Any] | None = None,
        input_scope: Mapping[str, Any] | None = None,
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
        if runtime_fingerprint is None or input_scope is None:
            if runtime_fingerprint is not None or input_scope is not None:
                raise GovernanceIntegrationError(
                    "release runtime fingerprint and input scope must be supplied together"
                )
            context = self.release_context(job_id)
            runtime_fingerprint = context["runtime_fingerprint"]
            input_scope = context["input_scope"]
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

    @staticmethod
    def _certification_findings(capability: Mapping[str, Any]) -> list[str]:
        certification = capability.get("certification") or {}
        findings: list[str] = []
        if certification.get("status") != "certified":
            findings.append(
                "Capability Envelope is not certified: "
                + str(certification.get("status") or "missing")
            )
        valid_until = certification.get("valid_until")
        if valid_until:
            try:
                expiry = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    findings.append("Capability Envelope certification has expired")
            except ValueError:
                findings.append("Capability Envelope certification expiry is invalid")
        return findings

    def _mapping_findings(self, mapping: WorkflowGovernanceBinding) -> list[str]:
        refs = {
            "goal": mapping.goal_ref,
            "autonomy": mapping.autonomy_ref,
            "assurance": mapping.assurance_ref,
            "capability": mapping.capability_ref,
        }
        rows: dict[str, dict[str, Any]] = {}
        findings: list[str] = []
        expected_kinds = {
            "goal": ContractKind.GOAL,
            "autonomy": ContractKind.AUTONOMY,
            "assurance": ContractKind.ASSURANCE,
            "capability": ContractKind.CAPABILITY,
        }
        for kind, ref in refs.items():
            row = self.controller.contract(ref)
            if row is None:
                findings.append(f"{kind} contract is missing: {ref}")
                continue
            rows[kind] = row
            if row["status"] != "active":
                findings.append(f"{kind} contract is {row['status']}: {ref}")
            if ContractRef.parse(ref).kind is not expected_kinds[kind]:
                findings.append(f"configured {kind} contract has the wrong kind: {ref}")
        if len(rows) != 4:
            return findings
        goal = rows["goal"]["payload"]
        autonomy = rows["autonomy"]["payload"]
        assurance = rows["assurance"]["payload"]
        capability = rows["capability"]["payload"]
        if assurance.get("goal_contract_ref") != mapping.goal_ref:
            findings.append("Assurance Contract does not match the active Goal Contract")
        autonomy_goal = (autonomy.get("scope") or {}).get("goal_contract_ref")
        if autonomy_goal and autonomy_goal != mapping.goal_ref:
            findings.append("Autonomy Contract does not match the active Goal Contract")
        required_clauses = {
            str(item["id"]) for item in goal.get("clauses", [])
            if isinstance(item, dict) and item.get("criticality") in {"hard", "required"}
        }
        missing = sorted(required_clauses - set(assurance.get("proof_obligations") or {}))
        if missing:
            findings.append("Assurance Contract misses required clauses: " + ", ".join(missing))
        domains = {
            str(item.get("scope", {}).get("domain"))
            for item in (goal, autonomy, assurance, capability)
            if item.get("scope", {}).get("domain") is not None
        }
        if len(domains) > 1:
            findings.append("configured contracts have incompatible scope domains")
        findings.extend(self._certification_findings(capability))
        observed_risk = float(
            (capability.get("certification") or {}).get(
                "selective_risk_upper_bound", 1.0
            )
        )
        risk_ceiling = float((goal.get("evaluation") or {}).get("selective_risk_ceiling", 0.0))
        if observed_risk > risk_ceiling:
            findings.append(
                f"Capability selective risk {observed_risk:.6f} exceeds "
                f"Goal ceiling {risk_ceiling:.6f}"
            )
        if self.controller.capability_has_pending_invalidation(mapping.capability_ref):
            findings.append("Capability Envelope has a pending drift invalidation")
        try:
            actual = self._runtime_fingerprint(mapping.workflow)
            if capability.get("runtime_fingerprint") != actual:
                findings.append("Capability Envelope runtime fingerprint is stale")
        except (GovernanceIntegrationError, OSError, ValueError, json.JSONDecodeError) as exc:
            findings.append(str(exc))
        return findings

    def readiness(self) -> dict[str, Any]:
        """Report whether every configured workflow can bind and enforce now."""
        reports: list[dict[str, Any]] = []
        for workflow in sorted(self._mappings):
            try:
                mapping = self.mapping_for(workflow)
                assert mapping is not None
                findings = self._mapping_findings(mapping)
                reports.append({
                    "workflow": workflow,
                    "ready": not findings,
                    "contract_refs": {
                        "goal": mapping.goal_ref,
                        "autonomy": mapping.autonomy_ref,
                        "assurance": mapping.assurance_ref,
                        "capability": mapping.capability_ref,
                    },
                    "findings": findings,
                })
            except (GovernanceIntegrationError, GovernanceError, ValueError) as exc:
                reports.append({"workflow": workflow, "ready": False, "findings": [str(exc)]})
        pending = int(self.state._connection().execute(
            "SELECT COUNT(*) FROM governance_reassessments WHERE status='pending'"
        ).fetchone()[0])
        checks = {
            "runtime_enabled": self.enabled,
            "configured_workflows_available": bool(reports),
            "configured_workflows_ready": bool(reports) and all(
                report["ready"] for report in reports
            ),
            "no_pending_reassessments": pending == 0,
        }
        return {
            "schema_version": "governance-runtime-readiness-v2",
            "mode": self.mode.value,
            "ready": all(checks.values()),
            "checks": checks,
            "workflows": reports,
            "pending_reassessments": pending,
        }

    @staticmethod
    def _require_release_report(
        path: Path,
        *,
        kind: str,
        predicate: Callable[[Mapping[str, Any]], bool],
    ) -> dict[str, Any]:
        if not path.is_file():
            raise GovernanceIntegrationError(f"release proof is missing: {path.name}")
        report = json.loads(path.read_text(encoding="utf-8"))
        protocol = report.get("protocol") if isinstance(report, dict) else None
        if (
            not isinstance(report, dict)
            or not isinstance(protocol, dict)
            or protocol.get("kind") != kind
            or not predicate(report)
        ):
            raise GovernanceIntegrationError(f"release proof is not eligible: {path.name}")
        return report

    def publish_wiki_release(
        self,
        *,
        job_id: str,
        workspace: str | Path,
        batch: str | Path,
        industry: str,
        node: str,
        risk: str = "low",
    ) -> dict[str, Any]:
        """Apply one reviewed Wiki snapshot through the governed release service."""
        workspace_path = Path(workspace).resolve()
        batch_path = Path(batch).resolve()
        expected_workspace = self.root / "var" / "workspaces" / "jobs" / job_id
        try:
            workspace_path.relative_to(expected_workspace.resolve())
            batch_path.relative_to(workspace_path)
        except ValueError as exc:
            raise GovernanceIntegrationError("Wiki release workspace is outside the Job boundary") from exc
        if re.fullmatch(r"[a-z][a-z0-9_]*", industry) is None:
            raise GovernanceIntegrationError(f"invalid Wiki release industry: {industry}")
        if not node.startswith(("P", "A")) or not node[1:].isdigit():
            raise GovernanceIntegrationError(f"invalid Wiki release node: {node}")
        job = self.state.get("jobs", job_id)
        request = (
            (((job or {}).get("payload") or {}).get("scope") or {}).get("request")
            or {}
        )
        if not isinstance(request, Mapping) or request.get("nodes") != [node]:
            raise GovernanceIntegrationError("Wiki release node does not match the frozen Job")

        gate_path = batch_path / "gate-report.json"
        gate = self._require_release_report(
            gate_path,
            kind="gate-report",
            predicate=lambda report: (
                report.get("all_passed") is True
                and (report.get("go_no_go") or {}).get("final_verdict") == "GO"
            ),
        )
        reviewed_path = batch_path / "reviewed-apply-report.json"
        reviewed = self._require_release_report(
            reviewed_path,
            kind="reviewed-apply-report",
            predicate=lambda report: (
                (report.get("report") or {}).get("transaction") == "committed"
            ),
        )
        publish_path = batch_path / "publish-report.json"
        publish = self._require_release_report(
            publish_path,
            kind="publish-report",
            predicate=lambda report: (
                (report.get("bundle") or {}).get("status") == "PASS"
                and (report.get("viewer") or {}).get("status") == "PASS"
                and bool(report.get("node_entrypoints"))
                and all(
                    item.get("status") == "PASS"
                    for item in report.get("node_entrypoints") or []
                )
            ),
        )
        journal_path = batch_path / "journal.json"
        journal = (
            json.loads(journal_path.read_text(encoding="utf-8"))
            if journal_path.is_file() else {}
        )
        if journal.get("state") != "published":
            raise GovernanceIntegrationError("Wiki staging journal is not published")

        source_roots = (
            ("wiki", workspace_path / "wiki" / industry),
            ("sources", workspace_path / "sources" / industry),
        )
        files: dict[str, bytes] = {}
        for release_prefix, source_root in source_roots:
            if not source_root.is_dir():
                raise GovernanceIntegrationError(
                    f"Wiki release source is missing: {source_root.relative_to(workspace_path)}"
                )
            for source in sorted(source_root.rglob("*")):
                if source.is_symlink():
                    raise GovernanceIntegrationError("Wiki release source contains a symlink")
                if source.is_file():
                    relative = source.relative_to(source_root)
                    files[str(Path(release_prefix) / relative)] = source.read_bytes()
        for name in (
            f"{industry}-name-graph.json",
            f"{industry}-name-graph.html",
            f"{industry}-wiki-data.js",
            f"{industry}-wiki.html",
            f"{industry}-wiki-{node}.html",
        ):
            source = workspace_path / "docs" / name
            if not source.is_file() or source.is_symlink():
                raise GovernanceIntegrationError(f"Wiki release artifact is missing: docs/{name}")
            files[f"docs/{name}"] = source.read_bytes()

        destination = self.root / "var" / "publications" / "wiki" / industry
        expected_current = {
            relative: (
                hashlib.sha256((destination / relative).read_bytes()).hexdigest()
                if (destination / relative).is_file() else None
            )
            for relative in files
        }
        manager = ReleaseManager(
            self.root / "var" / "releases" / "wiki",
            required_gates={"G10", "G11"},
            proof_authority=self.proof_authority,
        )
        manifest = {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in files.items()
        }
        subject = manager.subject_for(manifest)
        policy_version = str(
            (job or {}).get("payload", {}).get(
                "policy_version", "wiki-production-v4"
            )
        )
        gate_receipts = [
            self.proof_authority.issue_gate(
                gate_id=gate_id,
                input_hashes=list(manifest.values()),
                policy_version=policy_version,
                subject=subject,
                producer="release-checker",
            )
            for gate_id in ("G10", "G11")
        ]
        staged = manager.stage(
            files,
            expected_current=expected_current,
            gate_results=gate_receipts,
        )
        governed = self.wrap_release_manager(manager)
        if isinstance(governed, GovernedReleaseManager):
            context = self.release_context(job_id)
            backup = governed.apply(
                staged,
                destination,
                job_id=job_id,
                risk=risk,
                runtime_fingerprint=context["runtime_fingerprint"],
                input_scope=context["input_scope"],
            )
            decision = (
                governed.last_eligibility.decision.value
                if governed.last_eligibility is not None else "not_evaluated"
            )
        else:
            backup = governed.apply(staged, destination)
            decision = "governance_disabled"
        record = {
            "protocol": "release-record-v1",
            "publication_status": "published",
            "release_id": staged.id,
            "job_id": job_id,
            "industry": industry,
            "node": node,
            "destination": str(destination),
            "candidate_hashes": manifest,
            "gate_report_sha256": hashlib.sha256(gate_path.read_bytes()).hexdigest(),
            "reviewed_apply_sha256": hashlib.sha256(reviewed_path.read_bytes()).hexdigest(),
            "publish_report_sha256": hashlib.sha256(publish_path.read_bytes()).hexdigest(),
            "governance_decision": decision,
            "backup_path": str(backup),
        }
        record_path = batch_path / "release-record.json"
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.events.append(
            "job", job_id, "governance.wiki_release_applied", record,
            actor="governance-runtime",
        )
        return record

    def wrap_release_manager(self, manager: ReleaseManager) -> ReleaseManager | GovernedReleaseManager:
        if not self.enabled:
            return manager
        return GovernedReleaseManager(manager, self.controller, mode=self.mode)


__all__ = [
    "GovernanceIntegrationError", "GovernanceRuntime", "WorkflowGovernanceBinding",
]
