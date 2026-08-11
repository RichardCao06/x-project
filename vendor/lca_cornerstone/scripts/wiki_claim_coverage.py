#!/usr/bin/env python3
"""Build and apply a deterministic, claim-level Wiki coverage plan.

The planner joins a wiki-batch manifest/prepared file, per-node frozen
``claims.json``/``kus.json`` artifacts, and the current Wiki BODY.  It never
trusts frontmatter status labels.  ``body_status=reviewed`` is proposed only
when every factual sentence is either backed by a compliant CONFIRMED result
or explicitly marked as a safe editorial downgrade.

Usage:
  python3 scripts/wiki_claim_coverage.py plan <prepared.json> [--output FILE]
  python3 scripts/wiki_claim_coverage.py apply-plan <coverage.json> [--write]

``apply-plan`` is hash-locked and is dry-run unless ``--write`` is supplied;
the batch control plane can import :func:`apply_plan` for transactional use.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_VERSION = "wiki-claim-coverage-v1"
BODY_RE = re.compile(r"<!-- BODY:START -->(.*?)<!-- BODY:END -->", re.S)
FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
CITE_RE = re.compile(r"\[\^([a-z0-9\-]+)\](?!:)")
DOWNGRADE_RE = re.compile(r"〔(?:图谱事实|建模判断|证据缺口|未核实(?:·模型回忆)?)〕")
STATUS_MARK_RE = re.compile(r"(?:✅已核实(?:\([^)]*\))?|〔(?:图谱事实|建模判断|证据缺口|未核实(?:·模型回忆)?|历史记录)〕)")
VERDICTS = {"CONFIRMED", "CONTRADICTED", "NOT_FOUND", "INSUFFICIENT"}


class CoverageError(ValueError):
    pass


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CoverageError(f"{path} 顶层必须是对象")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def artifact_hash(artifact: dict[str, Any]) -> str:
    value = json.loads(json.dumps(artifact, ensure_ascii=False))
    value.pop("artifact_sha256", None)
    return sha256_text(canonical_json(value))


def resolve_path(value: str | Path, repo_root: Path, relative_to: Path | None = None) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    candidates = [repo_root / path]
    if relative_to is not None:
        candidates.append(relative_to / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def body_of(text: str) -> str:
    match = BODY_RE.search(text)
    if not match:
        raise CoverageError("Wiki 页面缺少 BODY 标记")
    return match.group(1)


def frontmatter_of(text: str) -> dict[str, str]:
    match = FM_RE.match(text)
    if not match:
        raise CoverageError("Wiki 页面缺少 frontmatter")
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        field = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if field:
            result[field.group(1)] = field.group(2).strip().strip('"')
    return result


def normalize_text(value: str) -> str:
    """Normalize text for exact claim/quote containment without paraphrasing."""
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_claim(value: str) -> str:
    value = CITE_RE.sub("", value)
    value = STATUS_MARK_RE.sub("", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"^[\-*+]\s+", "", value.strip())
    return normalize_text(value).strip()


def is_external_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return False
    host = hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


def factual_sentences(body: str) -> list[dict[str, Any]]:
    """Conservatively enumerate BODY prose that needs a coverage disposition.

    Tables, headings, HTML, block quotes and footnote definitions are governed
    by other lint sections.  Remaining prose is treated as factual unless it is
    explicitly downgraded.  This conservative boundary is deliberate: an
    uncited factual-looking sentence must not disappear merely because the old
    citation-driven atomizer did not select it.
    """
    body = re.split(r"\n##\s*出处\s*\n", body, maxsplit=1)[0]
    found: list[dict[str, Any]] = []
    section = ""
    for line_number, raw in enumerate(body.splitlines(), start=1):
        line = raw.strip()
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            section = heading.group(1)
            continue
        if (
            not line
            or line.startswith(("#", ">", "<!--", "```"))
            or re.match(r"^\[\^[a-z0-9\-]+\]:", line)
        ):
            continue
        line_cites = CITE_RE.findall(line)
        if line.startswith("|"):
            # Evidence-table headers/separators are schema, not assertions.
            # Cited data rows are frozen by prep_node_wiki and must participate
            # in the same bidirectional claim ledger as prose.
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if not line_cites or all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
        # Keep a trailing status/citation attached to the assertion before
        # punctuation splitting, matching prep_node_wiki's citation semantics.
        parse_line = re.sub(
            r"([。！？])\s*((?:(?:\[\^[a-z0-9\-]+\])|(?:〔(?:图谱事实|建模判断|证据缺口|未核实(?:·模型回忆)?)〕)|(?:✅已核实(?:\([^)]*\))?))+)",
            r"\2\1",
            line,
        )
        # A generated KU occupies one prose line.  A Chinese semicolon may be
        # part of that frozen claim text, so splitting on it fabricates two
        # page-side assertions that can never hard-join back to the one claim.
        for ordinal, sentence in enumerate(re.split(r"(?<=[。！？])", parse_line), start=1):
            sentence = sentence.strip()
            if not sentence:
                continue
            normalized = normalize_claim(sentence)
            # Tiny labels/list keys do not constitute useful external claims.
            if len(normalized) < 8 or re.fullmatch(r"[\w\s:/.-]+[:：]", normalized):
                continue
            cites = CITE_RE.findall(sentence) or line_cites
            found.append({
                "assertion_id": f"L{line_number}-{ordinal}",
                "section": section,
                "line_number": line_number,
                "text": normalized,
                "text_sha256": sha256_text(normalized),
                "citations": sorted(set(cites)),
                "explicit_downgrade": bool(DOWNGRADE_RE.search(sentence)),
            })
    return found


def _direct_evidence_check(row: dict[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
    fetch = row.get("fetchResult")
    verify = row.get("verify")
    errors: list[str] = []
    if not isinstance(fetch, dict):
        fetch = {}
        errors.append("missing_fetch")
    if not isinstance(verify, dict):
        verify = {}
        errors.append("missing_verify")
    url = str(fetch.get("url", "")).strip()
    excerpt = str(fetch.get("excerpt", "")).strip()
    quote = str(verify.get("supporting_quote", "")).strip()
    if fetch.get("status") != "found":
        errors.append("fetch_not_found")
    if not is_external_http_url(url):
        errors.append("non_external_url")
    if not excerpt:
        errors.append("empty_fetch_excerpt")
    if not quote:
        errors.append("empty_supporting_quote")
    if quote and excerpt and normalize_text(quote) not in normalize_text(excerpt):
        errors.append("supporting_quote_not_excerpt_substring")
    excerpt_hash = sha256_text(excerpt)
    quote_hash = sha256_text(quote)
    supplied_excerpt_hash = str(fetch.get("excerpt_sha256") or "").strip()
    source_content_hash = str(fetch.get("content_sha256") or "").strip()
    if supplied_excerpt_hash and supplied_excerpt_hash != excerpt_hash:
        errors.append("fetch_excerpt_hash_mismatch")
    if source_content_hash and not re.fullmatch(r"[0-9a-f]{64}", source_content_hash):
        errors.append("source_content_hash_invalid")
    evidence = {
        "url": url,
        "fetch_excerpt": excerpt,
        "supporting_quote": quote,
        "fetch_excerpt_sha256": excerpt_hash,
        "source_content_sha256": source_content_hash or None,
        "supporting_quote_sha256": quote_hash,
        "evidence_sha256": sha256_text(canonical_json({
            "url": url,
            "excerpt_sha256": excerpt_hash,
            "source_content_sha256": source_content_hash or None,
            "supporting_quote_sha256": quote_hash,
        })),
    }
    return not errors, evidence, errors


def evidence_check(
    row: dict[str, Any],
    rows_by_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, dict[str, Any], list[str]]:
    """Validate direct evidence or every frozen anchor behind an editorial assertion."""
    claim = row.get("claim") or {}
    evidence_ids = claim.get("evidence_claim_ids") or []
    if claim.get("claim_role") != "editorial_assertion" or not evidence_ids:
        return _direct_evidence_check(row)
    if rows_by_id is None:
        return False, {"derived_from": evidence_ids}, ["derived_evidence_index_missing"]
    evidence_items = []
    errors: list[str] = []
    for source_id in evidence_ids:
        source = rows_by_id.get(str(source_id))
        if source is None:
            errors.append(f"derived_evidence_missing:{source_id}")
            continue
        source_claim = source.get("claim") or {}
        if (
            source_claim.get("claim_role") not in {None, "research_claim", "evidence_anchor"}
            or source_claim.get("claim_kind") != "external_fact"
            or (source.get("verify") or {}).get("verdict") != "CONFIRMED"
        ):
            errors.append(f"derived_evidence_not_confirmed_anchor:{source_id}")
            continue
        valid, evidence, source_errors = _direct_evidence_check(source)
        evidence_items.append({"claim_id": source_id, **evidence})
        errors.extend(f"{source_id}:{error}" for error in source_errors)
        if not valid and not source_errors:
            errors.append(f"{source_id}:invalid_evidence")
    return not errors, {"derived_from": evidence_ids, "items": evidence_items}, errors


def frozen_rows(
    batch_dir: Path,
    node_id: str,
    records: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    kus: list[dict[str, Any]] = []
    node_root = batch_dir / "nodes" / node_id
    if records is not None:
        claim_record = records.get("claims") or {}
        ku_record = records.get("kus") or {}
        claim_path = Path(str(claim_record.get("path", ""))).resolve()
        ku_path = Path(str(ku_record.get("path", ""))).resolve()
        for path, record, label in (
            (claim_path, claim_record, "claims"),
            (ku_path, ku_record, "kus"),
        ):
            if not path.exists() or sha256_text(path.read_text(encoding="utf-8")) != record.get("sha256"):
                raise CoverageError(f"{node_id} 冻结 {label} artifact 缺失或漂移: {path}")
        discovered_claims = {path.resolve() for path in node_root.glob("*/claims.json")}
        discovered_kus = {path.resolve() for path in node_root.glob("*/kus.json")}
        if records.get("scope") == "curated-golden":
            # Golden authoring is intentionally outside the production batch
            # journal, but it must still freeze one explicit claims/KU pair and
            # may not mix in hidden per-node artifacts.
            if not claim_path.is_relative_to(batch_dir) or not ku_path.is_relative_to(batch_dir):
                raise CoverageError(f"{node_id} golden artifact 必须位于当前批次目录")
            if discovered_claims or discovered_kus:
                raise CoverageError(f"{node_id} golden artifact 与 per-node artifact 混用")
        elif discovered_claims != {claim_path} or discovered_kus != {ku_path}:
            raise CoverageError(f"{node_id} per-node artifact 集合与 frozen.json 不一致")
        claim_paths = [claim_path]
        ku_paths = [ku_path]
    else:
        claim_paths = sorted(node_root.glob("*/claims.json"))
        ku_paths = sorted(node_root.glob("*/kus.json"))
    for path in claim_paths:
        data = read_json(path)
        value = data.get("result", data).get("claims", [])
        if not isinstance(value, list):
            raise CoverageError(f"{path} claims 必须是数组")
        rows.extend(item for item in value if isinstance(item, dict))
    for path in ku_paths:
        value = read_json(path).get("kus", [])
        if not isinstance(value, list):
            raise CoverageError(f"{path} kus 必须是数组")
        kus.extend(item for item in value if isinstance(item, dict))
    return rows, kus


def merge_manual_reviews(batch_dir: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for path in sorted(batch_dir.glob("*merge-plan.json")):
        data = read_json(path)
        for item in data.get("files", []):
            node_id = str(item.get("node_id", ""))
            for review in item.get("manual_review", []):
                claim_id = str(review.get("claim_id", ""))
                if node_id and claim_id:
                    result[node_id].add(claim_id)
    return result


def _ku_consistency(rows: list[dict[str, Any]], kus: list[dict[str, Any]]) -> dict[str, list[str]]:
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ku in kus:
        by_id[str(ku.get("claim_id", ""))].append(ku)
    expected_authority = {
        "CONFIRMED": "reviewed",
        "CONTRADICTED": "contradicted",
        "NOT_FOUND": "draft",
        "INSUFFICIENT": "draft",
    }
    issues: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        claim = row.get("claim") or {}
        claim_id = str(claim.get("claim_id", ""))
        matches = by_id.get(claim_id, [])
        if len(matches) != 1:
            issues[claim_id].append("ku_missing_or_duplicate")
            continue
        verdict = str((row.get("verify") or {}).get("verdict", ""))
        ku = matches[0]
        if ku.get("authority") != expected_authority.get(verdict):
            issues[claim_id].append("ku_authority_verdict_mismatch")
        if normalize_claim(str(ku.get("claim_text", ""))) != normalize_claim(str(claim.get("claim_text", ""))):
            issues[claim_id].append("ku_claim_text_mismatch")
    return issues


def _frontmatter_updates(eligible: bool) -> dict[str, str]:
    return ({
        "schema_version": "wiki-v2",
        "body_status": "reviewed",
        "content_maturity": "research_ready",
        "provenance_status": "claim_verified",
        "claim_verification_status": "complete",
    } if eligible else {})


def plan_coverage(prepared_path: Path, repo_root: Path = ROOT) -> dict[str, Any]:
    prepared_path = prepared_path.resolve()
    prepared = read_json(prepared_path)
    protocol = prepared.get("protocol", {})
    if protocol.get("kind") != "prepared-batch":
        raise CoverageError("输入必须是 wiki-batch prepared-batch")
    manifest_path = resolve_path(prepared["manifest"], repo_root, prepared_path.parent)
    manifest = read_json(manifest_path)
    batch_dir = prepared_path.parent
    manual_by_node = merge_manual_reviews(batch_dir)
    committed_outputs: dict[str, str] = {}
    transaction_path = batch_dir / "apply-transaction.json"
    if transaction_path.exists():
        transaction = read_json(transaction_path)
        if transaction.get("state") == "committed":
            for target in transaction.get("targets", []):
                if not isinstance(target, dict):
                    continue
                path = str(Path(str(target.get("target", ""))).resolve())
                digest = str(target.get("new_sha256", ""))
                if path and re.fullmatch(r"[0-9a-f]{64}", digest):
                    committed_outputs[path] = digest
    golden_curation = prepared.get("golden_curation") is True
    pilot_only = bool(prepared.get("pilot_only") or manifest.get("protocol", {}).get("pilot_only"))
    upgrade_allowed = bool(prepared.get("reviewed_upgrade_allowed")) and not pilot_only
    frozen_node_artifacts: dict[str, Any] = {}
    if protocol.get("version") == "wiki-batch-v2" and not pilot_only:
        frozen_path = batch_dir / ("golden-frozen.json" if golden_curation else "frozen.json")
        if not frozen_path.exists():
            raise CoverageError(f"coverage 缺少 {frozen_path.name}")
        frozen_doc = read_json(frozen_path)
        expected_kind = "golden-frozen" if golden_curation else "frozen-batch"
        if (frozen_doc.get("protocol") or {}).get("kind") != expected_kind:
            raise CoverageError(f"{frozen_path.name} kind 非 {expected_kind}")
        if golden_curation:
            ready_record = frozen_doc.get("research_ready") or {}
            ready_path = Path(str(ready_record.get("path", ""))).resolve()
            if (
                not ready_path.is_file()
                or sha256_text(ready_path.read_text(encoding="utf-8")) != ready_record.get("sha256")
                or read_json(ready_path).get("go") is not True
            ):
                raise CoverageError("golden coverage 缺可重放的 research-ready GO 证明")
        frozen_node_artifacts = frozen_doc.get("node_artifacts") or {}
        expected_nodes = {str(item["node_id"]) for item in manifest.get("nodes", [])}
        if set(frozen_node_artifacts) != expected_nodes:
            raise CoverageError("frozen node_artifacts 与 manifest 节点集合不一致")
    node_reports: list[dict[str, Any]] = []
    aggregate = Counter()
    quote_total = quote_ok = 0

    for item in manifest.get("nodes", []):
        node_id = str(item["node_id"])
        page_path = resolve_path(item["page"], repo_root, manifest_path.parent)
        page_text = page_path.read_text(encoding="utf-8")
        body = body_of(page_text)
        body_hash = sha256_text(body)
        file_hash = sha256_text(page_text)
        transaction_hash = committed_outputs.get(str(page_path.resolve()))
        transaction_matches = bool(transaction_hash and transaction_hash == file_hash)
        fm = frontmatter_of(page_text)
        assertions = factual_sentences(body)
        rows, kus = frozen_rows(
            batch_dir, node_id, frozen_node_artifacts.get(node_id)
            if frozen_node_artifacts else None,
        )
        ku_issues = _ku_consistency(rows, kus)
        rows_by_id = {
            str((row.get("claim") or {}).get("claim_id", "")): row for row in rows
        }
        referenced_anchor_ids = {
            str(source_id)
            for row in rows
            if (row.get("claim") or {}).get("claim_role") == "editorial_assertion"
            for source_id in ((row.get("claim") or {}).get("evidence_claim_ids") or [])
        }
        by_text: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            claim = row.get("claim") or {}
            if str(claim.get("claim_id", "")) in referenced_anchor_ids:
                continue
            by_text[normalize_claim(str(claim.get("claim_text", "")))].append(row)
        claim_reports: list[dict[str, Any]] = []
        counts = Counter()
        matched_claim_ids: set[str] = set()
        manifest_hash = str(item.get("body_sha256", ""))
        anchor_hashes = {
            str((row.get("claim") or {}).get("body_sha256", ""))
            for row in rows if (row.get("claim") or {}).get("body_sha256")
        }
        hash_reasons = []
        if manifest_hash and manifest_hash != body_hash and not transaction_matches:
            hash_reasons.append("manifest_body_sha256_mismatch")
        if anchor_hashes and anchor_hashes != {body_hash} and not transaction_matches:
            hash_reasons.append("frozen_claim_body_sha256_mismatch")
        if hash_reasons:
            aggregate["hash_drift"] += 1

        for assertion in assertions:
            matches = by_text.get(assertion["text"], [])
            report = dict(assertion)
            report["claim_id"] = None
            report["evidence"] = None
            report["reasons"] = []
            if not matches:
                # A badge without a frozen claim_kind is still unaudited prose.
                # This closes the P003 path where model-written statements made
                # themselves eligible merely by appending a downgrade label.
                report["disposition"] = "missing"
                report["reasons"].append("factual_sentence_has_no_frozen_claim")
            elif len(matches) != 1:
                report["disposition"] = "manual_review"
                report["reasons"].append("claim_text_matches_multiple_frozen_rows")
            else:
                row = matches[0]
                claim = row.get("claim") or {}
                verify = row.get("verify") or {}
                claim_id = str(claim.get("claim_id", ""))
                claim_kind = str(claim.get("claim_kind", ""))
                verdict = str(verify.get("verdict", ""))
                report["claim_id"] = claim_id
                report["claim_kind"] = claim_kind
                report["verdict"] = verdict
                evidence_valid = False
                if verdict == "CONFIRMED":
                    quote_total += 1
                    evidence_valid, evidence, errors = evidence_check(row, rows_by_id)
                    report["evidence"] = evidence
                    report["reasons"].extend(errors)
                    if evidence_valid:
                        quote_ok += 1
                if claim_id in matched_claim_ids:
                    report["disposition"] = "manual_review"
                    report["reasons"].append("frozen_claim_maps_to_multiple_body_assertions")
                elif claim_id in manual_by_node.get(node_id, set()):
                    report["disposition"] = "manual_review"
                    report["reasons"].append("merge_plan_manual_review")
                elif ku_issues.get(claim_id):
                    report["disposition"] = "manual_review"
                    report["reasons"].extend(ku_issues[claim_id])
                elif claim_kind in {"internal_graph_fact", "modeling_judgment", "evidence_gap"}:
                    expected_source = (
                        "LCA-CORNERSTONE_GRAPH"
                        if claim_kind == "internal_graph_fact"
                        else "INTERNAL_MODELING_JUDGMENT"
                    )
                    if (
                        verdict == "NOT_FOUND"
                        and str(claim.get("believed_source", "")) == expected_source
                        and (
                            assertion["explicit_downgrade"]
                            or claim.get("claim_role") == "editorial_assertion"
                        )
                    ):
                        report["disposition"] = "controlled_internal"
                        report["editorial_controlled"] = (
                            claim.get("claim_role") == "editorial_assertion"
                        )
                    else:
                        report["disposition"] = "unresolved"
                        report["reasons"].append("internal_claim_protocol_mismatch")
                elif claim_kind != "external_fact":
                    report["disposition"] = "unresolved"
                    report["reasons"].append("missing_or_invalid_claim_kind")
                elif verdict == "CONTRADICTED":
                    report["disposition"] = "contradicted"
                elif verdict == "CONFIRMED":
                    if evidence_valid:
                        report["disposition"] = "confirmed"
                    else:
                        report["disposition"] = "unresolved"
                elif verdict in {"NOT_FOUND", "INSUFFICIENT"}:
                    # An unresolved external assertion may remain only after the
                    # current prose explicitly stops presenting it as reviewed
                    # fact.  CONTRADICTED is never made safe by a label.
                    report["disposition"] = (
                        "safe_degraded" if assertion["explicit_downgrade"] else "unresolved"
                    )
                else:
                    report["disposition"] = "unresolved"
                    report["reasons"].append("missing_or_invalid_verdict")
                matched_claim_ids.add(claim_id)
            counts[report["disposition"]] += 1
            claim_reports.append(report)

        # Coverage is bidirectional: every frozen verdict must map back to one
        # current BODY assertion.  Otherwise a contradicted/omitted table row
        # could disappear from the page-side enumeration and falsely permit a
        # reviewed upgrade.
        for row in rows:
            claim = row.get("claim") or {}
            claim_id = str(claim.get("claim_id", ""))
            if claim_id in referenced_anchor_ids:
                continue
            if claim_id in matched_claim_ids:
                continue
            text = normalize_claim(str(claim.get("claim_text", "")))
            verdict = str((row.get("verify") or {}).get("verdict", ""))
            orphan_evidence = None
            orphan_errors: list[str] = []
            if verdict == "CONFIRMED":
                quote_total += 1
                evidence_valid, orphan_evidence, orphan_errors = evidence_check(row, rows_by_id)
                if evidence_valid:
                    quote_ok += 1
            report = {
                "assertion_id": f"FROZEN-{claim_id or 'missing-id'}",
                "section": str(claim.get("section", "")),
                "line_number": None,
                "text": text,
                "text_sha256": sha256_text(text),
                "citations": [],
                "explicit_downgrade": False,
                "claim_id": claim_id or None,
                "verdict": verdict,
                "evidence": orphan_evidence,
                "disposition": "manual_review",
                "reasons": ["frozen_claim_not_mapped_to_current_body", *orphan_errors],
            }
            counts["manual_review"] += 1
            claim_reports.append(report)

        expected_full = prepared.get("full_claim_counts", {}).get(node_id)
        freeze_reasons = []
        if expected_full is not None:
            research_rows = [
                row for row in rows
                if not re.fullmatch(rf"{re.escape(node_id)}-C\d{{3}}", str((row.get("claim") or {}).get("claim_id", "")))
            ]
            content_rows = [row for row in rows if row not in research_rows]
            invalid_content = []
            for row in content_rows:
                claim = row.get("claim") or {}
                kind = claim.get("claim_kind")
                verdict = (row.get("verify") or {}).get("verdict")
                valid_internal = (
                    kind in {"modeling_judgment", "evidence_gap"}
                    and claim.get("believed_source") == "INTERNAL_MODELING_JUDGMENT"
                    and verdict == "NOT_FOUND"
                ) or (
                    kind == "internal_graph_fact"
                    and claim.get("believed_source") == "LCA-CORNERSTONE_GRAPH"
                    and verdict == "NOT_FOUND"
                    and bool(claim.get("evidence_claim_ids"))
                )
                valid_external = (
                    kind == "external_fact"
                    and claim.get("claim_role") == "editorial_assertion"
                    and claim.get("believed_source") == "DERIVED_VERIFIED_CLAIMS"
                    and verdict == "CONFIRMED"
                    and bool(claim.get("evidence_claim_ids"))
                    and evidence_check(row, rows_by_id)[0]
                )
                if not (valid_internal or valid_external):
                    invalid_content.append(row)
            if len(research_rows) != expected_full:
                freeze_reasons.append(f"frozen_research_claim_count_mismatch:{len(research_rows)}!={expected_full}")
            if invalid_content:
                freeze_reasons.append(f"invalid_content_blueprint_claims:{len(invalid_content)}")
        if pilot_only:
            freeze_reasons.append("pilot_only")
        if not upgrade_allowed:
            freeze_reasons.append("reviewed_upgrade_not_allowed")
        # ``reviewed`` must mean more than "all unresolved prose is honestly
        # labelled".  The repository lint contract also requires at least one
        # independently CONFIRMED assertion with a verified inline source.
        if counts["confirmed"] == 0:
            freeze_reasons.append("no_confirmed_claim")
        blocking = (
            counts["safe_degraded"] + counts["missing"] + counts["unresolved"] + counts["contradicted"]
            + counts["manual_review"] + len(hash_reasons) + len(freeze_reasons)
        )
        eligible = bool(assertions) and blocking == 0
        reasons = hash_reasons + freeze_reasons
        for key in ("safe_degraded", "missing", "unresolved", "contradicted", "manual_review"):
            if counts[key]:
                reasons.append(f"{key}:{counts[key]}")
        updates = _frontmatter_updates(eligible)
        if eligible:
            aggregate["pages_eligible"] += 1
        aggregate["pages_total"] += 1
        for key in ("confirmed", "controlled_internal", "safe_degraded", "missing", "unresolved", "contradicted", "manual_review"):
            aggregate[key] += counts[key]
        node_reports.append({
            "node_id": node_id,
            "page": str(page_path.relative_to(repo_root)) if page_path.is_relative_to(repo_root) else str(page_path),
            "file_sha256": file_hash,
            "body_sha256": body_hash,
            "manifest_body_sha256": manifest_hash,
            "content_transaction_sha256": transaction_hash,
            "frontmatter_current": {
                key: fm.get(key, "") for key in (
                    "body_status", "provenance_status", "claim_verification_status"
                )
            },
            "claims": claim_reports,
            # Explicit ledger requested by the protocol: uncited factual prose
            # cannot hide inside an aggregate counter.
            "uncovered": [
                claim for claim in claim_reports if claim["disposition"] == "missing"
            ],
            "counts": {
                "total": len(claim_reports),
                "eligible": counts["confirmed"] + counts["controlled_internal"],
                "confirmed": counts["confirmed"],
                "controlled_internal": counts["controlled_internal"],
                "safe_degraded": counts["safe_degraded"],
                "missing": counts["missing"],
                "unresolved": counts["unresolved"],
                "contradicted": counts["contradicted"],
                "manual_review": counts["manual_review"],
                "hash_drift": 1 if hash_reasons else 0,
            },
            "eligible_for_reviewed": eligible,
            "frontmatter_updates": updates,
            "reasons": reasons,
        })

    total = sum(node["counts"]["total"] for node in node_reports)
    eligible_claims = sum(node["counts"]["eligible"] for node in node_reports)
    artifact: dict[str, Any] = {
        "protocol": {
            "version": PROTOCOL_VERSION,
            "kind": "claim-coverage-plan",
            "pilot_only": pilot_only,
        },
        "industry": manifest.get("industry", ""),
        "batch_id": manifest.get("batch_id", ""),
        "manifest": str(manifest_path),
        "prepared": str(prepared_path),
        "summary": {
            "total": total,
            "eligible": eligible_claims,
            "confirmed": aggregate["confirmed"],
            "controlled_internal": aggregate["controlled_internal"],
            "safe_degraded": aggregate["safe_degraded"],
            "missing": aggregate["missing"],
            "unresolved": aggregate["unresolved"],
            "contradicted": aggregate["contradicted"],
            "manual_review": aggregate["manual_review"],
            "hash_drift": aggregate["hash_drift"],
            "coverage_rate": eligible_claims / total if total else 0.0,
            "quote_compliance_rate": quote_ok / quote_total if quote_total else 1.0,
            "pages_total": aggregate["pages_total"],
            "pages_eligible_for_reviewed": aggregate["pages_eligible"],
        },
        "nodes": node_reports,
    }
    artifact["artifact_sha256"] = artifact_hash(artifact)
    return artifact


def _serialized_evidence_valid(evidence: dict[str, Any]) -> bool:
    """Revalidate either one direct proof or every proof in a derived claim."""
    derived = evidence.get("derived_from")
    if derived is not None:
        items = evidence.get("items") or []
        expected = [str(value) for value in derived]
        actual = [str(item.get("claim_id", "")) for item in items]
        return bool(expected) and actual == expected and all(
            _serialized_evidence_valid(item) for item in items
        )
    url = str(evidence.get("url", ""))
    excerpt = str(evidence.get("fetch_excerpt", ""))
    quote = str(evidence.get("supporting_quote", ""))
    excerpt_hash = sha256_text(excerpt)
    quote_hash = sha256_text(quote)
    return (
        is_external_http_url(url)
        and bool(excerpt) and bool(quote)
        and normalize_text(quote) in normalize_text(excerpt)
        and evidence.get("fetch_excerpt_sha256") == excerpt_hash
        and (
            evidence.get("source_content_sha256") is None
            or bool(re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("source_content_sha256", ""))))
        )
        and evidence.get("supporting_quote_sha256") == quote_hash
        and evidence.get("evidence_sha256") == sha256_text(canonical_json({
            "url": url,
            "excerpt_sha256": excerpt_hash,
            "source_content_sha256": evidence.get("source_content_sha256"),
            "supporting_quote_sha256": quote_hash,
        }))
    )


def validate_artifact(artifact: dict[str, Any]) -> None:
    protocol = artifact.get("protocol") or {}
    if protocol.get("version") != PROTOCOL_VERSION or protocol.get("kind") != "claim-coverage-plan":
        raise CoverageError(f"coverage protocol 必须是 {PROTOCOL_VERSION} claim-coverage-plan")
    if artifact.get("artifact_sha256") != artifact_hash(artifact):
        raise CoverageError("coverage artifact_sha256 不匹配")
    if not isinstance(artifact.get("nodes"), list):
        raise CoverageError("coverage nodes 必须是数组")
    allowed = {"confirmed", "controlled_internal"}
    totals = Counter()
    quote_total = quote_ok = 0
    for node in artifact["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("claims"), list):
            raise CoverageError("coverage node/claims schema 非法")
        dispositions = Counter(str(c.get("disposition", "")) for c in node["claims"])
        expected_uncovered = [c for c in node["claims"] if c.get("disposition") == "missing"]
        if node.get("uncovered") != expected_uncovered:
            raise CoverageError(f"{node.get('node_id')} uncovered 台账非 claims 确定性派生值")
        for claim in node["claims"]:
            disposition = claim.get("disposition")
            if claim.get("verdict") == "CONFIRMED":
                quote_total += 1
            if disposition == "safe_degraded" and not claim.get("explicit_downgrade"):
                raise CoverageError(f"{node.get('node_id')} 安全降级缺显式标记")
            if disposition == "controlled_internal":
                if claim.get("claim_kind") not in {"internal_graph_fact", "modeling_judgment", "evidence_gap"}:
                    raise CoverageError(f"{node.get('node_id')} controlled_internal 缺合法 claim_kind")
                if not (claim.get("explicit_downgrade") or claim.get("editorial_controlled")):
                    raise CoverageError(f"{node.get('node_id')} controlled_internal 缺显式知识类型标记")
            if claim.get("verdict") == "CONFIRMED":
                evidence = claim.get("evidence") or {}
                valid = _serialized_evidence_valid(evidence)
                if disposition == "confirmed" and not valid:
                    raise CoverageError(f"{node.get('node_id')} confirmed 证据证明无效")
                if valid:
                    quote_ok += 1
        counts = node.get("counts") or {}
        expected_counts = {
            "total": len(node["claims"]),
            "eligible": dispositions["confirmed"] + dispositions["controlled_internal"],
            "confirmed": dispositions["confirmed"],
            "controlled_internal": dispositions["controlled_internal"],
            "safe_degraded": dispositions["safe_degraded"],
            "missing": dispositions["missing"],
            "unresolved": dispositions["unresolved"],
            "contradicted": dispositions["contradicted"],
            "manual_review": dispositions["manual_review"],
        }
        if any(counts.get(key) != value for key, value in expected_counts.items()):
            raise CoverageError(f"{node.get('node_id')} coverage counts 非派生值")
        if node.get("eligible_for_reviewed"):
            if not node["claims"] or set(dispositions) - allowed or node.get("reasons"):
                raise CoverageError(f"{node.get('node_id')} eligible_for_reviewed 非法自报")
            if counts.get("hash_drift"):
                raise CoverageError(f"{node.get('node_id')} hash 漂移却声称 eligible")
            if node.get("frontmatter_updates") != _frontmatter_updates(True):
                raise CoverageError(f"{node.get('node_id')} reviewed 升级字段非法")
            totals["pages_eligible"] += 1
        elif node.get("frontmatter_updates"):
            raise CoverageError(f"{node.get('node_id')} 未通过覆盖却包含升级计划")
        totals["pages_total"] += 1
        totals["total"] += expected_counts["total"]
        totals["eligible"] += expected_counts["eligible"]
        totals["confirmed"] += expected_counts["confirmed"]
        totals["controlled_internal"] += expected_counts["controlled_internal"]
        totals["safe_degraded"] += expected_counts["safe_degraded"]
        for key in ("missing", "unresolved", "contradicted", "manual_review"):
            totals[key] += expected_counts[key]
        totals["hash_drift"] += int(bool(counts.get("hash_drift")))
    summary = artifact.get("summary") or {}
    expected_summary = {
        "total": totals["total"],
        "eligible": totals["eligible"],
        "confirmed": totals["confirmed"],
        "controlled_internal": totals["controlled_internal"],
        "safe_degraded": totals["safe_degraded"],
        "missing": totals["missing"],
        "unresolved": totals["unresolved"],
        "contradicted": totals["contradicted"],
        "manual_review": totals["manual_review"],
        "hash_drift": totals["hash_drift"],
        "coverage_rate": totals["eligible"] / totals["total"] if totals["total"] else 0.0,
        "quote_compliance_rate": quote_ok / quote_total if quote_total else 1.0,
        "pages_total": totals["pages_total"],
        "pages_eligible_for_reviewed": totals["pages_eligible"],
    }
    if summary != expected_summary:
        raise CoverageError("coverage summary 非节点账确定性派生值")


def _replace_frontmatter(text: str, updates: dict[str, str]) -> str:
    match = FM_RE.match(text)
    if not match:
        raise CoverageError("Wiki 页面缺少 frontmatter")
    block = match.group(1)
    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}:\s*.*$", re.M)
        if pattern.search(block):
            block = pattern.sub(f"{key}: {value}", block)
        else:
            block += f"\n{key}: {value}"
    return f"---\n{block}\n---\n" + text[match.end():]


def apply_plan(artifact_path: Path, repo_root: Path = ROOT, write: bool = False) -> dict[str, Any]:
    artifact = read_json(artifact_path.resolve())
    validate_artifact(artifact)
    planned: list[tuple[Path, str]] = []
    for node in artifact["nodes"]:
        if not node.get("eligible_for_reviewed"):
            continue
        path = resolve_path(node["page"], repo_root, artifact_path.parent)
        current = path.read_text(encoding="utf-8")
        if sha256_text(current) != node.get("file_sha256"):
            raise CoverageError(f"{node['node_id']} 文件 hash 漂移，拒绝应用")
        if sha256_text(body_of(current)) != node.get("body_sha256"):
            raise CoverageError(f"{node['node_id']} BODY hash 漂移，拒绝应用")
        planned.append((path, _replace_frontmatter(current, node.get("frontmatter_updates", {}))))
    if write:
        staged: list[tuple[Path, Path]] = []
        try:
            for path, content in planned:
                fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                os.close(fd)
                tmp = Path(tmp_name)
                tmp.write_text(content, encoding="utf-8")
                staged.append((path, tmp))
            for path, tmp in staged:
                os.replace(tmp, path)
        finally:
            for _, tmp in staged:
                if tmp.exists():
                    tmp.unlink()
    return {"eligible_pages": len(planned), "written": len(planned) if write else 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("prepared", type=Path)
    plan.add_argument("--repo-root", type=Path, default=ROOT)
    plan.add_argument("--output", type=Path)
    apply = sub.add_parser("apply-plan")
    apply.add_argument("coverage", type=Path)
    apply.add_argument("--repo-root", type=Path, default=ROOT)
    apply.add_argument("--write", action="store_true", help="显式执行 hash-locked frontmatter 写回")
    args = parser.parse_args()
    try:
        if args.command == "plan":
            artifact = plan_coverage(args.prepared, args.repo_root.resolve())
            rendered = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
            print(rendered, end="")
        else:
            report = apply_plan(args.coverage, args.repo_root.resolve(), args.write)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, KeyError, json.JSONDecodeError, CoverageError) as exc:
        print(f"❌ {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
