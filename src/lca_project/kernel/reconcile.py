"""Deterministic reconciliation of desired state against recorded outcomes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ReconcileReport:
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]
    duplicates: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.missing or self.unexpected or self.duplicates)


def reconcile(expected: Iterable[str], observed: Iterable[str]) -> ReconcileReport:
    wanted, actual = set(expected), list(observed)
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in actual:
        if item in seen:
            duplicates.add(item)
        seen.add(item)
    got = set(actual)
    return ReconcileReport(tuple(sorted(wanted - got)), tuple(sorted(got - wanted)), tuple(sorted(duplicates)))
