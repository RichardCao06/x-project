#!/usr/bin/env python3
"""Deterministic minimum-quality gate for a single research-ready node Wiki.

This gate is intentionally stricter than the repository-wide compatibility
lint.  It measures the v3 sample floor without granting ``reviewed`` status.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from wiki_claim_coverage import body_of, factual_sentences, frontmatter_of, normalize_claim
from wiki_quality_contract import (
    CLAIM_KINDS, CORE_EVIDENCE_ZONES, SECTIONS, TABLE_MIN_ROWS,
    required_external_claim_slots, required_external_sections,
)


BODY_HEADINGS = SECTIONS["product"]  # compatibility export for existing callers
STATUS_RE = re.compile(r"〔(?:图谱事实|建模判断|证据缺口)〕")
CITE_RE = re.compile(r"\[\^([a-z0-9-]+)\](?!:)")
REQUIRED_DISABLED = {
    "browser_use", "in_app_browser", "computer_use", "standalone_web_search",
    "remote_plugin", "plugins", "apps", "multi_agent",
}


def table_rows(text: str, kind: str) -> int:
    match = re.search(rf"<!-- EV:{kind}:START -->(.*?)<!-- EV:{kind}:END -->", text, re.S)
    if not match:
        return 0
    rows = [line for line in match.group(1).splitlines() if line.strip().startswith("|")]
    return max(0, len(rows) - 2)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def argv_values(argv: list[str], flag: str) -> list[str]:
    """Return every value bound to an argv flag; a dangling flag is invalid."""
    return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == flag]


def runtime_attestation(runtime_dir: Path, frozen_rows: list[dict]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    paths = {
        "invocation": runtime_dir / "verify-invocation.json",
        "events": runtime_dir / "verify-events.jsonl",
        "stderr": runtime_dir / "verify-stderr.log",
        "usage": runtime_dir / "verify-usage.json",
        "verdicts": runtime_dir / "verify-verdicts.runtime.json",
    }
    if missing := [key for key, path in paths.items() if not path.is_file()]:
        return False, [f"missing_{key}" for key in missing]
    invocation = json.loads(paths["invocation"].read_text(encoding="utf-8"))
    usage = json.loads(paths["usage"].read_text(encoding="utf-8"))
    verdicts = json.loads(paths["verdicts"].read_text(encoding="utf-8"))
    argv = [str(value) for value in invocation.get("argv", [])]
    if argv_values(argv, "-m") != ["gpt-5.6-sol"]:
        errors.append("argv_model_mismatch")
    if argv_values(argv, "-s") != ["read-only"]:
        errors.append("argv_sandbox_mismatch")
    if argv_values(argv, "-c") != ['model_reasoning_effort="medium"']:
        errors.append("argv_reasoning_effort_mismatch")
    argv_disabled = argv_values(argv, "--disable")
    metadata_disabled = invocation.get("disabled_capabilities", [])
    if (
        len(argv_disabled) != len(REQUIRED_DISABLED)
        or set(argv_disabled) != REQUIRED_DISABLED
        or not isinstance(metadata_disabled, list)
        or len(metadata_disabled) != len(REQUIRED_DISABLED)
        or set(metadata_disabled) != REQUIRED_DISABLED
        or argv_disabled != metadata_disabled
    ):
        errors.append("disabled_capabilities_argv_mismatch")
    if invocation.get("model") != "gpt-5.6-sol" or invocation.get("reasoning_effort") != "medium":
        errors.append("model_config_mismatch")
    if invocation.get("sandbox") != "read-only":
        errors.append("sandbox_not_read_only")
    evidence = Path(str(invocation.get("evidence", "")))
    if not evidence.is_file() or sha256(evidence) != invocation.get("evidence_sha256"):
        errors.append("evidence_hash_mismatch")
    artifacts = usage.get("artifacts", {})
    for key, path in paths.items():
        if key == "usage":
            continue
        expected = artifacts.get(f"{key}_sha256")
        if not expected:
            errors.append(f"{key}_hash_missing")
        elif sha256(path) != expected:
            errors.append(f"{key}_hash_mismatch")
    if usage.get("exit_code") != 0 or not usage.get("usage_records"):
        errors.append("runtime_exit_or_usage_missing")
    event_rows = [json.loads(line) for line in paths["events"].read_text(encoding="utf-8").splitlines() if line.strip()]
    if not any(row.get("type") == "turn.completed" for row in event_rows):
        errors.append("turn_completed_missing")
    expected = {
        str(row.get("claim", {}).get("claim_id")): row
        for row in frozen_rows
        if row.get("verify", {}).get("verdict") != "NOT_FOUND"
        and (row.get("claim") or {}).get("claim_role") != "editorial_assertion"
    }
    actual = {str(row.get("claim_id")): row for row in verdicts.get("items", [])}
    if set(expected) != set(actual):
        errors.append("runtime_verdict_scope_mismatch")
    else:
        for claim_id, row in expected.items():
            decision = actual[claim_id]
            if (
                decision.get("verdict") != row.get("verify", {}).get("verdict")
                or decision.get("node_alignment") != row.get("verify", {}).get("node_alignment")
                or decision.get("supporting_quote", "") != row.get("verify", {}).get("supporting_quote", "")
                or decision.get("evidence_id") != row.get("fetchResult", {}).get("evidence_id")
            ):
                errors.append(f"runtime_verdict_mismatch:{claim_id}")
    return not errors, errors


def gate(page: Path, frozen: Path, runtime_dir: Path | None = None) -> dict:
    text = page.read_text(encoding="utf-8")
    body = body_of(text)
    frontmatter = frontmatter_of(text)
    result = json.loads(frozen.read_text(encoding="utf-8"))
    rows = result.get("result", result).get("claims", [])
    node_type = frontmatter.get("node_type", "product")
    if node_type not in SECTIONS:
        node_type = "product" if str(frontmatter.get("id", "")).startswith("P") else "activity"
    table_min = TABLE_MIN_ROWS[node_type]
    verdicts = Counter(row.get("verify", {}).get("verdict") for row in rows)
    confirmed = [row for row in rows if row.get("verify", {}).get("verdict") == "CONFIRMED"]
    confirmed_external = [
        row for row in confirmed
        if (row.get("claim") or {}).get("claim_kind") == "external_fact"
        and (row.get("verify") or {}).get("node_alignment") == "EXACT"
    ]
    domains = {
        (urlsplit(str(row.get("fetchResult", {}).get("url", ""))).hostname or "").lower()
        for row in confirmed_external
    } - {""}
    zones = {
        str((row.get("claim") or {}).get("section", "")) for row in confirmed_external
    } - {""}
    confirmed_sections = {
        str((row.get("claim") or {}).get("section", "")) for row in confirmed_external
    } - {""}
    required_sections = required_external_sections(node_type)
    required_slots = required_external_claim_slots(node_type)
    confirmed_slots = {
        str((row.get("claim") or {}).get("requirement_id", ""))
        for row in confirmed_external
    } - {""}
    core_zones = {
        zone: bool(sections & confirmed_sections)
        for zone, sections in CORE_EVIDENCE_ZONES[node_type].items()
    }
    invalid_claim_kinds = [
        str((row.get("claim") or {}).get("claim_id", ""))
        for row in rows
        if str((row.get("claim") or {}).get("claim_kind", "")) not in CLAIM_KINDS
    ]
    assertions = factual_sentences(body)
    classified_editorial_texts = {
        normalize_claim(str((row.get("claim") or {}).get("claim_text", "")))
        for row in rows
        if (row.get("claim") or {}).get("claim_role") == "editorial_assertion"
        and str((row.get("claim") or {}).get("claim_kind", "")) in CLAIM_KINDS
    }
    unclassified = [
        item for item in assertions
        if not item["citations"]
        and not STATUS_RE.search(item["text"])
        and normalize_claim(item["text"]) not in classified_editorial_texts
    ]
    tables = {kind: table_rows(text, kind) for kind in table_min}
    runtime_ok, runtime_errors = runtime_attestation(runtime_dir, rows) if runtime_dir else (False, ["runtime_dir_required"])
    checks = {
        "wiki_v2": frontmatter.get("schema_version") == "wiki-v2",
        "draft_not_fake_reviewed": frontmatter.get("body_status") == "draft",
        "research_ready_or_candidate": frontmatter.get("content_maturity") in {"candidate", "research_ready"},
        "fixed_body_headings": re.findall(r"(?m)^##\s+(.+?)\s*$", body) == SECTIONS[node_type],
        "no_model_recall_label": "未核实·模型回忆" not in body,
        "all_body_assertions_classified": not unclassified,
        "all_claim_kinds_valid": not invalid_claim_kinds,
        "all_confirmed_node_aligned": len(confirmed_external) == verdicts["CONFIRMED"],
        "confirmed_external_claims_meet_slot_floor": len(confirmed_external) >= len(required_slots),
        "independent_authorities_ge_2": len(domains) >= 2,
        "all_required_external_sections_confirmed": required_sections <= confirmed_sections,
        "all_required_external_claim_slots_confirmed": required_slots <= confirmed_slots,
        "core_identity_and_boundary_evidence": all(core_zones.values()),
        "typed_evidence_tables": all(tables[kind] >= minimum for kind, minimum in table_min.items()),
        "verify_runtime_attested": runtime_ok,
    }
    return {
        "protocol": "wiki-research-ready-v2",
        "node_id": frontmatter.get("id"),
        "go": all(checks.values()),
        "checks": checks,
        "metrics": {
            "verdicts": dict(verdicts), "confirmed_domains": sorted(domains),
            "confirmed_zones": sorted(zones), "confirmed_external_sections": sorted(confirmed_sections),
            "required_external_sections": sorted(required_sections),
            "confirmed_external_claim_slots": sorted(confirmed_slots),
            "required_external_claim_slots": sorted(required_slots),
            "core_evidence_zones": core_zones, "invalid_claim_kinds": invalid_claim_kinds,
            "body_assertions": len(assertions), "unclassified_assertions": unclassified,
            "evidence_table_rows": tables,
            "runtime_errors": runtime_errors,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page", type=Path)
    parser.add_argument("frozen", type=Path)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = gate(args.page, args.frozen, args.runtime_dir)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["go"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
