#!/usr/bin/env python3
"""Apply the fifth frozen P003 editorial-review delta."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from run_wiki_content_capture import _claims,validate_result
from curate_p003_editorial_repair import paragraph,sentence

def curate(c):
 s={x['heading']:x for x in c['sections']}
 ref=s['参考流与交接边界']['paragraphs']
 ref[3]['sentences'][0]['text']=(
  '称量包含运输托盘或一次性保护袋时，应记录并扣除这些边界外辅助物；随机附件先按型号配置和交付清单判定，属于交付配置的计入参考流，明确排除的才扣除。')
 adj=s['规格与相邻节点区分']['paragraphs']; moved=adj[4:6]; del adj[4:6]
 role=s['在系统中的角色']['paragraphs']; role[3:3]=moved
 scope=s['分类与适用范围']['paragraphs']
 scope[1]['sentences']=[
  scope[1]['sentences'][0],
  sentence('被排除的刀片机箱、机架等物理基础设施进入各自产品节点，其物理质量不计入P003，但通过产品或物料接口保留关联。'),
  sentence('这些基础设施提供的供电、冷却或管理负荷进入相应服务接口，在使用阶段分配给P003，不能作为刀片服务器固有物料。')]
 scope[2]['sentences'][1]['text']=(
  '混合功能产品先依据交付定义确认产品类别，再以主要计算功能判定是否仍属CPU通用计算；物理质量贡献只用于解释配置差异。'
  '当交付定义与主要功能证据冲突，或加速部件改变主要功能和制造路线时，应建立独立配置类别并进入复核。')
 gaps=s['数据适用状态与缺口']['paragraphs']; old=gaps[3]['sentences']
 gaps[3:4]=[
  paragraph('供应链代表性采用四个独立评价维度',[old[0],sentence(
   '供应商覆盖、地理范围、时间窗口和技术代际分别记录覆盖范围、偏差方向和代理依赖，不先合成为单一粒度等级。')]),
  paragraph('数据适用等级描述证据粒度而非代表性',[old[1],sentence(
   '缺失、家族级代理和型号级实测覆盖只描述证据粒度；它与四维代表性评价并列报告，任何综合结论都必须披露两套结果及采用的保守规则。')]),
 ]
 for sec in c['sections']:
  for i,p in enumerate(sec['paragraphs'],1):
   p['focus']=f"{sec['heading']}：{p['sentences'][0]['text'][:40]}（R5-{i}）"
   for j,row in enumerate(p['sentences']):row['rhetorical_role']='thesis' if j==0 else ('gap' if row['claim_kind']=='evidence_gap' else 'explanation')
 return c
def main():
 ap=argparse.ArgumentParser();ap.add_argument('content',type=Path);ap.add_argument('verify',type=Path);ap.add_argument('blueprint',type=Path);ap.add_argument('output',type=Path);a=ap.parse_args();b=json.loads(a.blueprint.read_text());d=curate(json.loads(a.content.read_text()));a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n');print(json.dumps(validate_result(a.output,b,_claims(a.verify,b['node_id'])),ensure_ascii=False));return 0
if __name__=='__main__':raise SystemExit(main())
