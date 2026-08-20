"""Hash-bound paragraph repair with deterministic non-regression checks."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
import hashlib
import json
import re
from typing import Any


class EditorialPatchError(ValueError):
    pass


LEGACY_CLAIM_NORMALIZER_REVISION = "fact-first-v1"
MAXIMUM_CLAIM_USE_COUNT = 3


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def paragraph_manifest(document: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for section in document.get("sections", []):
        section_id = str(section.get("section_id") or "")
        if not section_id:
            raise EditorialPatchError("every section requires section_id")
        for paragraph in section.get("paragraphs", []):
            paragraph_id = str(paragraph.get("paragraph_id") or "")
            key = f"{section_id}.{paragraph_id}"
            if not paragraph_id or key in result:
                raise EditorialPatchError("paragraph IDs must be non-empty and unique per section")
            result[key] = canonical_hash(paragraph)
    return result


def legacy_paragraph_manifest(document: dict[str, Any]) -> dict[str, str]:
    """Hash v2 paragraphs by deterministic heading and one-based position."""
    result: dict[str, str] = {}
    for section in document.get("sections", []):
        heading = str(section.get("heading") or "")
        if not heading or heading in result:
            raise EditorialPatchError("v2 draft section headings must be non-empty and unique")
        for index, paragraph in enumerate(section.get("paragraphs", []), 1):
            result[f"{heading}.p{index}"] = canonical_hash(paragraph)
    return result


_SPLIT_INSTRUCTION = re.compile(r"(?:拆分|拆成|分成|分别成段|独立成段|split)", re.IGNORECASE)
_DELETE_INSTRUCTION = re.compile(
    r"(?:(?:删除|移除|去掉|剔除)\s*(?:整段|该段|本段|此段|这个段落|整个段落)"
    r"|(?:整段|该段|本段|此段|这个段落|整个段落)\s*(?:删除|移除|去掉|剔除)"
    r"|delete\s+(?:the\s+)?(?:whole\s+)?paragraph)",
    re.IGNORECASE,
)
_IDENTIFIER_PATTERN = r"(?<![A-Za-z0-9])(?:A|P)\d{3}(?!\d)"
_IDENTITY_TOKEN = re.compile(
    rf"{_IDENTIFIER_PATTERN}\s+"
    rf"(?:(?![、，；。\n\"“”'‘’]|(?:和|与|及)?{_IDENTIFIER_PATTERN}\s|(?:和|与|及)全部).)+?"
    rf"(?=$|[、，；。\n\"“”'‘’]|(?:和|与|及)?{_IDENTIFIER_PATTERN}\s|(?:和|与|及)全部)"
)
_QUOTED_TOKEN = re.compile(r"[\"“'‘]([^\"”'’]+)[\"”'’]")
_CORRECTION = re.compile(
    rf"(?:将|把)?\s*(?P<old>{_IDENTIFIER_PATTERN})\s*"
    rf"(?:更正|修正|纠正|改正|替换|改)(?:为|成)\s*"
    rf"(?P<new>{_IDENTIFIER_PATTERN})"
)
_REMOVE_IDENTIFIER = re.compile(
    rf"(?:删除|移除|去掉|剔除|不保留|不得保留)"
    rf"\s*(?:错误的|误写的|原有的)?\s*(?P<identifier>{_IDENTIFIER_PATTERN})"
)
_MOVE_IDENTIFIER = re.compile(
    rf"(?:将|把)?\s*(?P<identifier>{_IDENTIFIER_PATTERN})"
    rf"(?:(?!{_IDENTIFIER_PATTERN})[^；;。\n]){{0,40}}(?:移入|移至|并入)",
    re.IGNORECASE,
)
_MERGE_INTO_PARAGRAPH = re.compile(
    r"(?:与第(?P<with>\d+)段合并|并入第(?P<into>\d+)段)", re.IGNORECASE,
)
_ONLY_RETAIN_CLAUSE = re.compile(
    r"(?:本段|该段|此段)?\s*仅保留(?P<body>[^；;。\n]+)", re.IGNORECASE,
)
_CURRENT_PARAGRAPH_SCOPE = re.compile(
    r"(?:并使)?(?:本段|该段|此段)(?:连续表达|只(?:引用|保留)|仅(?:引用|保留))"
    r"[:：]?(?P<body>[^；;。\n]+)",
    re.IGNORECASE,
)
_RELOCATE_LIST_INSTRUCTION = re.compile(
    r"(?:(?:把|将)[^；;。\n]{0,60}(?:清单|完整流名称)[^；;。\n]{0,30}(?:移至|留在)"
    r"|(?:清单|完整流名称)[^；;。\n]{0,30}(?:移至|留在)[^；;。\n]{0,60}"
    r"(?:不在|不得))",
    re.IGNORECASE,
)


def _paragraph_text(paragraph: dict[str, Any]) -> str:
    return "\n".join(
        [str(paragraph.get("focus") or "")]
        + [str(sentence.get("text") or "") for sentence in paragraph.get("sentences") or []]
    )


def _superseded_identifiers(instructions: str) -> set[str]:
    """Return identifiers that an explicit correction/removal permits replacing."""
    identifiers = {match.group("old") for match in _CORRECTION.finditer(instructions)}
    identifiers.update(
        match.group("identifier") for match in _REMOVE_IDENTIFIER.finditer(instructions)
    )
    identifiers.update(
        match.group("identifier") for match in _MOVE_IDENTIFIER.finditer(instructions)
    )
    return identifiers


def _identity_tokens(text: str) -> list[str]:
    """Extract one graph identity per token without binding prose punctuation."""
    return list(dict.fromkeys(
        match.group(0).strip().rstrip("和与及")
        for match in _IDENTITY_TOKEN.finditer(text)
        if match.group(0).strip().rstrip("和与及")
    ))


def _quoted_identity_overrides(instructions: str) -> dict[str, list[str]]:
    """Return explicitly quoted graph labels that supersede legacy shorthands.

    Review prose frequently asks for a canonical full label and quotes it.  A
    quoted phrase is an override only when the whole phrase is exactly one
    identity; quoted lists and explanatory clauses remain instructions rather
    than literal preservation tokens.
    """
    result: dict[str, list[str]] = {}
    for value in _QUOTED_TOKEN.findall(instructions):
        candidate = value.strip()
        tokens = _identity_tokens(candidate)
        if len(tokens) != 1 or tokens[0] != candidate:
            continue
        identifier = re.search(_IDENTIFIER_PATTERN, candidate)
        if identifier:
            result.setdefault(identifier.group(0), []).append(candidate)
    return result


def _legacy_tokens_to_preserve(paragraph: dict[str, Any], instructions: str) -> list[str]:
    """Preserve atomic graph identities while allowing instructed normalization."""
    paragraph_text = _paragraph_text(paragraph)
    source = paragraph_text + "\n" + instructions
    superseded = _superseded_identifiers(instructions)
    overrides = _quoted_identity_overrides(instructions)
    scoped_retain = (
        _ONLY_RETAIN_CLAUSE.search(instructions)
        or _CURRENT_PARAGRAPH_SCOPE.search(instructions)
    )
    if scoped_retain:
        retained_identifiers = set(re.findall(
            _IDENTIFIER_PATTERN, scoped_retain.group("body")
        )) - superseded
        if retained_identifiers:
            tokens = [
                token for token in _identity_tokens(paragraph_text)
                if set(re.findall(_IDENTIFIER_PATTERN, token)) <= retained_identifiers
            ]
            tokens.extend(
                value
                for identifier, values in overrides.items()
                if identifier in retained_identifiers
                for value in values
            )
            tokens.extend(sorted(retained_identifiers))
            return list(dict.fromkeys(token for token in tokens if token))
    if _RELOCATE_LIST_INSTRUCTION.search(instructions):
        return []
    tokens = []
    for token in _identity_tokens(paragraph_text):
        identifiers = re.findall(_IDENTIFIER_PATTERN, token)
        if superseded.intersection(identifiers) or any(item in overrides for item in identifiers):
            continue
        tokens.append(token)
    for identifier, values in overrides.items():
        if identifier not in superseded:
            tokens.extend(values)
    # Preserve the identifier independently as a fail-closed floor if the
    # review did not quote the full graph-local label.  Instruction prose can
    # introduce the replacement ID but never a new mandatory prose fragment.
    tokens.extend(
        identifier for identifier in re.findall(_IDENTIFIER_PATTERN, source)
        if identifier not in superseded
    )
    return list(dict.fromkeys(token for token in tokens if token))


def _repair_paragraphs(repair: dict[str, Any], operation: str) -> list[dict[str, Any]]:
    """Read the v1 split form, retaining compatibility with old replace artifacts."""
    raw = repair.get("replacements")
    if raw is None and repair.get("replacement") is not None:
        raw = [repair.get("replacement")]
    if operation == "delete":
        if raw is None:
            raw = []
        if raw != []:
            raise EditorialPatchError("delete operation requires zero replacement paragraphs")
        return []
    if not isinstance(raw, list) or not raw or not all(isinstance(item, dict) for item in raw):
        raise EditorialPatchError("repair requires one or more replacement paragraphs")
    if operation == "replace" and len(raw) != 1:
        raise EditorialPatchError("replace operation requires exactly one paragraph")
    if operation == "split_replace" and len(raw) < 2:
        raise EditorialPatchError("split_replace operation requires at least two paragraphs")
    if operation not in {"replace", "split_replace"}:
        raise EditorialPatchError(f"unsupported editorial operation: {operation}")
    return deepcopy(raw)


def _split_ids(paragraph_id: str, count: int) -> list[str]:
    """Keep the target ID and derive stable IDs solely from it and split ordinal."""
    if count == 0:
        return []
    return [paragraph_id, *(f"{paragraph_id}.split{ordinal}" for ordinal in range(2, count + 1))]


def prepare_legacy_patch_review(document: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    """Bind a v1 editorial review to immutable v2 paragraph hashes.

    Multiple observations for the same paragraph are deliberately coalesced so
    a repair can change that paragraph exactly once.
    """
    if document.get("protocol") != "wiki-content-draft-v2":
        raise EditorialPatchError("legacy patch preparation requires wiki-content-draft-v2")
    if review.get("protocol") != "wiki-editorial-review-v1" or review.get("verdict") != "NO_GO":
        raise EditorialPatchError("a NO_GO wiki-editorial-review-v1 is required")
    manifest = legacy_paragraph_manifest(document)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in review.get("issues", []):
        heading = str(issue.get("section") or "")
        paragraph_index = int(issue.get("paragraph_index") or 0)
        key = f"{heading}.p{paragraph_index}"
        if key not in manifest:
            raise EditorialPatchError(f"editorial issue targets unknown paragraph: {key}")
        grouped.setdefault(key, []).append(issue)
    if not grouped:
        raise EditorialPatchError("NO_GO review requires at least one targetable issue")
    issues = []
    for ordinal, (key, observations) in enumerate(grouped.items(), 1):
        heading, paragraph_id = key.rsplit(".", 1)
        section = next(row for row in document["sections"] if row.get("heading") == heading)
        paragraph = section["paragraphs"][int(paragraph_id.removeprefix("p")) - 1]
        instruction = "\n".join(str(row.get("repair_instruction") or "").strip()
                                  for row in observations)
        operation = (
            "delete" if _DELETE_INSTRUCTION.search(instruction)
            else "split_replace" if _SPLIT_INSTRUCTION.search(instruction)
            else "replace"
        )
        merge_target = _MERGE_INTO_PARAGRAPH.search(instruction)
        if merge_target:
            target_index = int(merge_target.group("with") or merge_target.group("into"))
            if target_index < int(paragraph_id.removeprefix("p")) and (
                f"{heading}.p{target_index}" in manifest
            ):
                operation = "delete"
        issues.append({
            "issue_id": f"E{ordinal:03d}",
            "section_id": heading,
            "paragraph_id": paragraph_id,
            "target_hash": manifest[key],
            "type": "+".join(dict.fromkeys(str(row.get("issue_type") or "other")
                                              for row in observations)),
            "operation": operation,
            "instruction": instruction,
            "explanations": [str(row.get("explanation") or "") for row in observations],
            "facts_must_preserve": [],
            "tokens_must_preserve": (
                [] if operation == "delete"
                else _legacy_tokens_to_preserve(paragraph, instruction)
            ),
        })
    return {
        "protocol": "wiki-editorial-patch-review-v1",
        "node_id": document.get("node_id"),
        "verdict": "NO_GO",
        "issues": issues,
    }


def apply_legacy_repairs(document: dict[str, Any], patch_review: dict[str, Any],
                         repairs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply hash-bound replacements/splits to a v2 draft without touching peers."""
    if document.get("protocol") != "wiki-content-draft-v2":
        raise EditorialPatchError("legacy paragraph patch requires wiki-content-draft-v2")
    if patch_review.get("protocol") != "wiki-editorial-patch-review-v1":
        raise EditorialPatchError("hash-bound patch review is required")
    issues = {str(item.get("issue_id")): item for item in patch_review.get("issues", [])}
    supplied = {str(item.get("issue_id")): item for item in repairs}
    if not issues or set(supplied) != set(issues):
        raise EditorialPatchError("repairs must match patch review issues exactly")
    before = deepcopy(document)
    before_manifest = legacy_paragraph_manifest(before)
    result = deepcopy(document)
    touched: set[str] = set()
    operations: list[tuple[str, int, str, list[dict[str, Any]], list[str]]] = []
    for issue_id, issue in issues.items():
        repair = supplied[issue_id]
        heading = str(issue.get("section_id") or "")
        paragraph_id = str(issue.get("paragraph_id") or "")
        key = f"{heading}.{paragraph_id}"
        if (repair.get("section_id") != heading or repair.get("paragraph_id") != paragraph_id
                or repair.get("target_hash") != issue.get("target_hash")
                or before_manifest.get(key) != issue.get("target_hash")):
            raise EditorialPatchError(f"target hash conflict: {key}")
        replacements = _repair_paragraphs(repair, str(issue.get("operation") or "replace"))
        if any(not paragraph.get("focus") or not paragraph.get("sentences")
               for paragraph in replacements):
            raise EditorialPatchError(f"replacement paragraph is incomplete: {issue_id}")
        replacement_text = "\n".join(_paragraph_text(item) for item in replacements)
        missing_tokens = [str(token) for token in issue.get("tokens_must_preserve") or []
                          if str(token) not in replacement_text]
        if missing_tokens:
            raise EditorialPatchError(
                f"required paragraph-local tokens were not preserved: {issue_id}: {missing_tokens}"
            )
        section = next((row for row in result["sections"] if row.get("heading") == heading), None)
        try:
            index = int(paragraph_id.removeprefix("p")) - 1
        except ValueError as exc:
            raise EditorialPatchError(f"invalid paragraph id: {paragraph_id}") from exc
        if section is None or index < 0 or index >= len(section.get("paragraphs", [])) or key in touched:
            raise EditorialPatchError(f"unknown or duplicate repair target: {key}")
        split_ids = _split_ids(paragraph_id, len(replacements))
        operations.append((heading, index, key, replacements, split_ids))
        touched.add(key)
    # Descending positional application prevents an earlier split from moving
    # a later hash-bound target. Equal-section order is therefore deterministic.
    for heading, index, _key, replacements, _split_ids_for_target in sorted(
        operations, key=lambda item: (item[0], item[1]), reverse=True
    ):
        section = next(row for row in result["sections"] if row.get("heading") == heading)
        section["paragraphs"][index:index + 1] = replacements
    after_untargeted: dict[str, str] = {}
    for section in before["sections"]:
        heading = str(section["heading"])
        result_section = next(row for row in result["sections"] if row.get("heading") == heading)
        cursor = 0
        for index, paragraph in enumerate(section.get("paragraphs") or [], 1):
            key = f"{heading}.p{index}"
            if key in touched:
                operation = next(item for item in operations if item[2] == key)
                cursor += len(operation[3])
                continue
            if cursor >= len(result_section.get("paragraphs") or []):
                raise EditorialPatchError("repair removed an untargeted paragraph")
            after_untargeted[key] = canonical_hash(result_section["paragraphs"][cursor])
            cursor += 1
    unchanged = {key: digest for key, digest in before_manifest.items() if key not in touched}
    if after_untargeted != unchanged:
        raise EditorialPatchError("repair changed an untargeted paragraph")
    changes = []
    for _heading, _index, key, replacements, split_ids in sorted(operations, key=lambda item: item[2]):
        changes.append({
            "paragraph": key,
            "before": before_manifest[key],
            "after": [
                {"paragraph_id": split_id, "sha256": canonical_hash(paragraph)}
                for split_id, paragraph in zip(split_ids, replacements, strict=True)
            ],
        })
    return result, {
        "protocol": "wiki-editorial-patch-receipt-v1",
        "before_hash": canonical_hash(before),
        "after_hash": canonical_hash(result),
        "targeted_paragraphs": sorted(touched),
        "unchanged_paragraphs": unchanged,
        "paragraph_changes": changes,
        "requires_independent_rereview": True,
    }


def normalize_legacy_repair_claim_bindings(
    repairs: list[dict[str, Any]], rows: list[dict[str, Any]],
    document: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize replacement bindings while reserving claim capacity for facts."""
    result = deepcopy(repairs)
    kinds = {
        str((row.get("claim") or {}).get("claim_id")): str(
            (row.get("claim") or {}).get("claim_kind") or ""
        )
        for row in rows
        if (row.get("claim") or {}).get("claim_id")
    }
    verdicts = {
        str((row.get("claim") or {}).get("claim_id")): str(
            (row.get("verify") or {}).get("verdict") or ""
        )
        for row in rows
        if (row.get("claim") or {}).get("claim_id")
    }
    confirmed_external = {
        claim_id for claim_id, kind in kinds.items()
        if kind == "external_fact" and verdicts.get(claim_id) == "CONFIRMED"
    }
    targets = {
        (str(repair.get("section_id") or ""), str(repair.get("paragraph_id") or ""))
        for repair in result
    }
    counts: Counter[str] = Counter()
    if document is not None:
        counts.update(claim_uses_outside_targets(document, targets, kinds))
    sentences: list[tuple[dict[str, Any], list[str], bool]] = []
    for repair in result:
        replacements = repair.get("replacements")
        if replacements is None:
            replacements = [repair.get("replacement") or {}]
        for replacement in replacements:
            for sentence in replacement.get("sentences") or []:
                sentence_kind = str(sentence.get("claim_kind") or "")
                ids = [str(item) for item in sentence.get("evidence_claim_ids") or []
                       if str(item) in kinds]
                if sentence_kind == "internal_graph_fact":
                    ids = [claim_id for claim_id in ids if kinds[claim_id] == sentence_kind]
                elif sentence_kind == "external_fact":
                    ids = [claim_id for claim_id in ids if claim_id in confirmed_external]
                sentences.append((
                    sentence,
                    list(dict.fromkeys(ids)),
                    sentence_kind in {"internal_graph_fact", "external_fact"},
                ))

    # Reserve one available same-kind binding for every fact sentence before
    # optional citations can consume the remaining maximum-three capacity. A
    # capacitated augmenting match avoids a greedy choice making a feasible
    # set of fact bindings appear impossible.
    fact_rows = [(sentence, ids) for sentence, ids, required in sentences if required]
    capacities = {
        claim_id: max(0, MAXIMUM_CLAIM_USE_COUNT - counts[claim_id])
        for _sentence, ids in fact_rows
        for claim_id in ids
    }
    owners: dict[str, list[int]] = {}
    reservations: dict[int, str] = {}

    def reserve(index: int, seen_sentences: set[int], seen_claims: set[str]) -> bool:
        if index in seen_sentences:
            return False
        seen_sentences.add(index)
        for claim_id in fact_rows[index][1]:
            if claim_id in seen_claims:
                continue
            seen_claims.add(claim_id)
            claim_owners = owners.setdefault(claim_id, [])
            if len(claim_owners) < capacities.get(claim_id, 0):
                claim_owners.append(index)
                reservations[index] = claim_id
                return True
            for position, owner in enumerate(tuple(claim_owners)):
                if reserve(owner, seen_sentences, seen_claims):
                    claim_owners[position] = index
                    reservations[index] = claim_id
                    return True
        return False

    for index, (sentence, _ids) in enumerate(fact_rows):
        if not reserve(index, set(), set()):
            kind = str(sentence.get("claim_kind") or "")
            text = str(sentence.get("text") or "")
            raise EditorialPatchError(
                f"required fact binding has no available claim-use capacity: {kind}: {text}"
            )
    for claim_id in reservations.values():
        counts[claim_id] += 1

    for index, (sentence, ids) in enumerate(fact_rows):
        reserved = reservations[index]
        kept = []
        for claim_id in ids:
            if claim_id == reserved:
                kept.append(claim_id)
            elif counts[claim_id] < MAXIMUM_CLAIM_USE_COUNT:
                kept.append(claim_id)
                counts[claim_id] += 1
        sentence["evidence_claim_ids"] = kept

    for sentence, ids, required in sentences:
        if required:
            continue
        kept = []
        for claim_id in ids:
            if counts[claim_id] < MAXIMUM_CLAIM_USE_COUNT:
                kept.append(claim_id)
                counts[claim_id] += 1
        sentence["evidence_claim_ids"] = kept

    for repair in result:
        replacements = repair.get("replacements")
        if replacements is None:
            replacements = [repair.get("replacement") or {}]
        repair["preserved_claim_ids"] = list(dict.fromkeys(
            claim_id
            for replacement in replacements
            for sentence in replacement.get("sentences") or []
            for claim_id in sentence.get("evidence_claim_ids") or []
        ))
    return result


def claim_uses_outside_targets(
    document: dict[str, Any],
    targeted_paragraphs: set[tuple[str, str]],
    claim_ids: Iterable[object] = (),
) -> dict[str, int]:
    """Count every claim use outside the hash-bound target paragraphs."""
    counts: Counter[str] = Counter()
    for section in document.get("sections") or []:
        heading = str(section.get("heading") or "")
        for index, paragraph in enumerate(section.get("paragraphs") or [], 1):
            if (heading, f"p{index}") in targeted_paragraphs:
                continue
            for sentence in paragraph.get("sentences") or []:
                counts.update(
                    str(item) for item in sentence.get("evidence_claim_ids") or []
                )
    all_claim_ids = {str(claim_id) for claim_id in claim_ids if str(claim_id)}
    all_claim_ids.update(counts)
    return {claim_id: counts[claim_id] for claim_id in sorted(all_claim_ids)}


def claim_remaining_uses(
    document: dict[str, Any],
    targeted_paragraphs: set[tuple[str, str]],
    claim_ids: Iterable[object] = (),
) -> dict[str, int]:
    """Return every claim's replacement budget after untargeted uses."""
    counts = claim_uses_outside_targets(document, targeted_paragraphs, claim_ids)
    return {
        claim_id: max(0, MAXIMUM_CLAIM_USE_COUNT - count)
        for claim_id, count in counts.items()
    }


def claim_binding_metrics(document: dict[str, Any]) -> dict[str, Any]:
    """Return receipt metrics for provenance completeness and the claim-use cap."""
    claim_use_counts: Counter[str] = Counter(
        str(claim_id)
        for section in document.get("sections") or []
        for paragraph in section.get("paragraphs") or []
        for sentence in paragraph.get("sentences") or []
        for claim_id in sentence.get("evidence_claim_ids") or []
    )
    internal_graph_fact_sentences = [
        sentence
        for section in document.get("sections") or []
        for paragraph in section.get("paragraphs") or []
        for sentence in paragraph.get("sentences") or []
        if sentence.get("claim_kind") == "internal_graph_fact"
    ]
    bound_internal_graph_facts: Counter[str] = Counter()
    for sentence in internal_graph_fact_sentences:
        bound_internal_graph_facts.update(list(dict.fromkeys(
            str(claim_id) for claim_id in sentence.get("evidence_claim_ids") or []
        )))
    return {
        "internal_graph_fact_sentences_without_evidence": sum(
            not (sentence.get("evidence_claim_ids") or [])
            for sentence in internal_graph_fact_sentences
        ),
        "maximum_claim_use_count": max(claim_use_counts.values(), default=0),
        "bound_internal_graph_fact_sentences_by_claim_id": dict(
            sorted(bound_internal_graph_facts.items())
        ),
    }


def _claim_ids(document: dict[str, Any]) -> set[str]:
    return {
        str(claim_id)
        for section in document.get("sections", [])
        for paragraph in section.get("paragraphs", [])
        for sentence in paragraph.get("sentences", [])
        for claim_id in sentence.get("evidence_claim_ids", [])
    }


def _body_chars(document: dict[str, Any]) -> int:
    return sum(
        len(str(sentence.get("text") or ""))
        for section in document.get("sections", [])
        for paragraph in section.get("paragraphs", [])
        for sentence in paragraph.get("sentences", [])
    )


def validate_draft(document: dict[str, Any], blueprint: dict[str, Any]) -> None:
    if document.get("protocol") != "wiki-content-draft-v3":
        raise EditorialPatchError("draft must use wiki-content-draft-v3")
    expected = list(blueprint.get("section_order") or blueprint.get("sections") or [])
    actual = [str(item.get("section_id") or "") for item in document.get("sections", [])]
    if len(expected) != 9 or actual != expected or len(set(actual)) != 9:
        raise EditorialPatchError("draft must contain the nine blueprint section IDs in order")
    allowed_kinds = {"external_fact", "internal_graph_fact", "modeling_judgment", "evidence_gap"}
    paragraph_manifest(document)
    for section in document["sections"]:
        if "heading" in section:
            raise EditorialPatchError("model draft may not supply headings")
        for paragraph in section.get("paragraphs", []):
            if not paragraph.get("focus") or not paragraph.get("sentences"):
                raise EditorialPatchError("paragraph requires focus and sentences")
            for sentence in paragraph["sentences"]:
                if sentence.get("claim_kind") not in allowed_kinds:
                    raise EditorialPatchError("invalid claim_kind")


def render_sections(document: dict[str, Any], blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    """Add deterministic headings; the source section is rendered separately."""
    validate_draft(document, blueprint)
    definitions = blueprint["sections"]
    return [{"section_id": row["section_id"],
             "heading": definitions[row["section_id"]]["heading"],
             "paragraphs": deepcopy(row["paragraphs"])} for row in document["sections"]]


def apply_repairs(document: dict[str, Any], blueprint: dict[str, Any],
                  review: dict[str, Any], repairs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_draft(document, blueprint)
    if review.get("protocol") != "wiki-editorial-patch-review-v1" or review.get("verdict") != "NO_GO":
        raise EditorialPatchError("a NO_GO patch review is required")
    issues = {str(item.get("issue_id")): item for item in review.get("issues", [])}
    if not issues or len(issues) != len(review.get("issues", [])):
        raise EditorialPatchError("review issue IDs must be non-empty and unique")
    supplied = {str(item.get("issue_id")): item for item in repairs}
    if set(supplied) != set(issues):
        raise EditorialPatchError("repairs must match review issues exactly")

    before = deepcopy(document)
    before_manifest = paragraph_manifest(before)
    result = deepcopy(document)
    touched: set[str] = set()
    explicitly_removed_claims: set[str] = set()
    explicitly_removed_chars = 0
    for issue_id, issue in issues.items():
        repair = supplied[issue_id]
        identity = (str(issue.get("section_id")), str(issue.get("paragraph_id")))
        if identity != (str(repair.get("section_id")), str(repair.get("paragraph_id"))):
            raise EditorialPatchError(f"repair target mismatch: {issue_id}")
        key = ".".join(identity)
        target_hash = str(issue.get("target_hash") or "")
        if target_hash != before_manifest.get(key) or repair.get("target_hash") != target_hash:
            raise EditorialPatchError(f"target hash conflict: {key}")
        operation = str(issue.get("operation") or "replace")
        replacements = _repair_paragraphs(repair, operation)
        if operation == "delete":
            before_section = next(
                row for row in before["sections"] if row["section_id"] == identity[0]
            )
            before_paragraph = next(
                row for row in before_section["paragraphs"]
                if row["paragraph_id"] == identity[1]
            )
            explicitly_removed_claims.update(_claim_ids({
                "sections": [{"paragraphs": [before_paragraph]}]
            }))
            explicitly_removed_chars += _body_chars({
                "sections": [{"paragraphs": [before_paragraph]}]
            })
        split_ids = _split_ids(identity[1], len(replacements))
        for replacement, split_id in zip(replacements, split_ids, strict=True):
            replacement["paragraph_id"] = split_id
        preserved = set(map(str, issue.get("facts_must_preserve", [])))
        reported = set(map(str, repair.get("preserved_claim_ids", [])))
        actual = _claim_ids({"sections": [{"paragraphs": replacements}]})
        if not preserved <= reported or not preserved <= actual:
            raise EditorialPatchError(f"required facts were not preserved: {issue_id}")
        replacement_text = "\n".join(_paragraph_text(item) for item in replacements)
        missing_tokens = [str(token) for token in issue.get("tokens_must_preserve") or []
                          if str(token) not in replacement_text]
        if missing_tokens:
            raise EditorialPatchError(
                f"required paragraph-local tokens were not preserved: {issue_id}: {missing_tokens}"
            )
        section = next((row for row in result["sections"]
                        if row["section_id"] == identity[0]), None)
        paragraph_index = next((index for index, row in enumerate(section["paragraphs"])
                                if row["paragraph_id"] == identity[1]), None) if section else None
        if section is None or paragraph_index is None or key in touched:
            raise EditorialPatchError(f"unknown or duplicate repair target: {key}")
        section["paragraphs"][paragraph_index:paragraph_index + 1] = replacements
        touched.add(key)

    validate_draft(result, blueprint)
    after_manifest = paragraph_manifest(result)
    changed = {key for key in before_manifest if before_manifest[key] != after_manifest.get(key)}
    if changed != touched:
        raise EditorialPatchError("repair changed an untargeted paragraph")
    inserted = set(after_manifest) - set(before_manifest)
    expected_inserted = {
        f"{issue['section_id']}.{split_id}"
        for issue_id, issue in issues.items()
        for split_id in _split_ids(
            str(issue["paragraph_id"]),
            len(_repair_paragraphs(supplied[issue_id], str(issue.get("operation") or "replace"))),
        )[1:]
    }
    if inserted != expected_inserted:
        raise EditorialPatchError("repair produced non-deterministic inserted paragraph IDs")
    before_claims, after_claims = _claim_ids(before), _claim_ids(result)
    if not (before_claims - explicitly_removed_claims) <= after_claims:
        raise EditorialPatchError("repair removed previously bound claims")
    before_chars, after_chars = _body_chars(before), _body_chars(result)
    if before_chars and after_chars < before_chars * 0.9 - explicitly_removed_chars:
        raise EditorialPatchError("repair reduced body length by more than 10%")
    receipt = {
        "protocol": "wiki-editorial-patch-receipt-v1",
        "before_hash": canonical_hash(before), "after_hash": canonical_hash(result),
        "targeted_paragraphs": sorted(touched),
        "unchanged_paragraphs": {key: digest for key, digest in before_manifest.items()
                                 if key not in touched},
        "paragraph_changes": [{
            "paragraph": key,
            "before": before_manifest[key],
            "after": [
                {"paragraph_id": split_id,
                 "sha256": after_manifest[f"{issue['section_id']}.{split_id}"]}
                for split_id in _split_ids(
                    str(issue["paragraph_id"]),
                    len(_repair_paragraphs(
                        supplied[issue_id], str(issue.get("operation") or "replace")
                    )),
                )
            ],
        } for issue_id, issue in sorted(issues.items())
          for key in [f"{issue['section_id']}.{issue['paragraph_id']}"]],
        "body_chars_before": before_chars, "body_chars_after": after_chars,
        "preserved_claim_ids": sorted(after_claims),
        "requires_independent_rereview": True,
    }
    return result, receipt
