#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨行业关联 gate（《跨行业关联规约》§6）。在各行业 validate_graph(A–E) 之上。
检查: GPID 覆盖/确定/唯一 · 单母行业 · C′跨行业闭合(无哑名) · 解析目标有效 · CPC/HS sanity · 覆盖率。
用法: python3 scripts/cross_link_lint.py
退出码 0=全过(硬检查) 1=有违反；CPC/HS 不一致只 warn。"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assign_gpid import slug, INDUSTRIES

def main():
    reg=json.load(open('registry/products.json')); P=reg['products']
    graphs={ind:json.load(open(p)) for ind,p in INDUSTRIES.items()}
    order={ind:[f['name'] for f in g['conventions']['product_facets']] for ind,g in graphs.items()}
    R=[]; warns=[]; ok=lambda b,m:R.append((b,m))

    # 1 GPID 覆盖 + 确定性
    miss=[]; nondet=[]
    for ind,g in graphs.items():
        for p in g['products']:
            if p.get('boundary')=='background': continue
            gp=p.get('gpid')
            if not gp: miss.append((ind,p['id'])); continue
            want=f"{ind}::{slug(p.get('facets') or {},order[ind])}"
            if gp!=want: nondet.append((p['id'],gp,want))
    ok(not miss, f"GPID 覆盖(每前景产品有gpid) ({len(miss)}缺 e.g.{miss[:3]})")
    ok(not nondet, f"GPID 确定(==<行业>::slug(键)) ({len(nondet)}不符 e.g.{[n[0] for n in nondet[:3]]})")

    # 2 registry GPID 唯一 + 单母行业（注册表键即 gpid;校验每 gpid 一个 home,且与图内一致)
    homes={}; bad_home=[]
    for ind,g in graphs.items():
        for p in g['products']:
            if p.get('boundary')=='background' or not p.get('gpid'): continue
            gp=p['gpid']
            if gp in homes and homes[gp]!=ind: bad_home.append(gp)
            homes[gp]=ind
            if P.get(gp,{}).get('home_industry')!=ind: bad_home.append(gp)
    ok(not bad_home, f"单母行业(无产品被两行业前景化/注册表一致) ({len(set(bad_home))}冲突 e.g.{list(set(bad_home))[:2]})")

    # 3 C′ 跨行业闭合:每个背景输入 已解析(built gpid) 或 显式 placeholder —— 无哑名
    dangling=[]; bad_target=[]; cov={ind:{'resolved':0,'placeholder':0,'total':0} for ind in graphs}
    for ind,g in graphs.items():
        for p in g['products']:
            if p.get('boundary')!='background': continue
            cov[ind]['total']+=1
            st=p.get('home_status'); rt=p.get('resolves_to')
            if st in ('linked','internal') and rt:
                cov[ind]['resolved']+=1
                if rt not in P or P[rt].get('status')!='built': bad_target.append((p['id'],rt))
            elif st=='placeholder' and p.get('home_industry'):
                cov[ind]['placeholder']+=1
            else:
                dangling.append((ind,p['id']))   # 既没解析也没占位 = 哑名
    ok(not dangling, f"C′ 无哑名(背景输入皆 已解析|占位) ({len(dangling)}哑名 e.g.{dangling[:3]})")
    ok(not bad_target, f"解析目标有效(指向 built 前景 gpid) ({len(bad_target)}无效 e.g.{bad_target[:3]})")

    # 4 CPC/HS sanity（warn,非硬）:已连接两端 cpc 或 hs 至少一项同
    for ind,g in graphs.items():
        for p in g['products']:
            if p.get('home_status')=='linked' and p.get('resolves_to') in P:
                t=P[p['resolves_to']]
                # CPC/HS 粗,按 HS 4位族 与 cpc 比;族级都不同才警(疑似错配)
                hs_diff = p.get('hs') and t.get('hs') and str(p['hs'])[:4]!=str(t['hs'])[:4]
                cpc_diff = p.get('cpc') and t.get('cpc') and p['cpc']!=t['cpc']
                if hs_diff and cpc_diff:
                    warns.append(f"{p['id']}({p['name']}) HS族/CPC 与目标 {p['resolves_to']} 均不同—疑似错配")

    # 4.5 跨族 sanity（优化2,《方法论复盘-EPC案例.md》§4）:名义化学/材料族冲突 warn。
    # 补 HS 抓不到的同码跨族错配(铅酸↔NMC 同 HS8507;退役铅酸→锂电芯就栽在这)。
    INCOMPAT=[  # (族A 词集, 族B 词集):源命中A而目标命中B(或反)= 疑似跨族错配
        ({'铅酸','lead_acid','lead'}, {'nmc','ncm','lfp','lithium','锂','三元','磷酸铁'}),       # 电池化学体系
        ({'碳酸锂','氢氧化锂','锂盐','lithium_carbonate'}, {'para_xylene','对二甲苯','苯乙烯','styrene','olefin','烯烃'}),  # 锂盐↔石化
        ({'环氧','epoxy'}, {'fluoropolymer','氟塑料','ptfe','pvdf','聚四氟','聚偏氟'}),            # 聚合物族
        ({'天然橡胶','natural_rubber','nr,'}, {'sbr','丁苯','br,','顺丁','nbr','丁腈'}),           # 弹性体族(可选)
    ]
    famwarns=[]
    def hit(text,s): t=(text or '').lower(); return any(k.lower() in t for k in s)
    for ind,g in graphs.items():
        for p in g['products']:
            if p.get('home_status') not in ('linked','internal') or p.get('resolves_to') not in P: continue
            src=p['name']; tgt=p['resolves_to']+' '+P[p['resolves_to']].get('display_name','')
            for a,b in INCOMPAT:
                if (hit(src,a) and hit(tgt,b)) or (hit(src,b) and hit(tgt,a)):
                    famwarns.append(f"{p['id']}({p['name'][:20]}) → {p['resolves_to'][:40]} 跨族冲突")
                    break

    npass=sum(1 for b,_ in R if b)
    print(f"\n{'='*60}\n  跨行业关联 lint · {len(P)} GPID · 行业 {sorted(graphs)}\n{'='*60}")
    for b,m in R: print(("  ✅ " if b else "  ❌ ")+m)
    print("  ── 覆盖率(背景输入 已解析/占位)──")
    for ind,c in cov.items():
        print(f"     {ind}: 解析 {c['resolved']}/{c['total']} · 占位 {c['placeholder']}/{c['total']}")
    if warns:
        print("  ── CPC/HS warn(需专家复核)──")
        for w in warns[:6]: print("     ⚠ "+w)
    if famwarns:
        print("  ── 跨族 sanity warn(化学/材料族冲突,优先复核)──")
        for w in famwarns: print("     ⚠⚠ "+w)
    else:
        print("  ── 跨族 sanity: 0 冲突 ✅")
    print(f"  → {npass}/{len(R)} 硬检查通过" + (f" · {len(warns)} CPC/HS warn" if warns else "") + (f" · {len(famwarns)} 跨族冲突" if famwarns else ""))
    return npass==len(R)

if __name__=='__main__':
    sys.exit(0 if main() else 1)
