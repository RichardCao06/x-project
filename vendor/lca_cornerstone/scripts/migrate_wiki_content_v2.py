#!/usr/bin/env python3
"""Deterministically editorialize a validated v1 content draft into protocol v2."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from run_wiki_content_capture import _claims, validate_result


def merge_text(left: str, right: str) -> str:
    left = re.sub(r"[。！？；]+$", "", left.strip())
    right = re.sub(r"^[同时此外并且，,\s]+", "", right.strip())
    return f"{left}；同时，{right}"


def migrate(legacy: dict, blueprint: dict) -> dict:
    fusions = {
        (item["section"], tuple(item["claim_ids"])): item
        for item in blueprint.get("editorial_fusions", [])
    }
    sections = []
    for section in legacy["sections"]:
        paragraphs = []
        for paragraph in section["paragraphs"]:
            converted = []
            index = 0
            sentences = paragraph["sentences"]
            while index < len(sentences):
                matched = None
                for (heading, claim_ids), fusion in fusions.items():
                    if heading != section["heading"] or index + len(claim_ids) > len(sentences):
                        continue
                    actual = tuple(
                        str(sentences[index + offset].get("source_claim_id"))
                        for offset in range(len(claim_ids))
                    )
                    if actual == claim_ids:
                        matched = fusion
                        break
                if matched:
                    converted.append({"text": matched["text"], "claim_kind": matched["claim_kind"],
                                      "rhetorical_role": "thesis" if not converted else "evidence",
                                      "evidence_claim_ids": list(matched["claim_ids"])})
                    index += len(matched["claim_ids"])
                    continue
                sentence = sentences[index]
                source_id = sentence.get("source_claim_id")
                converted.append({
                    "text": sentence["text"],
                    "claim_kind": sentence["claim_kind"],
                    "rhetorical_role": "thesis" if not converted else (
                        "gap" if sentence["claim_kind"] == "evidence_gap" else "explanation"
                    ),
                    "evidence_claim_ids": [source_id] if source_id else [],
                })
                index += 1
            while len(converted) > int(blueprint["golden_target"].get("maximum_sentences_per_paragraph", 4)):
                pair = None
                for pos in range(0, len(converted) - 1):
                    if converted[pos]["claim_kind"] == converted[pos + 1]["claim_kind"]:
                        pair = pos
                        break
                if pair is None:
                    raise ValueError(f"{section['heading']} 无法在不混淆 claim_kind 的情况下收敛段落")
                left, right = converted[pair], converted[pair + 1]
                left["text"] = merge_text(left["text"], right["text"])
                left["evidence_claim_ids"].extend(right["evidence_claim_ids"])
                left["rhetorical_role"] = (
                    "thesis" if pair == 0 else
                    "gap" if left["claim_kind"] == "evidence_gap" else "explanation"
                )
                converted.pop(pair + 1)
            for pos, sentence in enumerate(converted):
                sentence["rhetorical_role"] = "thesis" if pos == 0 else sentence["rhetorical_role"]
            focus = converted[0]["text"].split("。", 1)[0]
            paragraphs.append({"focus": focus[:80], "sentences": converted})
        sections.append({"heading": section["heading"], "paragraphs": paragraphs})
    return {"protocol": "wiki-content-draft-v2", "node_id": blueprint["node_id"], "sections": sections}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("legacy_content", type=Path)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    blueprint = json.loads(args.blueprint.read_text(encoding="utf-8"))
    legacy = json.loads(args.legacy_content.read_text(encoding="utf-8"))
    result = migrate(legacy, blueprint)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    scorecard = validate_result(args.output, blueprint, _claims(args.verify_output, blueprint["node_id"]))
    print(json.dumps(scorecard, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
