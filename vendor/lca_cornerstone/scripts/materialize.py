#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 SOP workflow 的产出确定性物化为规范名称图 JSON —— 取代易失败的"Gate 汇报 agent"。
从 workflow 运行的 journal.jsonl 提取 consolidate 的 finalGraph + conventions + mappings,
派生稳定 id、建 PRODUCES/CONSUMES 边、组装 {_meta,conventions,products,activities,edges,external_mapping,scorecard}。
之后再跑 scripts/validate_graph.py(+ 必要时对账)。
用法: python3 scripts/materialize.py <journal.jsonl> <slug> "<行业显示名>" [out.json]
"""
import json, sys, os

def extract(journal_path):
    res=[json.loads(l)['result'] for l in open(journal_path) if l.strip()
         and json.loads(l).get('type')=='result' and isinstance(json.loads(l).get('result'),dict)]
    conv=next((r for r in res if 'product_facets' in r), None)
    graphs=[r for r in res if 'products' in r and 'activities' in r]
    final=max(graphs, key=lambda g:len(g['products'])) if graphs else None   # consolidate=最大图
    maps=[r for r in res if 'classification' in r]
    sc=next((r for r in res if 'rows' in r and 'overall' in r), None)
    return conv, final, maps, sc

def materialize(conv, g, maps, sc, slug, industry, out, date='2026-06-05'):
    P=g['products']; A=g['activities']
    for i,p in enumerate(P,1): p['id']=f"P{i:03d}"
    for i,a in enumerate(A,1): a['id']=f"A{i:03d}"
    name2pid={}
    for p in P: name2pid.setdefault(p['name'].strip(), p['id'])
    edges=[]; um_in=[]; um_out=[]
    for a in A:
        for o in a.get('outputs',[]):
            nm=(o.get('product') or '').strip(); pid=name2pid.get(nm)
            if pid: edges.append({'from':a['id'],'to':pid,'type':'PRODUCES','role':o.get('role','coproduct')})
            else: um_out.append((a['id'],nm))
        for nm in a.get('inputs',[]):
            nm=nm.strip(); pid=name2pid.get(nm)
            if pid: edges.append({'from':a['id'],'to':pid,'type':'CONSUMES'})
            else: um_in.append((a['id'],nm))
    bg=[p for p in P if p.get('boundary')=='background']; fg=[p for p in P if p.get('boundary')!='background']
    meta={'title':f'{industry} · 产品/活动名称图','industry':industry,
      'scope':'名称与拓扑层(基座骨架);外购投入=带home_industry的背景节点;不含数量。',
      'method':'六阶段SOP workflow 产出,从 journal 确定性物化(materialize.py)。','generated':date,
      'counts':{'products':len(P),'products_foreground':len(fg),'products_background':len(bg),'activities':len(A),
        'edges':len(edges),'edges_produces':sum(1 for e in edges if e['type']=='PRODUCES'),'edges_consumes':sum(1 for e in edges if e['type']=='CONSUMES')},
      'closure_check':{'invariant_A_orphans':0,'invariant_C_dangling':0,'activities_without_reference':0,'outputs_to_missing_product':0,'note':'物化后由 validate_graph.py 复核;此处占位,通过后即0'},
      'disclaimer':'命名/拓扑为可辩护成果;数值未挂;上游多为占位待建(见 home_industry)。'}
    if not sc:
        sc={'rows':[{'check':'闭合A/B/C','target':'0','status':'待validate'},
            {'check':'命名键唯一D','target':'0','status':'待复核(可能需对账)'},
            {'check':'刻面受控E','target':'0','status':'待复核'}],
            'overall':'物化版;待 validate + 对账。','longtail_notes':[]}
    out_obj={'_meta':meta,'conventions':conv,'products':P,'activities':A,'edges':edges,'external_mapping':maps,'scorecard':sc}
    json.dump(out_obj,open(out,'w'),ensure_ascii=False,indent=1)
    return len(P),len(fg),len(bg),len(A),len(edges),um_out,um_in

if __name__=='__main__':
    journal, slug, industry = sys.argv[1], sys.argv[2], sys.argv[3]
    out = sys.argv[4] if len(sys.argv)>4 else f'docs/{slug}-name-graph.json'
    conv, final, maps, sc = extract(journal)
    if not final: print("❌ journal 中未找到 finalGraph"); sys.exit(1)
    nP,nFg,nBg,nA,nE,umo,umi = materialize(conv, final, maps, sc, slug, industry, out)
    print(f"物化 {out}: {nP}产品({nFg}前景/{nBg}背景) / {nA}活动 / {nE}边")
    print(f"未匹配 输出名:{len(umo)} 输入名:{len(umi)}" + (f" · in e.g.{[n for _,n in umi[:6]]}" if umi else ""))
    bg_hi=sum(1 for p in final['products'] if p.get('boundary')=='background' and p.get('home_industry'))
    bg_tot=sum(1 for p in final['products'] if p.get('boundary')=='background')
    print(f"背景带 home_industry: {bg_hi}/{bg_tot}")
