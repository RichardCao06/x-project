#!/usr/bin/env python3
"""Apply the seventh frozen P003 editorial-review delta."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from curate_p003_editorial_repair import paragraph
from run_wiki_content_capture import _claims, validate_result


def curate(content: dict) -> dict:
    sections = {section["heading"]: section for section in content["sections"]}

    scope = sections["分类与适用范围"]["paragraphs"]
    scope[3]["sentences"][1]["text"] = (
        "交付定义与主要功能证据冲突时，应把对象列为待分类并补充交付文件、配置清单和功能证据，"
        "不能先建立配置类别。"
    )
    scope[3]["sentences"][2]["text"] = (
        "证据补齐前，相似外形只可支持候选筛选，不能替代产品身份、成品层级与BOM边界判定。"
    )

    fields = sections["节点特定采集字段"]["paragraphs"]
    original = fields[3]
    fields[3:4] = [
        paragraph(
            "制造活动与良率记录共同闭合投入、返工和报废计量",
            original["sentences"][:2],
        ),
        paragraph(
            "包装交接记录确定运输货量与运输边界",
            original["sentences"][2:],
        ),
    ]
    fields[3]["sentences"][0]["text"] = (
        "制造记录应覆盖装配、测试、老化筛选、返工和报废等活动，并为各活动保存计量范围、"
        "能源或辅料单位、批量及活动版本。"
    )
    fields[4]["sentences"][0]["text"] = (
        "包装字段应记录内包装、外箱、托盘和随机附件的材料、质量、装载数量、重复使用次数与"
        "运输交接点；交接点和装载数量共同确定进入运输模型的货量及起始边界。"
    )
    fields[4]["sentences"][1]["text"] = (
        "运输记录应从该交接点关联起讫地、运输方式、距离依据、装载状态、批量和中转节点；"
        "只有运输方式而没有路径与货量时，不足以形成型号级前景运输。"
    )

    regional = sections["区域化补充要求"]["paragraphs"]
    regional[1]["sentences"][2]["text"] = (
        "装配地或主要供应商区域变化时，应重配受影响活动的电力代理，并保留变更前后的地域依据。"
    )

    for section in content["sections"]:
        for index, para in enumerate(section["paragraphs"], 1):
            para["focus"] = f"{section['heading']}：{para['sentences'][0]['text'][:38]}（R7-{index}）"
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
