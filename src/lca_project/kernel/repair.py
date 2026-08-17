"""Versioned, fail-closed repair policy decisions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path
from typing import Any


class RepairAction(StrEnum):
    RETRY = "retry"
    REPAIR = "repair"
    RECOVER = "recover"
    QUARANTINE = "quarantine"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class RepairDecision:
    action: RepairAction
    reason: str
    next_attempt: int | None = None
    repairer_capability: str | None = None
    invalidates: tuple[str, ...] = ()
    preserves: tuple[str, ...] = ()
    policy_version: str = "legacy"
    policy_hash: str = ""


class RepairPolicyRegistry:
    """Load one immutable policy; capability hints never grant repair authority."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        raw_bytes = self.path.read_bytes()
        self.policy_hash = hashlib.sha256(raw_bytes).hexdigest()
        value = json.loads(raw_bytes)
        if value.get("schema_version") != "wiki-repair-policy-v1":
            raise ValueError("unsupported repair policy schema")
        self.version = str(value.get("version") or "")
        rules = value.get("rules")
        if not self.version or not isinstance(rules, dict):
            raise ValueError("repair policy requires version and rules")
        self.rules: dict[str, dict[str, Any]] = rules
        self.unknown_action = RepairAction(value.get("unknown_failure_action", "quarantine"))

    def decide(self, error_code: str, *, attempt: int, max_attempts: int,
               actor: str = "worker") -> RepairDecision:
        rule = self.rules.get(error_code)
        if rule is None:
            return self._decision(
                self.unknown_action, f"unknown failure is fail-closed: {error_code}", None, {}
            )
        allowed_actors = rule.get("allowed_actors", ["worker"])
        if actor not in allowed_actors:
            return self._decision(RepairAction.QUARANTINE,
                                  f"actor {actor} is not authorized for {error_code}", None, rule)
        policy_limit = int(rule.get("max_automatic_attempts", 0))
        action = RepairAction(str(rule.get("action", "quarantine")))
        automatic = action in {RepairAction.RETRY, RepairAction.RECOVER, RepairAction.REPAIR}
        effective_limit = min(max_attempts, policy_limit) if policy_limit else 0
        # ``attempt`` counts automatic repairs already consumed.  The Worker
        # must pass the pre-claim Task attempt, not the just-failed ordinal.
        if automatic and attempt >= effective_limit:
            exhausted = RepairAction(str(rule.get("exhausted_state", "quarantine")))
            return self._decision(exhausted, f"repair budget exhausted for {error_code}", None, rule)
        next_attempt = attempt + 1 if automatic else None
        return self._decision(action, f"policy {self.version} classified {error_code}",
                              next_attempt, rule)

    def _decision(self, action: RepairAction, reason: str, next_attempt: int | None,
                  rule: dict[str, Any]) -> RepairDecision:
        return RepairDecision(
            action, reason, next_attempt, rule.get("repairer_capability"),
            tuple(rule.get("invalidates", [])), tuple(rule.get("preserves", [])),
            self.version, self.policy_hash,
        )


class RepairRouter:
    """Compatibility facade for callers that have not yet supplied a policy path."""

    transient_codes = frozenset({"TIMEOUT", "RATE_LIMIT", "TEMPORARY_IO", "PROVIDER_TIMEOUT"})
    contract_codes = frozenset({"OUTPUT_PROTOCOL", "SCHEMA_VIOLATION", "HASH_MISMATCH"})

    def decide(self, error_code: str, *, attempt: int, max_attempts: int,
               repair_available: bool = False) -> RepairDecision:
        if error_code in self.transient_codes and attempt < max_attempts:
            return RepairDecision(RepairAction.RETRY, f"transient {error_code}", attempt + 1)
        if error_code in self.contract_codes and repair_available and attempt < max_attempts:
            return RepairDecision(RepairAction.REPAIR, f"deterministic repair for {error_code}", attempt + 1)
        return RepairDecision(RepairAction.QUARANTINE,
                              f"non-retryable or retry budget exhausted: {error_code}")
