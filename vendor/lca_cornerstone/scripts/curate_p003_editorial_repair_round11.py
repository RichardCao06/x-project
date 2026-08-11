#!/usr/bin/env python3
"""Apply the eleventh frozen P003 editorial-review delta."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_wiki_content_capture import _claims, validate_result


def curate(content: dict) -> dict:
    sections = {section["heading"]: section for section in content["sections"]}

    reference = sections["参考流与交接边界"]["paragraphs"]
    reference[2]["sentences"][1]["text"] = (
        "BOM质量闭合应同时检查标配件、选配件、紧固件、散热材料和外壳等易遗漏项目，并明确电子数据中"
        "数量单位与质量单位的换算关系；单台称量、型号级BOM和配置快照属于同一代表期和制造批次，"
        "是三方数据能够比较并形成质量闭合的前提。"
    )
    reference[3]["sentences"][1]["text"] = (
        "若研究对象确需包含独立附件，应另行定义成套交付组合、列明产品本体与每类附件的独立质量，"
        "并保持该组合口径与“一台完整刀片服务器”的产品本体参考流相互区分。"
    )

    scope = sections["分类与适用范围"]["paragraphs"]
    scope[2]["sentences"][1]["text"] = (
        "完整成品只有同时满足刀片式交付形态和以CPU通用计算为主的产品定位才能归入P003；"
        "两个正向准入项任一不满足即予排除，例如机架式交付或加速器主导的成品应转入与其全部身份刻面相符的服务器节点。"
    )

    for section in content["sections"]:
        for index, para in enumerate(section["paragraphs"], 1):
            para["focus"] = f"{section['heading']}：{para['sentences'][0]['text'][:38]}（R11-{index}）"
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
