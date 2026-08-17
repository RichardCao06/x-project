#!/usr/bin/env python3
"""Fail-closed content gate for staged node-Wiki draft merge plans.

The gate evaluates the exact page text that a merge plan would write, before
the repository transaction starts.  It protects content completeness and
Golden-relative quality; it does not grant ``reviewed`` authority.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path
from typing import Any

from merge_wiki_ku import (
    BODY_RE,
    PROTOCOL_VERSION,
    add_changelog,
    force_partial_draft,
    replace_evidence_sections,
    resolve_plan_path,
    sha256,
    update_footnotes,
)
from wiki_claim_coverage import body_of, factual_sentences, frontmatter_of
from wiki_quality_contract import SECTIONS, TABLE_MIN_ROWS, required_external_sections
from wiki_research_ready import table_rows


PROTOCOL = "wiki-draft-content-gate-v1"
CITE_RE = re.compile(r"\[\^([a-z0-9-]+)\](?!:)")
GENERIC_GAP = "该 claim slot 的目标节点特异性外部证据尚未达到 CONFIRMED"
MINIMUMS = {
    "product": {"assertions": 24, "cited": 14, "sources": 4},
    "activity": {"assertions": 24, "cited": 16, "sources": 4},
}


def _candidate_text(item: dict[str, Any], kus_by_id: dict[str, dict[str, Any]], entries: dict) -> tuple[Path, str, str]:
    path = resolve_plan_path(str(item["path"]))
    text = path.read_text(encoding="utf-8")
    if sha256(text) != item.get("file_sha256"):
        raise ValueError(f"{path} 在计划生成后已变化")
    match = BODY_RE.search(text)
    if not match or sha256(match.group(1)) != item.get("body_sha256"):
        raise ValueError(f"{path} BODY 锚点失效")
    original = text
    body = match.group(1)
    applied_kus: list[dict[str, Any]] = []
    if "replacement_body" in item:
        body = str(item["replacement_body"])
        applied_kus = [
            kus_by_id[operation["ku_id"]]
            for operation in item.get("operations", [])
            if operation.get("ku_id") in kus_by_id
        ]
    else:
        for operation in item.get("operations", []):
            old = str(operation.get("old_text", ""))
            if body.count(old) != 1:
                raise ValueError(f"{path}: {operation.get('ku_id')} 原文不再唯一命中")
            body = body.replace(old, str(operation.get("new_text", "")), 1)
            if operation.get("ku_id") in kus_by_id:
                applied_kus.append(kus_by_id[operation["ku_id"]])
        body = update_footnotes(body, applied_kus, entries)
    candidate = text[:match.start(1)] + body + text[match.end(1):]
    if item.get("replacement_evidence"):
        candidate = replace_evidence_sections(candidate, str(item["replacement_evidence"]))
    if item.get("operations"):
        candidate = add_changelog(candidate, str(item.get("changelog", "")))
        candidate = force_partial_draft(candidate)
    return path, original, candidate


def _section_text(body: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)", body
    )
    return match.group(1) if match else ""


def _metrics(text: str, node_type: str) -> dict[str, Any]:
    body = body_of(text)
    assertions = factual_sentences(body)
    cited = [item for item in assertions if item.get("citations")]
    refs = set(CITE_RE.findall(body))
    external_refs = refs - {"internal-graph", "internal-review"}
    return {
        "body_chars": len(body),
        "assertions": len(assertions),
        "cited_assertions": len(cited),
        "distinct_external_sources": len(external_refs),
        "generic_gap_count": body.count(GENERIC_GAP),
        "model_recall_count": body.count("未核实·模型回忆"),
        "evidence_gap_count": body.count("〔证据缺口〕"),
        "modeling_judgment_count": body.count("〔建模判断〕"),
        "table_rows": {
            kind: table_rows(text, kind) for kind in TABLE_MIN_ROWS[node_type]
        },
        "inline_refs": sorted(refs),
    }


def _content_blueprint(node_id: str) -> dict[str, Any] | None:
    roots = (
        Path(__file__).resolve().parents[1] / "content-blueprints",
        Path(__file__).resolve().parents[1] / "fixtures" / "wiki-phase2" / "content-blueprints",
    )
    for root in roots:
        path = root / f"{node_id}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def _blueprint_checks(candidate: str, body: str, blueprint: dict[str, Any]) -> tuple[dict[str, bool], dict[str, Any]]:
    paragraphs_by_section: dict[str, int] = {}
    paragraph_sentence_counts: list[int] = []
    for heading, contract in blueprint["sections"].items():
        section = _section_text(body, heading)
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", section) if part.strip()]
        paragraphs_by_section[heading] = len(paragraphs)
        paragraph_sentence_counts.extend(len(factual_sentences(part)) for part in paragraphs)
    paragraph_count = sum(paragraphs_by_section.values())
    single_count = sum(count <= 1 for count in paragraph_sentence_counts)
    ratio = single_count / paragraph_count if paragraph_count else 1.0
    target = blueprint["golden_target"]
    maximum_sentences = int(target.get("maximum_sentences_per_paragraph", 4))
    paragraph_shape_ok = bool(paragraph_sentence_counts) and all(
        2 <= count <= maximum_sentences for count in paragraph_sentence_counts
    )
    assertion_texts = [
        re.sub(r"[\W_]+", "", row["text"])
        for row in factual_sentences(body)
        if len(re.sub(r"[\W_]+", "", row["text"])) >= 12
    ]
    worst_duplicate = 0.0
    for index, left in enumerate(assertion_texts):
        for right in assertion_texts[index + 1:]:
            worst_duplicate = max(
                worst_duplicate,
                difflib.SequenceMatcher(None, left, right).ratio(),
            )
    duplicate_limit = float(target.get("maximum_near_duplicate_ratio", 1.0))
    assertions = len(factual_sentences(body))
    modeling = body.count("〔建模判断〕")
    table_labels = [
        label for labels in blueprint.get("evidence_tables", {}).values() for label in labels
    ]
    checks = {
        "content_blueprint_assertion_depth": assertions >= int(target["minimum_assertions"]),
        "content_blueprint_assertion_not_stuffed": assertions <= int(target.get("maximum_assertions", 10**9)),
        "content_blueprint_paragraph_depth": paragraph_count >= int(target["minimum_paragraphs"]),
        # The old gate equated "not a single-sentence paragraph" with
        # coherence.  Coherence now also requires bounded paragraph shape and
        # rejects near-duplicate claim-ledger prose.
        "content_blueprint_modeling_depth": (
            modeling >= int(target["minimum_modeling_judgments"])
            if "〔建模判断〕" in body else assertions >= int(target["minimum_assertions"])
        ),
        "content_blueprint_modeling_not_stuffed": modeling <= int(target.get("maximum_modeling_judgments", 10**9)),
        "content_blueprint_paragraph_coherence": (
            ratio <= float(target["maximum_single_sentence_paragraph_ratio"])
            and paragraph_shape_ok
            and worst_duplicate <= duplicate_limit
        ),
        "content_blueprint_section_depth": all(
            paragraphs_by_section.get(heading, 0) >= int(contract["minimum_paragraphs"])
            for heading, contract in blueprint["sections"].items()
        ),
        "content_blueprint_required_topics": all(token in body for token in blueprint.get("required_tokens", [])),
        "content_blueprint_no_false_gap": not any(phrase in body for phrase in blueprint.get("forbidden_phrases", [])),
        "content_blueprint_node_specific_tables": all(label in candidate for label in table_labels),
    }
    return checks, {"paragraphs": paragraph_count, "paragraphs_by_section": paragraphs_by_section,
                    "single_sentence_paragraphs": single_count, "single_sentence_paragraph_ratio": ratio,
                    "paragraph_sentence_counts": paragraph_sentence_counts,
                    "maximum_near_duplicate_ratio": round(worst_duplicate, 4),
                    "required_table_labels": len(table_labels)}


def gate_page(
    path: Path,
    original: str,
    candidate: str,
    available_sources: set[str],
    *,
    write_mode: str | None = None,
) -> dict[str, Any]:
    fm = frontmatter_of(candidate)
    node_type = fm.get("node_type") or ("activity" if fm.get("id", "").startswith("A") else "product")
    if node_type not in SECTIONS:
        raise ValueError(f"{path} node_type 非法: {node_type}")
    body = body_of(candidate)
    headings = re.findall(r"(?m)^##\s+(.+?)\s*$", body)
    metrics = _metrics(candidate, node_type)
    thresholds = MINIMUMS[node_type]
    core_sections = required_external_sections(node_type)
    core_cited = {
        section: len([
            row for row in factual_sentences(_section_text(body, section))
            if row.get("citations")
            and set(row["citations"]) - {"internal-graph", "internal-review"}
        ])
        for section in sorted(core_sections)
    }
    unresolved_refs = sorted(set(metrics["inline_refs"]) - available_sources)
    checks = {
        "wiki_v2_ten_sections": headings == SECTIONS[node_type],
        "draft_candidate_state": (
            fm.get("schema_version") == "wiki-v2"
            and fm.get("body_status") == "draft"
            and fm.get("content_maturity") == "candidate"
        ),
        "assertions_rich_enough": metrics["assertions"] >= thresholds["assertions"],
        "cited_content_rich_enough": metrics["cited_assertions"] >= thresholds["cited"],
        "source_diversity": metrics["distinct_external_sources"] >= thresholds["sources"],
        # A draft needs one factual anchor in each core evidence zone, not a
        # verified citation for every explanatory claim.  Clearly labeled
        # modeling judgments carry the remaining interpretation and content
        # completeness; stricter coverage belongs to reviewed/publish gates.
        "core_sections_source_grounded": all(value >= 1 for value in core_cited.values()),
        "no_generic_gap_shell": metrics["generic_gap_count"] == 0,
        # 〔建模判断〕 is first-class draft content.  Reject only the legacy
        # unclassified model-recall label, which does not distinguish an
        # external fact from an LCA judgment.
        "no_unclassified_model_assertion": metrics["model_recall_count"] == 0,
        "citations_resolve": not unresolved_refs,
        "evidence_tables_complete": all(
            metrics["table_rows"][kind] >= minimum
            for kind, minimum in TABLE_MIN_ROWS[node_type].items()
        ),
    }
    blueprint = _content_blueprint(str(fm.get("id") or ""))
    blueprint_metrics: dict[str, Any] = {}
    if blueprint:
        content_checks, blueprint_metrics = _blueprint_checks(candidate, body, blueprint)
        checks.update(content_checks)

    old_fm = frontmatter_of(original)
    old_body = body_of(original)
    old_is_rich = (
        old_fm.get("schema_version") == "wiki-v2"
        and "未核实·模型回忆" not in old_body
        and GENERIC_GAP not in old_body
    )
    if old_is_rich and write_mode != "rebuild":
        previous = _metrics(original, node_type)
        checks["repair_non_degradation"] = all((
            metrics["assertions"] >= previous["assertions"],
            metrics["cited_assertions"] >= previous["cited_assertions"],
            all(
                metrics["table_rows"][kind] >= previous["table_rows"].get(kind, 0)
                for kind in TABLE_MIN_ROWS[node_type]
            ),
        ))
    else:
        checks["repair_non_degradation"] = True

    return {
        "node_id": fm.get("id"),
        "page": str(path),
        "go": all(checks.values()),
        "checks": checks,
        "metrics": {**metrics, "core_cited_assertions": core_cited,
                    "content_blueprint": blueprint_metrics},
        "unresolved_refs": unresolved_refs,
    }


def gate_merge_plan(plan_path: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    protocol = plan.get("protocol") or {}
    if protocol.get("version") != PROTOCOL_VERSION or protocol.get("kind") != "wiki-ku-merge-plan":
        raise ValueError("不是受支持的 wiki-ku 合并计划")
    ku_path = resolve_plan_path(str(plan["ku_path"]))
    if sha256(ku_path.read_text(encoding="utf-8")) != plan.get("ku_sha256"):
        raise ValueError("KU 文件在计划生成后已变化")
    kus_by_id = {
        item["ku_id"]: item
        for item in json.loads(ku_path.read_text(encoding="utf-8")).get("kus", [])
    }
    registry_path = resolve_plan_path(str(plan["registry_path"]))
    registry_text = registry_path.read_text(encoding="utf-8")
    if sha256(registry_text) != plan.get("registry_sha256"):
        raise ValueError("registry 在计划生成后已变化")
    registry = json.loads(registry_text).get("sources") or {}
    available_sources = set(registry) | set(plan.get("registry_entries") or {})
    pages = []
    for item in plan.get("files", []):
        if not item.get("operations"):
            continue
        path, original, candidate = _candidate_text(
            item, kus_by_id, plan.get("registry_entries") or {}
        )
        pages.append(gate_page(
            path, original, candidate, available_sources,
            write_mode=str(item.get("write_mode") or "") or None,
        ))
    report = {
        "protocol": {"version": PROTOCOL, "kind": "draft-content-gate"},
        "plan": str(plan_path.resolve()),
        "go": bool(pages) and all(page["go"] for page in pages),
        "disposition": "ready_for_content_apply" if pages and all(page["go"] for page in pages) else "blocked_before_content_apply",
        "pages": pages,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = gate_merge_plan(args.plan.resolve())
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["go"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
