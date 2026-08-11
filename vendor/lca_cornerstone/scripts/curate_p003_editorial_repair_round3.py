#!/usr/bin/env python3
"""Apply the third frozen P003 editorial-review delta."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from run_wiki_content_capture import _claims, validate_result
from curate_p003_editorial_repair import paragraph, sentence


def curate(content):
    sections={s['heading']:s for s in content['sections']}
    form=sections['性质与形态']['paragraphs']
    original=form[0]['sentences']
    component=form[1]
    interface=form[2]['sentences'][-1]
    packaging=form[3]
    form[:]=[
        paragraph('产品性质、最低构成与独立性的限定', [original[0], sentence(
            '这里的“独立”只表示刀片服务器能作为独立计算节点承担服务器功能，并不表示它可以脱离刀片机箱运行；“服务器刀片”只是同义简称。')]),
        paragraph('产品构成边界、BOM闭合与配置不确定性', [
            sentence('产品构成边界从完成装配并随交付移交的刀片服务器本体开始，覆盖板载电路以及随配置交付的计算、存储、网络和管理部件。'),
            original[2], original[3], interface]),
        component, packaging,
    ]

    ref=sections['参考流与交接边界']['paragraphs']
    ref[3]['sentences'].pop(1)

    adjacent=sections['规格与相邻节点区分']['paragraphs']
    adjacent[1]['sentences'][1]['text']=(
        '机架式服务器或完整刀片机箱即使与本产品共享部分部件，也不能仅凭用途相近套用同一成品BOM，除非逐项证明交付边界和配置等价。')
    adjacent[2]['sentences'][1]['text']=(
        '成品SKU、装配范围和整机检验状态用于区分主板PCBA与成品；刀片本体结构壳体与独立机架式整机外壳则作为互斥形态字段，后者只用于排除机架式服务器。')
    allocation=adjacent[4]['sentences']
    adjacent[4:5]=[
        paragraph('共享制造资源的负荷分配', [
            sentence('共享制造资源确实服务多台刀片服务器且无法直接计量时，可按制造批次、设备占用时间或可解释的资源驱动量分配。', ids=['P003-15']),
            sentence('能够直接归属单台产品的制造消耗不得参与平均分配，分配记录应保存资源总量、批量、驱动量和替代方法。')]),
        paragraph('使用阶段共享机箱负荷的分配与失效条件', [
            sentence('使用阶段可依据槽位、刀片服务器数量、运行时长、利用率或实测增量负荷分配机箱供电与冷却服务。'),
            sentence('存在独占电源、独立冷却回路、显著运行时长差异或直接增量计量时，按槽位平均即失效，并应记录空槽位、冗余配置和敏感性方法。')]),
    ]

    role=sections['在系统中的角色']['paragraphs']
    text=role[1]['sentences'][0]['text']
    parts=[p.strip() for p in text.replace('；同时，','；').split('；') if p.strip()]
    trace, purpose=parts[1].split('，使',1)
    role[1]['sentences']=[sentence(parts[0]+'。',role='thesis'), sentence(trace+'。'),
                          sentence('这些追溯字段使'+purpose+'。'), role[1]['sentences'][1]]

    fields=sections['节点特定采集字段']['paragraphs']
    old=fields[1]['sentences']
    management, shared=old[2]['text'].split(' 供电接口',1)
    fields[1:2]=[
        paragraph('硬件配置字段及板级归属', [old[0], old[1], sentence(management), old[3]]),
        paragraph('供电与冷却字段只描述共享机箱接口', [
            sentence('供电接口'+shared),
            sentence('这些接口字段用于匹配下游使用模型，不把机箱实体纳入刀片服务器本体，也不把额定配置直接当作实际能耗。')]),
    ]

    gaps=sections['数据适用状态与缺口']['paragraphs']
    gaps[3]['sentences'][0]['text']='供应链代表性应分别从供应商覆盖、地理范围、时间窗口和技术代际四个维度评价。'
    gaps[3]['sentences'][1]['text']=(
        '评价结果统一分为真实缺失、家族级代理和型号级实测覆盖三类；家族代理不得被描述为型号数据已经完整。')

    for sec in content['sections']:
        for i,p in enumerate(sec['paragraphs'],1):
            p['focus']=f"{sec['heading']}：{p['sentences'][0]['text'][:44]}（R3-{i}）"
            for j,s in enumerate(p['sentences']):
                s['rhetorical_role']='thesis' if j==0 else ('gap' if s['claim_kind']=='evidence_gap' else 'explanation')
    return content

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('content',type=Path); ap.add_argument('verify',type=Path); ap.add_argument('blueprint',type=Path); ap.add_argument('output',type=Path); a=ap.parse_args()
    b=json.loads(a.blueprint.read_text()); d=curate(json.loads(a.content.read_text())); a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(validate_result(a.output,b,_claims(a.verify,b['node_id'])),ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
