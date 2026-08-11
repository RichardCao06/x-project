#!/usr/bin/env python3
"""Apply the tenth frozen P003 editorial-review delta."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_wiki_content_capture import _claims, validate_result


def curate(content: dict) -> dict:
    sections = {section["heading"]: section for section in content["sections"]}

    reference = sections["参考流与交接边界"]["paragraphs"]
    reference[3]["sentences"][0]["text"] = (
        "称量包含运输托盘或一次性保护袋时，应记录并扣除这些边界外辅助物；只有装配在服务器本体内、"
        "构成该型号配置的部件计入单台净质量，独立随附的线缆、工具或备件即使列入交付清单也应作为单独交付物流记录。"
    )
    reference[3]["sentences"][1]["text"] = (
        "若研究对象确需包含独立附件，应另行定义成套交付组合及其质量口径；若单台称量、型号级BOM和配置快照"
        "不属于同一代表期或制造批次，则产品本体质量闭合不成立，也不得与附件质量拼成所谓实测参考流。"
    )

    scope = sections["分类与适用范围"]["paragraphs"]
    scope[2]["sentences"][0]["text"] = (
        "排除判定先区分组件与成品：未达到完整装配和成品检验状态的对象转入相应部件节点；"
        "只有完整成品才进入服务器产品形态与主要功能的联合判定。"
    )
    scope[2]["sentences"][1]["text"] = (
        "完整成品必须同时具有刀片式交付形态和以CPU通用计算为主的产品定位才能归入P003；"
        "机架式外形或以加速器为主的功能任一不满足时，均应转入与其全部身份刻面相符的服务器节点，"
        "两项同时不满足时不得只按其中一项覆盖另一项。"
    )

    for section in content["sections"]:
        for index, para in enumerate(section["paragraphs"], 1):
            para["focus"] = f"{section['heading']}：{para['sentences'][0]['text'][:38]}（R10-{index}）"
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
