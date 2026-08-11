#!/usr/bin/env python3
"""Apply the fourth frozen P003 editorial-review delta."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from run_wiki_content_capture import _claims,validate_result
from curate_p003_editorial_repair import sentence

def curate(c):
 s={x['heading']:x for x in c['sections']}
 form=s['性质与形态']['paragraphs']
 form[1]['sentences'][1]['text']=form[1]['sentences'][1]['text'].replace('、是否标配和是否可更换','和是否随成品交付')
 form[1]['sentences'][2]['text']=(
  '无法确认存储器件、网络适配件或管理模块是否随产品交付时，应分别建立纳入与不纳入情景；不得依据产品家族资料反推具体部件数量。')
 form[2]['sentences'][-1]['text']='若维修资料只给出备件套装质量而未区分实际换下部件，套装不得直接充当初始BOM，也不得假定每次维修消耗整套备件。'
 adj=s['规格与相邻节点区分']['paragraphs']
 for p in adj:
  for row in p['sentences']:
   row['text']=row['text'].replace('刀片本体','刀片服务器本身')
 adj[2]['sentences'][0]['text']=(
  '主板PCBA只是刀片服务器的组成部件；只有完成规定结构装配、配置集成和整机检验后，才形成P003成品。')
 adj[2]['sentences'][2]['text']=(
  '来源中的“服务器模块”是待识别称谓，只有证据表明它代表完整成品时才映射到刀片服务器；结构壳体仍只是该成品的组成部分。')
 adj[4]['focus']='共享制造资源不得被误认成刀片服务器规格或实体'
 adj[4]['sentences'][0]['text']=(
  '共享制造资源位于刀片服务器产品实体之外；只有无法直接计量且确实服务多台产品时，才按制造批次、设备占用时间或资源驱动量分配其负荷。')
 adj[4]['sentences'][1]['text']=(
  '分配结果是制造活动负荷，不是产品规格或相邻产品实体；能够直接归属单台产品的消耗不得参加平均分配。')
 adj[5]['focus']='刀片机箱服务与刀片服务器实体边界'
 adj[5]['sentences'][0]['text']=(
  '使用阶段的机箱供电和冷却属于刀片机箱提供的共享服务，不属于刀片服务器的固有物料构成。')
 adj[5]['sentences'][1]['text']=(
  '在实体边界明确后，服务负荷可按槽位、产品数量、运行时长或实测增量分配；存在独占资源或直接计量时，平均分配即失效。')
 role=s['在系统中的角色']['paragraphs']
 for p in role:
  for row in p['sentences']: row['text']=row['text'].replace('。。','。')
 fields=s['节点特定采集字段']['paragraphs']
 fields[2]['sentences'][1]['text']='共享机箱只作为供电与冷却接口的连接对象记录，不纳入刀片服务器本体。'
 gaps=s['数据适用状态与缺口']['paragraphs']
 gaps[3]['sentences'][1]['text']=(
  '另行设置数据适用等级：未取得记录为“缺失”，仅有家族映射为“代理”，能够追溯到具体型号和配置才属于“型号级实测覆盖”。')
 gaps[3]['sentences'].insert(2,sentence(
  '四个代表性维度分别记录覆盖范围和偏差；任一维度仍依赖家族映射时，整体适用等级不得高于代理，不能写成型号数据已经完整。'))
 for sec in c['sections']:
  for i,p in enumerate(sec['paragraphs'],1):
   p['focus']=f"{sec['heading']}：{p['sentences'][0]['text'][:42]}（R4-{i}）"
   for j,row in enumerate(p['sentences']): row['rhetorical_role']='thesis' if j==0 else ('gap' if row['claim_kind']=='evidence_gap' else 'explanation')
 return c
def main():
 ap=argparse.ArgumentParser();ap.add_argument('content',type=Path);ap.add_argument('verify',type=Path);ap.add_argument('blueprint',type=Path);ap.add_argument('output',type=Path);a=ap.parse_args();b=json.loads(a.blueprint.read_text());d=curate(json.loads(a.content.read_text()));a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n');print(json.dumps(validate_result(a.output,b,_claims(a.verify,b['node_id'])),ensure_ascii=False));return 0
if __name__=='__main__':raise SystemExit(main())
