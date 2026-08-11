"""Claim-set conservation for Wiki reviewed upgrades."""
from __future__ import annotations

import re
from typing import Any


_CITATION = re.compile(r"\[\^([^\]]+)\]")
_DEFINITION = re.compile(r"^\[\^[^\]]+\]:")


def validate_coverage(value: dict[str, Any]) -> dict[str, Any]:
    required = set(value.get("required_claim_ids", ()))
    reported = set(value.get("reported_claim_ids", ()))
    statuses = dict(value.get("statuses", {}))
    unknown = reported - required
    absent_status = required - statuses.keys()
    missing = required - reported
    dispositions = {"CONFIRMED", "INSUFFICIENT", "CONTRADICTED", "NOT_FOUND", "BUDGET_SKIPPED", "UNMAPPED"}
    invalid = {claim_id for claim_id in required if statuses.get(claim_id) not in dispositions}
    # The denominator is the frozen required set, never the subset returned by
    # Verify.  Missing/skipped/unmapped claims remain explicit non-passing rows.
    return {
        "coverage_set": required,
        "reported_set": reported,
        "missing_claim_ids": missing,
        "unknown_claim_ids": unknown,
        "invalid_claim_ids": invalid | absent_status,
        "reviewed_upgrade_allowed": not (missing or unknown or invalid or absent_status)
        and all(statuses[item] == "CONFIRMED" for item in required),
    }


def factual_claims(markdown: str) -> list[dict[str, Any]]:
    """Atomize prose without losing paragraph-end shared citations.

    A footnote placed after the final period applies to every factual sentence
    in that paragraph, so every sentence stays in the coverage denominator.
    """
    claims: list[dict[str, Any]] = []
    paragraphs: list[str] = []
    current: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(" ".join(current)); current = []
            continue
        if stripped.startswith("#") or _DEFINITION.match(stripped) or stripped.startswith("---"):
            continue
        current.append(stripped)
    if current:
        paragraphs.append(" ".join(current))
    for paragraph in paragraphs:
        citations = list(dict.fromkeys(_CITATION.findall(paragraph)))
        prose = _CITATION.sub("", paragraph)
        for sentence in re.findall(r"[^。！？.!?]+[。！？.!?]?", prose):
            text = sentence.strip()
            if text:
                claims.append({"text": text, "citations": citations.copy()})
    return claims

