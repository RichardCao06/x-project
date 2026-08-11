"""Bounded, evidence-preserving repair decisions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RepairAction(StrEnum):
    RETRY = "retry"
    REPAIR = "repair"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class RepairDecision:
    action: RepairAction
    reason: str
    next_attempt: int | None = None


class RepairRouter:
    transient_codes = frozenset({"TIMEOUT", "PROCESS_EXIT", "RATE_LIMIT", "TEMPORARY_IO"})
    contract_codes = frozenset({"OUTPUT_PROTOCOL", "SCHEMA_VIOLATION", "HASH_MISMATCH"})

    def decide(self, error_code: str, *, attempt: int, max_attempts: int, repair_available: bool = False) -> RepairDecision:
        if error_code in self.transient_codes and attempt < max_attempts:
            return RepairDecision(RepairAction.RETRY, f"transient {error_code}", attempt + 1)
        if error_code in self.contract_codes and repair_available and attempt < max_attempts:
            return RepairDecision(RepairAction.REPAIR, f"deterministic repair for {error_code}", attempt + 1)
        return RepairDecision(RepairAction.QUARANTINE, f"non-retryable or retry budget exhausted: {error_code}")
