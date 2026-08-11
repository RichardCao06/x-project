#!/usr/bin/env python3
"""Apply the sixth frozen P003 editorial-review delta."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from run_wiki_content_capture import _claims,validate_result
from curate_p003_editorial_repair import paragraph,sentence
def curate(c):
 s={x['heading']:x for x in c['sections']}
 definition=s['定义与产品身份']['paragraphs']
 definition[0]['sentences'][1]['text']=(
  '高密度只描述产品形态，不足以单独确认P003成品身份；本文将“刀片服务器”和“服务器刀片”视为同一称谓，并以完整装配状态和交接记录判定对象。')
 adjacent=s['规格与相邻节点区分']['paragraphs']
 adjacent[2]['sentences'][1]['text']='成品SKU、装配范围和整机检验状态共同用于区分主板PCBA与P003成品。'
 adjacent[2]['sentences'][2]['text']=(
  '来源中的“服务器模块”属于待识别称谓：只有装配范围和检验记录证明它代表完整成品时才映射到刀片服务器，否则按PCBA、管理模块或待识别组件处理。')
 role=s['在系统中的角色']['paragraphs']
 role[0]['sentences'][2]['text']=(
  'A039与P003接口以产品流标识、配置版本和制造批次作为识别键；合格产品数量和对应净质量作为输出量值，良率口径与包装交接状态作为关联属性。')
 scope=s['分类与适用范围']['paragraphs']; old=scope[2]['sentences']
 scope[2:3]=[
  paragraph('不同排除维度分别对应功能、形态和产品层级',[sentence(
   '以加速器为主要计算功能的服务器因主要功能不同而排除；拥有独立机架外壳的机架式服务器因交付形态不同而排除；单独组件因未达到成品层级而排除。'),sentence(
   '三类对象分别转入相应服务器、机架式产品或部件节点，并通过产品、物料或服务接口与P003保持关系。')]),
  paragraph('混合功能产品的分类顺序与证据边界',[sentence(
   '混合功能产品先依据交付定义确认产品类别，再判断主要计算功能；物理质量贡献只解释配置差异，不单独决定分类。'),sentence(
   '交付定义与主要功能证据冲突，或加速部件改变主要功能和制造路线时，应建立独立配置类别并复核。'),old[-1]])]
 gaps=s['数据适用状态与缺口']['paragraphs']
 for p in gaps:
  for row in p['sentences']:
   row['text']=row['text'].replace('型号级实测覆盖','型号级可追溯覆盖')
 gaps[4]['sentences'][0]['text']=(
  '数据适用等级分为缺失、家族级代理和型号级可追溯覆盖；最高等级要求记录能够对应具体型号、配置、数据来源和代表期，但不因此自动声称数据来自直接测量。')
 for sec in c['sections']:
  for i,p in enumerate(sec['paragraphs'],1):
   p['focus']=f"{sec['heading']}：{p['sentences'][0]['text'][:38]}（R6-{i}）"
   for j,row in enumerate(p['sentences']):row['rhetorical_role']='thesis' if j==0 else ('gap' if row['claim_kind']=='evidence_gap' else 'explanation')
 return c
def main():
 ap=argparse.ArgumentParser();ap.add_argument('content',type=Path);ap.add_argument('verify',type=Path);ap.add_argument('blueprint',type=Path);ap.add_argument('output',type=Path);a=ap.parse_args();b=json.loads(a.blueprint.read_text());d=curate(json.loads(a.content.read_text()));a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n');print(json.dumps(validate_result(a.output,b,_claims(a.verify,b['node_id'])),ensure_ascii=False));return 0
if __name__=='__main__':raise SystemExit(main())
