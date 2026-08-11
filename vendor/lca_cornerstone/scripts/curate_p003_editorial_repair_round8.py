#!/usr/bin/env python3
"""Apply the eighth frozen P003 editorial-review delta."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_wiki_content_capture import _claims, validate_result


def curate(content: dict) -> dict:
    sections = {section["heading"]: section for section in content["sections"]}
    scope = sections["分类与适用范围"]["paragraphs"]
    scope[2]["sentences"][0]["text"] = (
        "排除判定先检查成品层级和交付形态：单独组件转入相应部件节点，并只在它实际进入P003的BOM时作为物料输入；"
        "具有独立机架外壳的完整成品转入机架式服务器节点，不因同属服务器而并入P003。"
    )
    scope[2]["sentences"][1]["text"] = (
        "通过成品层级和交付形态检查后再判断主要计算功能；以加速器为主要功能的成品转入相应加速服务器节点，"
        "即使同时具有机架式外形，也不得回退为刀片服务器，只有冻结图谱或具体产品系统存在实际关系时才建立接口。"
    )
    for section in content["sections"]:
        for index, para in enumerate(section["paragraphs"], 1):
            para["focus"] = f"{section['heading']}：{para['sentences'][0]['text'][:38]}（R8-{index}）"
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
