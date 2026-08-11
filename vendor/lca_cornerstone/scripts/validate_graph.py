#!/usr/bin/env python3
"""名称图闭合+命名纪律校验 gate(对账v2)。
从 conventions 自动读身份刻面;按 identity_scope 对前景域判『键唯一/受控』,背景节点名为键。
用法: python scripts/validate_graph.py docs/steel-name-graph.json [...]
退出码 0=全过, 1=有违反。"""
import json, sys
from collections import Counter, defaultdict
def tok(s): return s.split("(")[0].strip()

def validate(path):
    d=json.load(open(path)); P=d['products']; A=d['activities']; E=d['edges']; c=d['conventions']
    R=[]; ok=lambda b,m:R.append((b,m))
    bfield='boundary' if P and 'boundary' in P[0] else 'role'
    bg={p['id'] for p in P if p.get(bfield) in ('background','bg')}
    fg=lambda i: i not in bg
    pid={p['id']:p for p in P}; aid={a['id']:a for a in A}
    pident=[f['name'] for f in c['product_facets']]      # 声明=身份
    aident=[f['name'] for f in c['activity_facets']]
    pfac={f['name']:{tok(x) for x in f['controlled_values']} for f in c['product_facets']}
    afac={f['name']:{tok(x) for x in f['controlled_values']} for f in c['activity_facets']}

    # ID/名唯一
    ok(len({p['id'] for p in P})==len(P) and len({a['id'] for a in A})==len(A),"ID 唯一")
    dpn={k:v for k,v in Counter(p['name'] for p in P).items() if v>1}
    dan={k:v for k,v in Counter(a['name'] for a in A).items() if v>1}
    ok(not dpn and not dan,f"显示名唯一 (产品重复{list(dpn)[:3]} 活动重复{list(dan)[:3]})")

    # 键唯一(前景域)
    pk=Counter(tuple((p.get('facets') or {}).get(x) for x in pident) for p in P if fg(p['id']))
    ak=Counter(tuple(a['facets'].get(x) for x in aident) for a in A if fg(a['id']))
    dpk=[k for k,v in pk.items() if v>1]; dak=[k for k,v in ak.items() if v>1]
    ok(not dpk,f"前景产品键唯一 ({len(dpk)}组碰撞)")
    ok(not dak,f"前景活动键唯一 ({len(dak)}组碰撞)")

    # 受控(前景域)
    pv=[(p['id'],fn,fv) for p in P if fg(p['id']) for fn,fv in (p.get('facets') or {}).items() if fn in pfac and fv not in pfac[fn]]
    av=[(a['id'],fn,fv) for a in A if fg(a['id']) for fn,fv in a['facets'].items() if fn in afac and fv not in afac[fn]]
    ok(not pv,f"前景产品刻面受控 ({len(pv)}越界 e.g.{pv[:3]})")
    ok(not av,f"前景活动刻面受控 ({len(av)}越界 e.g.{av[:3]})")

    # 边完整 + A/B/C
    allids={**pid,**aid}
    ok(all(e['from'] in allids and e['to'] in allids for e in E),"边端点存在")
    produced={e['to'] for e in E if e['type']=='PRODUCES'}
    orph=[i for i in pid if fg(i) and i not in produced]
    ok(not orph,f"不变量A 无孤儿前景产品 ({len(orph)} e.g.{orph[:4]})")
    noref=[a['id'] for a in A if sum(1 for o in a.get('outputs',[]) if o.get('role')=='reference')!=1]
    ok(not noref,f"不变量B 每活动恰1参考产出 ({len(noref)} e.g.{noref[:4]})")
    cons=[e for e in E if e['type']=='CONSUMES']
    dang=[e for e in cons if e['to'] not in pid and e['from'] not in pid]
    ok(not dang,f"不变量C 无悬空输入 ({len(dang)})")

    # 内联 outputs 名 ↔ PRODUCES 边 一致
    name2id=defaultdict(list)
    for p in P: name2id[p['name'].strip()].append(p['id'])
    ape=defaultdict(set)
    for e in E:
        if e['type']=='PRODUCES' and e['from'] in aid and e['to'] in pid: ape[e['from']].add(e['to'])
    mis=sum(1 for a in A if {i for o in a.get('outputs',[]) for i in name2id.get((o.get('product') or '').strip(),[])}!=ape.get(a['id'],set()))
    ok(mis==0,f"内联产出↔PRODUCES边 一致 ({mis}不符)")

    npass=sum(1 for b,_ in R if b)
    return R,npass,len(R)

allok=True
for path in sys.argv[1:]:
    R,np_,nt=validate(path)
    print(f"\n{'='*64}\n  {path}\n{'='*64}")
    for b,m in R: print(("  ✅ " if b else "  ❌ ")+m)
    print(f"  → {np_}/{nt} 通过")
    allok=allok and np_==nt
sys.exit(0 if allok else 1)
