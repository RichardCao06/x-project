"""Versioned governance contracts for autonomous goal-aligned execution.

The module is deliberately dependency-free.  It validates the normative
contracts introduced by the v2 architecture without coupling their meaning to
an individual model, prompt, Gate, or Workflow implementation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Mapping


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CONTRACT_REF = re.compile(
    r"^(goal|autonomy|assurance|capability)://"
    r"([a-z0-9][a-z0-9_.-]*)@([A-Za-z0-9][A-Za-z0-9_.-]*)$"
)


class ContractKind(StrEnum):
    GOAL = "goal"
    AUTONOMY = "autonomy"
    ASSURANCE = "assurance"
    CAPABILITY = "capability"


SCHEMA_TO_KIND: dict[str, ContractKind] = {
    "goal-contract-v2": ContractKind.GOAL,
    "autonomy-contract-v1": ContractKind.AUTONOMY,
    "assurance-contract-v1": ContractKind.ASSURANCE,
    "capability-envelope-v1": ContractKind.CAPABILITY,
}


class ClauseStatus(StrEnum):
    PROVED = "proved"
    FAILED = "failed"
    INSUFFICIENT = "insufficient"
    NOT_APPLICABLE = "not_applicable"


class AlignmentVerdict(StrEnum):
    ALIGNED_COMPLETE = "aligned_complete"
    ALIGNED_INCOMPLETE = "aligned_incomplete"
    MISALIGNED = "misaligned"
    HUMAN_JUDGMENT_REQUIRED = "human_judgment_required"


class AutonomyDecision(StrEnum):
    AUTHORIZED = "authorized"
    BLOCKED = "blocked"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContractRef:
    kind: ContractKind
    contract_id: str
    version: str

    @classmethod
    def parse(cls, value: str) -> "ContractRef":
        match = _CONTRACT_REF.fullmatch(value)
        if match is None:
            raise ValueError(
                "contract ref must be kind://contract-id@version; "
                f"received {value!r}"
            )
        return cls(ContractKind(match.group(1)), match.group(2), match.group(3))

    def __str__(self) -> str:
        return f"{self.kind.value}://{self.contract_id}@{self.version}"


@dataclass(frozen=True)
class ContractDocument:
    ref: ContractRef
    payload: dict[str, Any]
    digest: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContractDocument":
        payload = json.loads(canonical_json(dict(value)))
        kind = validate_contract(payload)
        identifier_key = "envelope_id" if kind is ContractKind.CAPABILITY else "contract_id"
        ref = ContractRef(kind, str(payload[identifier_key]), str(payload["version"]))
        return cls(ref, payload, payload_digest(payload))


@dataclass(frozen=True)
class JobContractBinding:
    job_id: str
    goal_ref: str
    autonomy_ref: str
    assurance_ref: str
    capability_ref: str
    binding_hash: str = ""
    schema_version: str = "job-contract-binding-v1"

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id must not be empty")
        expected = {
            "goal_ref": ContractKind.GOAL,
            "autonomy_ref": ContractKind.AUTONOMY,
            "assurance_ref": ContractKind.ASSURANCE,
            "capability_ref": ContractKind.CAPABILITY,
        }
        for field_name, kind in expected.items():
            ref = ContractRef.parse(getattr(self, field_name))
            if ref.kind is not kind:
                raise ValueError(f"{field_name} must reference {kind.value}")
        if not self.binding_hash:
            value = {
                "job_id": self.job_id,
                "goal_ref": self.goal_ref,
                "autonomy_ref": self.autonomy_ref,
                "assurance_ref": self.assurance_ref,
                "capability_ref": self.capability_ref,
            }
            object.__setattr__(self, "binding_hash", payload_digest(value))

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AutonomyEligibility:
    eligibility_id: str
    decision: AutonomyDecision
    action: str
    risk: str
    binding_hash: str
    reasons: tuple[str, ...] = ()
    contract_hashes: dict[str, str] = field(default_factory=dict)
    requirement_evidence_hashes: dict[str, str] = field(default_factory=dict)
    schema_version: str = "autonomy-eligibility-v1"

    @property
    def authorized(self) -> bool:
        return self.decision is AutonomyDecision.AUTHORIZED

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AlignmentAssessment:
    assessment_id: str
    job_id: str
    binding_hash: str
    verdict: AlignmentVerdict
    terminal_state: str
    claimed_complete: bool
    clause_results: dict[str, str]
    prohibited_outcomes: dict[str, str]
    capability_match: bool
    findings: tuple[str, ...]
    evidence: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "alignment-assessment-v1"

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def _mapping(value: Any, path: str, *, nonempty: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ValueError(f"{path} must be a {qualifier}object")
    return value


def _no_extra(value: Mapping[str, Any], path: str, allowed: set[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{path} contains unsupported fields: {', '.join(unknown)}")


def _unique_texts(value: Any, path: str, *, nonempty: bool = False) -> list[str]:
    items = _list(value, path, nonempty=nonempty)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(_nonempty_text(item, f"{path}[{index}]"))
    if len(result) != len(set(result)):
        raise ValueError(f"{path} contains duplicate values")
    return result


def _nonempty_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _list(value: Any, path: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ValueError(f"{path} must be a {qualifier}array")
    return value


def _ratio(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{path} must be between 0 and 1")
    return result


def _unique_items(items: list[Any], path: str, *, key: str = "id") -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    identifiers: list[str] = []
    for index, raw in enumerate(items):
        item = _mapping(raw, f"{path}[{index}]")
        identifiers.append(_nonempty_text(item.get(key), f"{path}[{index}].{key}"))
        values.append(item)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{path} contains duplicate {key} values")
    return values


def _base(value: dict[str, Any], *, identifier_key: str = "contract_id") -> None:
    _nonempty_text(value.get("schema_version"), "schema_version")
    identifier = _nonempty_text(value.get(identifier_key), identifier_key)
    if _IDENTIFIER.fullmatch(identifier) is None:
        raise ValueError(f"{identifier_key} has an unsupported format")
    version = _nonempty_text(value.get("version"), "version")
    if _VERSION.fullmatch(version) is None:
        raise ValueError("version has an unsupported format")
    _mapping(value.get("scope"), "scope", nonempty=True)


def _validate_goal(value: dict[str, Any]) -> None:
    _no_extra(value, "Goal Contract", {
        "schema_version", "contract_id", "version", "purpose", "scope", "clauses",
        "prohibited_outcomes", "terminal_states", "reserved_authority", "evaluation",
        "metadata",
    })
    _base(value)
    purpose = _mapping(value.get("purpose"), "purpose")
    _no_extra(purpose, "purpose", {"decision", "value_statement", "stakeholders"})
    _nonempty_text(purpose.get("decision"), "purpose.decision")
    _nonempty_text(purpose.get("value_statement"), "purpose.value_statement")
    _unique_texts(purpose.get("stakeholders"), "purpose.stakeholders", nonempty=True)

    clauses = _unique_items(_list(value.get("clauses"), "clauses", nonempty=True), "clauses")
    allowed_criticality = {"hard", "required", "optimize", "diagnostic"}
    for index, clause in enumerate(clauses):
        _no_extra(
            clause,
            f"clauses[{index}]",
            {"id", "statement", "criticality", "proof_obligation"},
        )
        _nonempty_text(clause.get("statement"), f"clauses[{index}].statement")
        criticality = _nonempty_text(clause.get("criticality"), f"clauses[{index}].criticality")
        if criticality not in allowed_criticality:
            raise ValueError(f"clauses[{index}].criticality is unsupported")
        _nonempty_text(clause.get("proof_obligation"), f"clauses[{index}].proof_obligation")
    if not any(item["criticality"] == "hard" for item in clauses):
        raise ValueError("Goal Contract requires at least one hard clause")

    prohibited = _unique_items(
        _list(value.get("prohibited_outcomes"), "prohibited_outcomes", nonempty=True),
        "prohibited_outcomes",
    )
    for index, outcome in enumerate(prohibited):
        _no_extra(
            outcome,
            f"prohibited_outcomes[{index}]",
            {"id", "statement", "detection"},
        )
        _nonempty_text(outcome.get("statement"), f"prohibited_outcomes[{index}].statement")
        _nonempty_text(outcome.get("detection"), f"prohibited_outcomes[{index}].detection")

    states = _mapping(value.get("terminal_states"), "terminal_states", nonempty=True)
    allowed_state_kinds = {"success", "honest_incomplete", "escalation", "failure"}
    kinds: set[str] = set()
    for state_name, raw in states.items():
        _nonempty_text(state_name, "terminal_states key")
        state = _mapping(raw, f"terminal_states.{state_name}")
        _no_extra(state, f"terminal_states.{state_name}", {"kind", "meaning"})
        state_kind = _nonempty_text(state.get("kind"), f"terminal_states.{state_name}.kind")
        if state_kind not in allowed_state_kinds:
            raise ValueError(f"terminal_states.{state_name}.kind is unsupported")
        kinds.add(state_kind)
        _nonempty_text(state.get("meaning"), f"terminal_states.{state_name}.meaning")
    if "success" not in kinds or "honest_incomplete" not in kinds:
        raise ValueError("Goal Contract requires success and honest_incomplete terminal states")

    _unique_texts(value.get("reserved_authority"), "reserved_authority", nonempty=True)
    evaluation = _mapping(value.get("evaluation"), "evaluation")
    _no_extra(
        evaluation,
        "evaluation",
        {"cohort_refs", "selective_risk_ceiling", "false_pass_budget"},
    )
    _unique_texts(evaluation.get("cohort_refs"), "evaluation.cohort_refs", nonempty=True)
    _ratio(evaluation.get("selective_risk_ceiling"), "evaluation.selective_risk_ceiling")
    _ratio(evaluation.get("false_pass_budget"), "evaluation.false_pass_budget")


def _validate_autonomy(value: dict[str, Any]) -> None:
    _no_extra(value, "Autonomy Contract", {
        "schema_version", "contract_id", "version", "scope", "actions",
        "forbidden_actions", "reserved_authority", "escalation_triggers", "budgets",
    })
    _base(value)
    scope = _mapping(value.get("scope"), "scope", nonempty=True)
    if "goal_contract_ref" in scope:
        ref = ContractRef.parse(
            _nonempty_text(scope["goal_contract_ref"], "scope.goal_contract_ref")
        )
        if ref.kind is not ContractKind.GOAL:
            raise ValueError("scope.goal_contract_ref must reference a Goal Contract")
    actions = _mapping(value.get("actions"), "actions", nonempty=True)
    allowed_risks = {"low", "medium", "high", "critical"}
    for action_name, raw in actions.items():
        _nonempty_text(action_name, "actions key")
        action = _mapping(raw, f"actions.{action_name}")
        _no_extra(action, f"actions.{action_name}", {"automatic", "max_risk", "requirements"})
        if not isinstance(action.get("automatic"), bool):
            raise ValueError(f"actions.{action_name}.automatic must be boolean")
        maximum = _nonempty_text(action.get("max_risk"), f"actions.{action_name}.max_risk")
        if maximum not in allowed_risks:
            raise ValueError(f"actions.{action_name}.max_risk is unsupported")
        _unique_texts(action.get("requirements", []), f"actions.{action_name}.requirements")

    for key in ("forbidden_actions", "reserved_authority", "escalation_triggers"):
        _unique_texts(value.get(key), key, nonempty=True)
    _mapping(value.get("budgets"), "budgets", nonempty=True)


def _validate_assurance(value: dict[str, Any]) -> None:
    _no_extra(value, "Assurance Contract", {
        "schema_version", "contract_id", "version", "scope", "goal_contract_ref",
        "proof_obligations", "release_rule", "sampling", "recertification_triggers",
    })
    _base(value)
    goal_ref = ContractRef.parse(_nonempty_text(value.get("goal_contract_ref"), "goal_contract_ref"))
    if goal_ref.kind is not ContractKind.GOAL:
        raise ValueError("goal_contract_ref must reference a Goal Contract")
    obligations = _mapping(value.get("proof_obligations"), "proof_obligations", nonempty=True)
    for clause_id, raw in obligations.items():
        _nonempty_text(clause_id, "proof_obligations key")
        obligation = _mapping(raw, f"proof_obligations.{clause_id}")
        _no_extra(
            obligation,
            f"proof_obligations.{clause_id}",
            {"evaluator", "evidence_types", "independence_required"},
        )
        _nonempty_text(obligation.get("evaluator"), f"proof_obligations.{clause_id}.evaluator")
        _unique_texts(
            obligation.get("evidence_types"),
            f"proof_obligations.{clause_id}.evidence_types",
            nonempty=True,
        )
        if not isinstance(obligation.get("independence_required"), bool):
            raise ValueError(
                f"proof_obligations.{clause_id}.independence_required must be boolean"
            )

    release = _mapping(value.get("release_rule"), "release_rule")
    _no_extra(
        release,
        "release_rule",
        {"all_hard_proved", "all_required_resolved", "no_prohibited_outcomes"},
    )
    for key in ("all_hard_proved", "all_required_resolved", "no_prohibited_outcomes"):
        if not isinstance(release.get(key), bool):
            raise ValueError(f"release_rule.{key} must be boolean")
    _mapping(value.get("sampling"), "sampling", nonempty=True)
    _unique_texts(
        value.get("recertification_triggers"),
        "recertification_triggers",
        nonempty=True,
    )


def _validate_capability(value: dict[str, Any]) -> None:
    _no_extra(value, "Capability Envelope", {
        "schema_version", "envelope_id", "version", "scope", "capability_id",
        "runtime_fingerprint", "input_scope", "budget", "certification",
        "known_failures", "fallback",
    })
    _base(value, identifier_key="envelope_id")
    _nonempty_text(value.get("capability_id"), "capability_id")
    fingerprint = _mapping(value.get("runtime_fingerprint"), "runtime_fingerprint")
    _no_extra(fingerprint, "runtime_fingerprint", {"model", "prompt", "toolset", "workflow"})
    for key in ("model", "prompt", "toolset", "workflow"):
        _nonempty_text(fingerprint.get(key), f"runtime_fingerprint.{key}")
    _mapping(value.get("input_scope"), "input_scope", nonempty=True)
    _mapping(value.get("budget"), "budget", nonempty=True)
    certification = _mapping(value.get("certification"), "certification")
    _no_extra(certification, "certification", {
        "status", "cohort_id", "evidence_refs", "sample_size", "coverage",
        "selective_risk_upper_bound", "abstention_recall", "valid_until",
    })
    status = _nonempty_text(certification.get("status"), "certification.status")
    if status not in {"shadow", "certified", "suspended", "expired"}:
        raise ValueError("certification.status is unsupported")
    _nonempty_text(certification.get("cohort_id"), "certification.cohort_id")
    _unique_texts(
        certification.get("evidence_refs"),
        "certification.evidence_refs",
        nonempty=True,
    )
    sample_size = certification.get("sample_size")
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size <= 0:
        raise ValueError("certification.sample_size must be a positive integer")
    _ratio(certification.get("coverage"), "certification.coverage")
    _ratio(
        certification.get("selective_risk_upper_bound"),
        "certification.selective_risk_upper_bound",
    )
    _ratio(certification.get("abstention_recall"), "certification.abstention_recall")
    valid_until = certification.get("valid_until")
    if valid_until is not None:
        text = _nonempty_text(valid_until, "certification.valid_until")
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("certification.valid_until must be ISO-8601") from exc
    _unique_texts(value.get("known_failures", []), "known_failures")
    fallback = _mapping(value.get("fallback"), "fallback")
    _no_extra(fallback, "fallback", {"terminal_state", "reason"})
    _nonempty_text(fallback.get("terminal_state"), "fallback.terminal_state")
    _nonempty_text(fallback.get("reason"), "fallback.reason")

def validate_contract(value: dict[str, Any]) -> ContractKind:
    """Validate one governance contract and return its semantic kind."""
    schema = _nonempty_text(value.get("schema_version"), "schema_version")
    try:
        kind = SCHEMA_TO_KIND[schema]
    except KeyError as exc:
        raise ValueError(f"unsupported governance contract schema: {schema}") from exc
    {
        ContractKind.GOAL: _validate_goal,
        ContractKind.AUTONOMY: _validate_autonomy,
        ContractKind.ASSURANCE: _validate_assurance,
        ContractKind.CAPABILITY: _validate_capability,
    }[kind](value)
    return kind
