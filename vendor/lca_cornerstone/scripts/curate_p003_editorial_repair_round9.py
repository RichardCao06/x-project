#!/usr/bin/env python3
"""Apply the ninth frozen P003 editorial-review delta."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_wiki_content_capture import _claims, validate_result


def curate(content: dict) -> dict:
    sections = {section["heading"]: section for section in content["sections"]}
    fields = sections["节点特定采集字段"]["paragraphs"]
    fields[0]["sentences"][1]["text"] = (
        "BOM条目至少应包含部件名称、内部料号、供应商料号、数量、计量单位、单件质量、材料或部件类别以及是否随产品交付；"
        "随机附件若随整机交付，应以独立BOM条目记录其质量，不得并入包装质量。"
    )
    fields[4]["sentences"][0]["text"] = (
        "包装字段只记录内包装、外箱和托盘等包装组成的材料、质量、装载数量、重复使用次数与运输交接点；"
        "交接点和装载数量共同确定进入运输模型的货量及起始边界，且包装汇总必须排除已列入BOM的随机附件。"
    )
    for section in content["sections"]:
        for index, para in enumerate(section["paragraphs"], 1):
            para["focus"] = f"{section['heading']}：{para['sentences'][0]['text'][:38]}（R9-{index}）"
            for sentence_index, row in enumerate(para["sentences"]):
                row["rhetorical_role"] = (
                    "thesis" if sentence_index == 0
                    else "gap" if row["claim_kind"] == "evidence_gap"
                    else "explanation"
                )
    return content


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("content", type=Path)
    parser.add_argument("verify", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    blueprint = json.loads(args.blueprint.read_text(encoding="utf-8"))
    result = curate(json.loads(args.content.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(validate_result(args.output, blueprint, _claims(args.verify, blueprint["node_id"])), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
