"""Risk-tier editorial review policy for preview and reviewed publication modes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


PREVIEW_BLOCKING_ISSUES = frozenset({
    "identity_drift", "citation_intrusion", "claim_dump", "unsupported_fusion",
    "poor_transition", "disconnected", "other",
})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify_editorial_review(review: dict[str, Any], publication_mode: str) -> dict[str, Any]:
    issues = list(review.get("issues") or [])
    if review.get("verdict") == "GO":
        return {"decision": "accept", "blocking_issues": [], "advisory_issues": []}
    if publication_mode != "preview":
        return {"decision": "block", "blocking_issues": issues, "advisory_issues": []}
    blocking = [issue for issue in issues
                if str(issue.get("issue_type") or "other") in PREVIEW_BLOCKING_ISSUES]
    advisory = [issue for issue in issues if issue not in blocking]
    return {"decision": "block" if blocking else "accept_with_advisories",
            "blocking_issues": blocking, "advisory_issues": advisory}


def apply_editorial_policy(
    review_path: Path, content_path: Path, publication_mode: str,
    *, usage_path: Path | None = None,
) -> dict[str, Any]:
    review = json.loads(review_path.read_text(encoding="utf-8"))
    raw_review_hash = _sha256(review_path)
    classification = classify_editorial_review(review, publication_mode)
    decision_path = review_path.parent / "editorial-policy-decision.json"
    advisory_path = review_path.parent / "editorial-advisories.json"
    if classification["decision"] == "accept_with_advisories":
        advisory_path.write_text(json.dumps({
            "protocol": "wiki-editorial-advisories-v1",
            "node_id": review.get("node_id"),
            "content_sha256": _sha256(content_path),
            "raw_review_sha256": raw_review_hash,
            "issues": classification["advisory_issues"],
            "raw_checks": review.get("checks") or {},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if usage_path and usage_path.is_file():
            usage = json.loads(usage_path.read_text(encoding="utf-8"))
            usage.update({"policy_decision": "accept_with_advisories",
                          "raw_model_verdict": review.get("verdict"),
                          "advisory_count": len(classification["advisory_issues"])})
            usage_path.write_text(json.dumps(usage, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    decision = {
        "protocol": "wiki-editorial-policy-decision-v1",
        "publication_mode": publication_mode,
        "decision": classification["decision"],
        "content_sha256": _sha256(content_path),
        "raw_review_sha256": raw_review_hash,
        "review_sha256": _sha256(review_path),
        "advisory_count": len(classification["advisory_issues"]),
        "blocking_count": len(classification["blocking_issues"]),
    }
    decision_path.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return {**classification, "decision_path": str(decision_path),
            "review_sha256": decision["review_sha256"]}
