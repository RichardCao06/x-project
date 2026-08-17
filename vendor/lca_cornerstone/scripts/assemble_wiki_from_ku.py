#!/usr/bin/env python3
"""按 KU 组装 wiki 正文段落——每条断言带真实溯源分级标记,取代裸 [^tag] 装饰性引用。
不改写现有 wiki 页(products/*.md 整页结构不动),只产出可供 apply_bodies 之类工具
拼装进 BODY 区块的分节文本，按 node_id 落一个文件。

用法: python3 scripts/assemble_wiki_from_ku.py <ku.json> <out_dir>
"""
import json, sys, os
from collections import defaultdict

from wiki_quality_contract import SECTIONS

BADGE = {
    'reviewed': '✅已核实',
    'draft': '〔未核实·模型回忆〕',
    'contradicted': '⚠️与检索结果冲突,待人工复核',
}


def badge_for(ku):
    """Render provenance class before confidence.

    A non-Web claim is not failed model recall: graph facts, modeling choices,
    and explicit evidence gaps each have a different epistemic meaning.
    """
    if ku['authority'] == 'reviewed':
        return BADGE['reviewed']
    kind = ku.get('claim_kind')
    if kind == 'internal_graph_fact':
        return '〔图谱事实〕'
    if kind == 'evidence_gap':
        return '〔证据缺口〕'
    if kind == 'modeling_judgment' or ku.get('believed_source') == 'INTERNAL_MODELING_JUDGMENT':
        return '〔建模判断〕'
    return BADGE.get(ku['authority'], BADGE['draft'])


def render_node(kus):
    by_section = defaultdict(list)
    for k in kus:
        by_section[k['section']].append(k)
    out = []
    for section, items in by_section.items():
        out.append(f'## {section}\n')
        for k in items:
            badge = badge_for(k)
            cite = (
                f" [^{k['ku_id']}]" if k['authority'] == 'reviewed'
                else " [^internal-graph]"
                if k.get('claim_kind') == 'internal_graph_fact'
                else " [^internal-review]"
                if k.get('claim_kind') in {'modeling_judgment', 'evidence_gap'}
                or k.get('believed_source') == 'INTERNAL_MODELING_JUDGMENT'
                else ''
            )
            out.append(f"{k['claim_text']} {badge}{cite}")
        out.append('')
    return '\n'.join(out)


def render_footnotes(kus):
    lines = ['## 出处\n']
    if any(k.get('claim_kind') == 'internal_graph_fact' for k in kus):
        lines.append(
            "[^internal-graph]: lca-cornerstone 名称图冻结节点、刻面与连线——"
            "由图谱文件确定性提取，不作为外部事实证据。"
        )
    if any(
        k.get('claim_kind') in {'modeling_judgment', 'evidence_gap'}
        or k.get('believed_source') == 'INTERNAL_MODELING_JUDGMENT'
        for k in kus
    ):
        lines.append(
            "[^internal-review]: 内部评审与建模约定——仅支持显式标注的系统边界、"
            "参考流、采集字段与数据缺口判断，不作为外部事实证据。"
        )
    for k in kus:
        if k['authority'] != 'reviewed':
            continue
        p = k['provenance']
        lines.append(f"[^{k['ku_id']}]: {p['ref']}"
                     + (f"，{p['locator']}" if p.get('locator') else '')
                     + f" —— 抓取摘录:「{p['quote']}」")
    return '\n'.join(lines)


def render_complete_node(kus, node_type):
    """Render the immutable ten-section BODY for a production v2 rebuild."""
    required = SECTIONS[node_type]
    by_section = defaultdict(list)
    for ku in kus:
        by_section[ku['section']].append(ku)
    expected = set(required[:-1])
    if set(by_section) != expected:
        raise ValueError(
            f"{node_type} KU 章节漂移: missing={sorted(expected-set(by_section))} "
            f"extra={sorted(set(by_section)-expected)}"
        )
    if any(not by_section[section] for section in expected):
        raise ValueError("production v2 每个正文章节必须至少一条冻结 KU")
    parts = []
    for section in required[:-1]:
        rendered = []
        for ku in by_section[section]:
            if ku.get('claim_kind') == 'external_fact' and ku['authority'] != 'reviewed':
                # Keep unsupported external claims in the frozen ledger for
                # coverage/reviewed gating, but do not copy one identical gap
                # shell into every content section.  Real gaps belong in the
                # dedicated evidence_gap KU under 数据适用状态与缺口.
                continue
            sentence = ku['claim_text']
            badge = badge_for(ku)
            if ku['authority'] == 'reviewed':
                cite = f" [^{ku['ku_id']}]"
            elif ku.get('claim_kind') == 'internal_graph_fact':
                cite = " [^internal-graph]"
            elif ku.get('claim_kind') != 'external_fact':
                cite = " [^internal-review]"
            else:
                cite = ""
            rendered.append(f"{sentence} {badge}{cite}")
        if not rendered:
            raise ValueError(
                f"{node_type} 章节 {section!r} 没有可写入的已核实或受控 KU；"
                "应补充研究或受控判断，不能用重复证据缺口壳占位"
            )
        parts.extend([f"## {section}", "", "\n\n".join(rendered), ""])
    parts.append(render_footnotes(kus).strip())
    return "\n".join(parts).strip() + "\n"


def _json_cell(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).replace('|', '\\|')


def render_evidence_tables(node_type, dossier):
    """Derive a non-numeric evidence-table floor from the frozen node dossier.

    Values that are not graph facts are rendered as collection requirements,
    never as model-supplied measurements.  This makes the structure independent
    of the nomination model while preserving the quantity firewall.
    """
    name = str(dossier.get('display_name') or dossier.get('name') or '待核节点')
    boundary = str(dossier.get('boundary') or '待核')
    facets = dossier.get('facets') or {}
    connections = dossier.get('connections') or {}
    if node_type == 'product':
        return f"""## 产品性质与交付状态

<!-- EV:props:START -->
| property | condition | unit | 值 | 源 | pedigree |
|---|---|---|---|---|---|
| 节点产品身份 | 冻结名称图 | — | {name} | internal-graph | 4,4,4,4,4 |
| 系统边界 | 冻结名称图 | — | {boundary} | internal-graph | 4,4,4,4,4 |
| 身份刻面 | 冻结名称图 | — | {_json_cell(facets)} | internal-graph | 4,4,4,4,4 |
| 图谱交接关系 | 冻结名称图 | — | {_json_cell(connections)} | internal-graph | 4,4,4,4,4 |
<!-- EV:props:END -->

## 产品规格与地区参数

<!-- EV:params:START -->
| parameter | geo | unit | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | pedigree |
|---|---|---|---|---|---|---|---|---|
| 完整型号或品级 | target | — | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
| 参考流与交接单位 | target | — | reference | 待采 | internal-review | 待采 | internal-review | 待评 |
| 单件或单位净质量 | target | kg | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
| 关键规格与组成 | target | — | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
| 生产地域与代表期 | target | — | reference | 待采 | internal-review | 待采 | internal-review | 待评 |
| 包装、运输与交接状态 | target | — | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
<!-- EV:params:END -->

## 数据质量与代表性

<!-- EV:quality:START -->
| field | unit | basis | 中国项目值 CN | 中国源 CN | proxy_policy | pedigree |
|---|---|---|---|---|---|---|
| 型号、批次与规格覆盖 | — | measured_average | 待采 | internal-review | 不得以相邻产品冒充目标节点 | 待评 |
| 参考流和交接边界一致性 | — | reference | 待核 | internal-review | 边界不一致只能作外部对照 | 待评 |
| 质量或数量测量方法 | — | measured_average | 待采 | internal-review | 规格上限不得冒充运行平均 | 待评 |
| 地域、技术和时间代表性 | — | reference | 待采 | internal-review | 保留原生产地与代表期 | 待评 |
| 代理、分配与不确定度 | — | calculated | 待算 | internal-review | 代理必须留痕并做敏感性分析 | 待评 |
<!-- EV:quality:END -->"""

    anchor = str((facets or {}).get('reference_product_anchor', '待核'))
    produces = [str(item) for item in connections.get('produces', [])]
    consumes = [str(item) for item in connections.get('consumes', [])]
    ordered_outputs = [anchor] + [item for item in produces if item != anchor]
    flow_rows = "\n".join(
        f"| {flow} | 输出 | 待采 | reference | 待采 | internal-graph | 待采 | internal-graph | 4,4,4,4,4 |"
        for flow in ordered_outputs
    )
    flow_rows += "\n" + "\n".join(
        f"| {flow} | 输入 | 待采 | reference | 待采 | internal-graph | 待采 | internal-graph | 4,4,4,4,4 |"
        for flow in consumes
    )
    flow_rows += "\n| 参考单位与合格产量分母 | 输出 | 待采 | reference | 待采 | internal-review | 待采 | internal-review | 待评 |"

    return f"""## 活动投入产出与参考流

<!-- EV:flows:START -->
| 流 | 方向 | 单位 | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | pedigree |
|---|---|---|---|---|---|---|---|---|
{flow_rows}
<!-- EV:flows:END -->

## 参考产品性质与交接状态

<!-- EV:props:START -->
| property | condition | unit | 值 | 源 | pedigree |
|---|---|---|---|---|---|
| 参考产品身份 | 活动交接点 | — | {anchor} | internal-graph | 4,4,4,4,4 |
| 参考产品完整型号与配置 | 活动交接点 | — | 待采 | internal-review | 待评 |
| 参考产品净质量或数量基准 | 活动交接点 | 待采 | 待采 | internal-review | 待评 |
| 参考产品规格、质量与交接状态 | 活动交接点 | — | 待采 | internal-review | 待评 |
<!-- EV:props:END -->

## 活动规格与地区参数

<!-- EV:params:START -->
| parameter | geo | unit | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | pedigree |
|---|---|---|---|---|---|---|---|---|
| 技术路线与设备配置 | target | — | reference | 待采 | internal-review | 待采 | internal-review | 待评 |
| 参考单位与产量分母 | target | — | reference | 待采 | internal-review | 待采 | internal-review | 待评 |
| 物料投入与损耗 | target | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
| 能源与公用工程 | target | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
| 直接排放与废物去向 | target | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
| 场址、代表期与分配规则 | target | — | reference | 待采 | internal-review | 待采 | internal-review | 待评 |
<!-- EV:params:END -->

## 直接排放与废物流

<!-- EV:emissions:START -->
| substance | CAS | compartment | unit | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | pedigree |
|---|---|---|---|---|---|---|---|---|---|
| 直接大气排放 | 待采 | air | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
| 直接水体排放 | 待采 | water | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
| 直接土壤排放 | 待采 | soil | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待评 |
<!-- EV:emissions:END -->

## 活动监测指标

<!-- EV:indicators:START -->
| indicator | medium | unit | basis | 国际值 INT | 国际源 INT | 中国值 CN | 中国源 CN | mapping_status | pedigree |
|---|---|---|---|---|---|---|---|---|---|
| 合格产量与吞吐量 | process | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待映射 | 待评 |
| 良率、返工率与报废率 | process | % | calculated | 待算 | internal-review | 待算 | internal-review | 待映射 | 待评 |
| 能源、公用工程与设备工时 | process | 待采 | measured_average | 待采 | internal-review | 待采 | internal-review | 待映射 | 待评 |
<!-- EV:indicators:END -->

## 数据质量与代表性

<!-- EV:quality:START -->
| field | unit | basis | 中国项目值 CN | 中国源 CN | proxy_policy | pedigree |
|---|---|---|---|---|---|---|
| 工艺路线和设备覆盖 | — | reference | 待核 | internal-review | 相邻工艺不得直接替代 | 待评 |
| 参考产品、单位与边界一致性 | — | reference | 待核 | internal-review | 边界不一致只作外部对照 | 待评 |
| 物料能源计量和分配 | — | measured_average | 待采 | internal-review | 额定值不得冒充实测平均 | 待评 |
| 地域、技术与时间代表性 | — | reference | 待采 | internal-review | 保留原场址和代表期 | 待评 |
| 代理、缺口与不确定度 | — | calculated | 待算 | internal-review | 代理必须留痕并做敏感性分析 | 待评 |
<!-- EV:quality:END -->"""


if __name__ == '__main__':
    kus = json.load(open(sys.argv[1]))['kus']
    outdir = sys.argv[2]
    os.makedirs(outdir, exist_ok=True)

    by_node = defaultdict(list)
    for k in kus:
        by_node[k['node_id']].append(k)

    for node_id, ks in by_node.items():
        body = render_node(ks)
        footnotes = render_footnotes(ks)
        open(os.path.join(outdir, f'{node_id}.ku-body.md'), 'w').write(body)
        if any(
            k['authority'] == 'reviewed'
            or k.get('claim_kind') == 'internal_graph_fact'
            or k.get('claim_kind') == 'evidence_gap'
            or k.get('believed_source') == 'INTERNAL_MODELING_JUDGMENT'
            for k in ks
        ):
            open(os.path.join(outdir, f'{node_id}.ku-footnotes.md'), 'w').write(footnotes)

    print(f'{len(by_node)} 节点渲染完成 → {outdir}')
    reviewed = sum(1 for k in kus if k['authority'] == 'reviewed')
    print(f'其中 {reviewed}/{len(kus)} 条断言达到 ✅已核实 等级,可安全升级 wiki 正文引用')
