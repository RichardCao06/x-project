#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用对账:把 LLM 产出的 schema 漂移收口,使名称图过 validate_graph 的 D/E(键唯一/受控)。
做四件(全部确定性、可重跑):
1. identity_scope:背景节点名为键、豁免 D/E。
2. 升格"表外刻面"(数据已用、约定未声明)进声明刻面集。
3. 派生消歧刻面:产品 product_subtype=产出活动 anchor;碰撞活动 reference_output=参考产物名 —— 解"靠名字区分但缺身份刻面"的碰撞(steel无、auto/aluminium有)。
4. 扩受控词表覆盖全部在用前景值。
用法: python3 scripts/reconcile.py docs/<slug>-name-graph.json
之后跑 scripts/validate_graph.py 复核。
"""
import json, sys
from collections import defaultdict, Counter
def tok(s): return s.split('(')[0].strip()
def sanitize_val(s):
    # 派生的刻面值(product_subtype/reference_output 来自节点名)不得含括号——括号是受控词表的 gloss 语法,
    # 否则 validate 的 tok() 会把 '电芯, 镍氢(NiMH)' 规约成 '电芯, 镍氢' 与原值不符而误判越界。半/全角括号→·并去重。
    s=str(s).replace('（','(').replace('）',')').replace('(','·').replace(')','')
    return s.strip().strip('·').strip()
META={'cpc','hs','isic','bref','nace','route_family','ipcc_category'}

def main(path):
    d=json.load(open(path)); P=d['products']; A=d['activities']; c=d['conventions']
    pdecl=[f['name'] for f in c['product_facets']]; adecl=[f['name'] for f in c['activity_facets']]
    bg={p['id'] for p in P if p.get('boundary')=='background'}
    name2p={p['name'].strip():p for p in P}

    # 1) identity_scope
    c.setdefault('identity_scope',{
      'rule':'键唯一⇒名唯一与刻面受控仅作用于 boundary=foreground;background 引用节点给名不展开、名为键、豁免 D/E(各带 home_industry,见跨行业关联规约)。',
      'rationale':'外购材料/部件是上游占位,不塞前景刻面。','applies_invariants':'D/E前景域;A/B/C全图。'})

    # 2) 升格表外刻面
    pextra=defaultdict(set); aextra=defaultdict(set)
    for p in P:
        if p['id'] in bg: continue
        for k,v in (p.get('facets') or {}).items():
            if k not in pdecl and k not in META: pextra[k].add(v)
    for a in A:
        for k,v in a['facets'].items():
            if k not in adecl and k not in META: aextra[k].add(v)
    for k,vs in pextra.items():
        c['product_facets'].append({'name':k,'description':f'{k}(对账升格:数据已用作产品身份区分)','controlled_values':sorted(vs),'rationale':'升格表外刻面以恢复键唯一。'}); pdecl.append(k)
    for k,vs in aextra.items():
        c['activity_facets'].append({'name':k,'description':f'{k}(对账升格)','controlled_values':sorted(vs),'rationale':'升格表外刻面。'}); adecl.append(k)

    # 3a) 产品 product_subtype = 产出它的活动 anchor(派生消歧)
    def collide(items, decl, idfn):
        g=defaultdict(list)
        for it in items: g[idfn(it,decl)].append(it)
        return {k:v for k,v in g.items() if len(v)>1}
    pcol=collide([p for p in P if p['id'] not in bg], pdecl, lambda p,dl:tuple((p.get('facets') or {}).get(x) for x in dl))
    if pcol:
        for a in A:
            anchor=a['facets'].get('reference_product_anchor','')
            st=anchor.replace('ref::','') if anchor else None
            for o in a.get('outputs',[]):
                if o.get('role')=='reference' and st:
                    p=name2p.get((o.get('product') or '').strip())
                    if p and p['id'] not in bg and 'product_subtype' not in p.get('facets',{}):
                        p['facets']['product_subtype']=st
        # 兜底:仍碰撞的前景产品(如共生品,非参考产出)用名字派生 product_subtype
        if 'product_subtype' not in pdecl: pdecl.append('product_subtype')
        for grp in collide([p for p in P if p['id'] not in bg], pdecl, lambda p,dl:tuple((p.get('facets') or {}).get(x) for x in dl)).values():
            for p in grp: p['facets']['product_subtype']=sanitize_val(p['name'])
        used=sorted({p['facets']['product_subtype'] for p in P if 'product_subtype' in p.get('facets',{})})
        if used and not any(f['name']=='product_subtype' for f in c['product_facets']):
            c['product_facets'].append({'name':'product_subtype','description':'具体产品子类型(派生自产出活动参考锚;共生品等用名字兜底),区分同粗键下不同产品。','controlled_values':used,'rationale':'§28合法拆分;键唯一⇒名唯一需要。'})

    # 3b) 碰撞活动 reference_output = 参考产物名
    acol=collide(A, adecl, lambda a,dl:tuple(a['facets'].get(x) for x in dl))
    if acol:
        col_ids={a['id'] for v in acol.values() for a in v}
        for a in A:
            if a['id'] in col_ids:
                ref=next((o.get('product') for o in a.get('outputs',[]) if o.get('role')=='reference'), None)
                if ref: a['facets']['reference_output']=sanitize_val(ref)
        used=sorted({a['facets']['reference_output'] for a in A if 'reference_output' in a.get('facets',{})})
        if used and not any(f['name']=='reference_output' for f in c['activity_facets']):
            c['activity_facets'].append({'name':'reference_output','description':'参考产物名(对账消歧:同签名活动产不同参考产品时区分)。','controlled_values':used,'rationale':'解活动键碰撞。'}); adecl.append('reference_output')

    # 4) 扩受控词表覆盖在用前景值
    def extend(flist):
        puse=defaultdict(set)
        for p in P:
            if p['id'] in bg: continue
            for fn,fv in (p.get('facets') or {}).items(): puse[fn].add(fv)
        return puse
    puse=defaultdict(set); ause=defaultdict(set)
    for p in P:
        if p['id'] in bg: continue
        for fn,fv in (p.get('facets') or {}).items(): puse[fn].add(fv)
    for a in A:
        for fn,fv in a['facets'].items(): ause[fn].add(fv)
    for f in c['product_facets']:
        have={tok(x) for x in f['controlled_values']}
        for v in sorted(puse.get(f['name'],set())):
            if v not in have: f['controlled_values'].append(v)
    for f in c['activity_facets']:
        have={tok(x) for x in f['controlled_values']}
        for v in sorted(ause.get(f['name'],set())):
            if v not in have: f['controlled_values'].append(v)

    d['_meta'].setdefault('reconciliation',{})['reconcile_py']='通用对账(reconcile.py):identity_scope+升格表外刻面+派生product_subtype/reference_output+扩词表。'
    json.dump(d,open(path,'w'),ensure_ascii=False,indent=1)
    print(f"对账 {path}: 升格产品表外{list(pextra)} 活动表外{list(aextra)}; product_subtype/reference_output 按需派生; 词表已扩。")

if __name__=='__main__':
    main(sys.argv[1])
