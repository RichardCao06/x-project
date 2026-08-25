"""Validated failure envelopes emitted by capabilities or infrastructure adapters."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


INFRASTRUCTURE_CODES = frozenset({
    "TIMEOUT", "PROCESS_EXIT", "OUTPUT_PROTOCOL", "SIDE_EFFECT", "POLICY",
    "TEMPORARY_IO", "RATE_LIMIT", "PROVIDER_TIMEOUT", "WORKER_LOST",
})


@dataclass(frozen=True)
class FailureEnvelope:
    code: str
    category: str
    scope: str
    message: str
    evidence_artifacts: tuple[str, ...] = ()
    reported_retryable: bool | None = None
    reported_automatic_repair: str | None = None
    reported_invalidates: tuple[str, ...] = ()
    reported_preserves: tuple[str, ...] = ()
    gate_id: str | None = None
    gate_version: str | None = None
    gate_decision: str | None = None
    failed_requirement_ids: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    question_contract_sha256: str | None = None
    strategy_hash: str | None = None
    gate_result: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_capability(cls, value: Any) -> "FailureEnvelope":
        if not isinstance(value, dict):
            raise ValueError("failure must be an object")
        code = str(value.get("code") or "").strip()
        if not code:
            raise ValueError("failure.code is required")
        if code in {"PROCESS_EXIT", "TIMEOUT"}:
            raise ValueError(f"capability may not report adapter-owned infrastructure code {code}")
        category = str(value.get("category") or "business_validation").strip()
        scope = str(value.get("scope") or "task").strip()
        message = str(value.get("message") or code).strip()
        return cls(
            code=code, category=category, scope=scope, message=message,
            evidence_artifacts=_strings(value.get("evidence_artifacts")),
            reported_retryable=(value.get("retryable")
                                if isinstance(value.get("retryable"), bool) else None),
            reported_automatic_repair=(str(value["automatic_repair"])
                                       if value.get("automatic_repair") else None),
            reported_invalidates=_strings(value.get("invalidates")),
            reported_preserves=_strings(value.get("preserves")),
            gate_id=_optional_string(value.get("gate_id")),
            gate_version=_optional_string(value.get("gate_version")),
            gate_decision=_optional_string(value.get("gate_decision")),
            failed_requirement_ids=_strings(value.get("failed_requirement_ids")),
            metrics=_mapping(value.get("metrics"), "failure.metrics"),
            question_contract_sha256=_optional_string(value.get("question_contract_sha256")),
            strategy_hash=_optional_string(value.get("strategy_hash")),
            gate_result=_mapping(value.get("gate_result"), "failure.gate_result"),
        )

    @classmethod
    def infrastructure(cls, code: str, message: str, *, scope: str = "task") -> "FailureEnvelope":
        if code not in INFRASTRUCTURE_CODES:
            raise ValueError(f"not an infrastructure failure code: {code}")
        return cls(code, "infrastructure", scope, message)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError("failure list fields must contain non-empty strings")
    return tuple(value)


def _optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return dict(value)
