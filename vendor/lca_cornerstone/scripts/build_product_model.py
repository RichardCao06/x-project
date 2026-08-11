#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_product_model.py — 无数值产品系统模型(统一引擎,Mode A + Mode B 同形)。

两模式同一棵树形:**整车 → 子系统 → 组件 → 骨架材料节点 → 沿拓扑递归上溯到地基**。
**都不挂 LCA 数据集、不算 GWP**(本版只做结构)。自带骨架展开引擎(剥 lca,产品无关)。

  Mode B(真实BOM):子系统层级来自【真实 BOM 的 System】,叶=探针映射到的骨架材料节点。
                    读 docs/<slug>-probe-graded.json(grade/claimed/node)+ /tmp/<slug>-bom-buckets.json(每桶的 system)。
  Mode A(具名车型):子系统层级来自【auto 骨架装配拓扑】(整车→总成→部件),叶=骨架材料输入。
                    从 auto BEV 原型根递归展开(--mode-a [root_gpid])。

产出:docs/<slug>-system-model.html + docs/<slug>-system-model.json。
用法: python3 scripts/build_product_model.py <slug>                # Mode B
      python3 scripts/build_product_model.py <slug> --mode-a [根GPID]  # Mode A(默认 auto BEV 根)
"""
import json, os, sys
from collections import defaultdict, Counter

import glob
# 自动扫描 docs/*-name-graph.json(新行业自动可用;不再硬编码遗漏 shipping/aviation/...)
INDUSTRIES = sorted(os.path.basename(f).replace('-name-graph.json', '') for f in glob.glob('docs/*-name-graph.json'))
DOCS = {i: f'docs/{i}-name-graph.json' for i in INDUSTRIES}


def load():
    G = {}
    for i, p in DOCS.items():
        if os.path.exists(p):
            G[i] = json.load(open(p))
    reg = json.load(open('registry/products.json'))['products'] if os.path.exists('registry/products.json') else {}
    IDX = {}
    for ind, g in G.items():
        pid = {p['id']: p for p in g['products']}
        name2id = {p['name']: p['id'] for p in g['products']}
        ref_prod = defaultdict(list)
        act = {a['id']: a for a in g['activities']}
        for e in g['edges']:
            if e['type'] == 'PRODUCES' and e.get('role') == 'reference':
                ref_prod[e['to']].append(e['from'])
        act_inputs = {a['id']: [name2id[nm] for nm in a.get('inputs', []) if nm in name2id] for a in g['activities']}
        IDX[ind] = dict(pid=pid, name2id=name2id, ref_prod=ref_prod, act=act, act_inputs=act_inputs)
    return G, reg, IDX


G, REG, IDX = load()


def home_of(gpid):
    e = REG.get(gpid) if isinstance(REG, dict) else None
    return (e['home_industry'], e['home_node']) if e else (None, None)


# ── 骨架展开引擎(剥掉 lca;沿 act_inputs/resolves_to 递归上溯到地基)──────────
# 内容级 memo:每个 (ind,pid) 的完整子树只算一次,后面所有出现处复用同一份结果
# (点开任意一处外购/跨行业节点都能看到它在母行业里的完整展开,而不是"折叠占位")。
# path 只用来判真环(节点是自己的祖先)——图里大量共享材料/活动是"汇聚"(DAG diamond)不是环,
# 靠 path 去重会把每条路径重新展开一遍、组合爆炸;靠 memo 每个节点只展开一次,规模 = O(唯一节点数)。
MAXD = 16
stats = Counter()
ind_nodes = defaultdict(set)
MEMO = {}


def expand(ind, pid, depth, path=frozenset()):
    key = (ind, pid)
    if key in path:
        return {'label': '↺ (见上,成环截断)', 'kind': 'fold', 'children': []}
    if key in MEMO:
        return MEMO[key]
    g = IDX.get(ind)
    if not g or pid not in g['pid']:
        node = {'label': f'?{ind}:{pid}', 'kind': 'missing', 'children': []}
        MEMO[key] = node
        return node
    p = g['pid'][pid]
    node = {'ind': ind, 'pid': pid, 'label': p['name'], 'kind': None, 'children': []}
    ind_nodes[ind].add(pid)
    if p.get('boundary') == 'background':
        rt = p.get('resolves_to')
        st = p.get('home_status')
        if rt and st in ('linked', 'internal'):
            hind, hnode = home_of(rt)
            node['kind'] = 'xlink'
            node['to'] = hind
            if depth >= MAXD or not hnode:
                node['children'] = [{'label': f'↺ {p["name"]} → {hind}', 'kind': 'fold', 'children': []}]
                MEMO[key] = node
                return node
            node['children'] = [expand(hind, hnode, depth + 1, path | {key})]
            MEMO[key] = node
            return node
        node['home'] = p.get('home_industry')
        node['kind'] = 'bg'
        stats['bg'] += 1
        MEMO[key] = node
        return node
    if depth >= MAXD:
        node['kind'] = 'fold'
        node['children'] = [{'label': '↺ (见上,达最大深度)', 'kind': 'fold', 'children': []}]
        MEMO[key] = node
        return node
    acts = g['ref_prod'].get(pid, [])
    node['kind'] = 'fg'
    node['routes'] = len(acts)
    if not acts:
        node['kind'] = 'leaf'
        MEMO[key] = node
        return node
    a = acts[0]
    for cand in acts:
        if g['act'][cand].get('confidence') == 'core':
            a = cand
            break
    node['act'] = g['act'][a]['name']
    newpath = path | {key}
    for ipid in g['act_inputs'].get(a, []):
        node['children'].append(expand(ind, ipid, depth + 1, newpath))
    stats['fg'] += 1
    MEMO[key] = node
    return node


# ── 组件身份 → 最佳骨架节点 + 定级(与 grade_bom_matches 同口径)────────────
FAMILY_KEYS = {'battery': ['chemistry'], 'aluminium': ['alloy_series'], 'steel': ['form_state'],
               'plastics': ['polymer_family'], 'glass': ['glass_type'], 'rubber': ['elastomer_type'],
               'copper': ['copper_form'], 'electronics': ['component_class'], 'chemicals': ['chemical_family'],
               'textiles': ['fiber_class'], 'nonferrous_metals': ['metal'], 'magnesium': ['magnesium_form']}
NULL = {None, '', 'na', 'n/a', 'none', 'null'}


def sup(fac, need):
    return bool(need) and all((fac or {}).get(k) == v for k, v in need.items())


def short_label(s, cap=32):
    """LLM 的整段身份描述 → 概括头(第一个【括号外】分隔符前),供节点名;全文留作 hover。"""
    s = str(s).strip()
    depth = 0
    cut = len(s)
    for i, c in enumerate(s):
        if c in '(（':
            depth += 1
        elif c in ')）':
            depth = max(0, depth - 1)
        elif depth == 0 and c in '：:—–。;；，,+' and i > 0:
            cut = i
            break
    s = s[:cut].strip()
    return s[:cap] + ('…' if len(s) > cap else '')


def resolve(ind, claimed, nid):
    if ind not in IDX:
        return (None, 'Unmatched')
    g = IDX[ind]
    claimed = {k: v for k, v in (claimed or {}).items() if str(v).strip().lower() not in NULL}
    if nid and nid in g['pid'] and sup(g['pid'][nid]['facets'], claimed):
        return (nid, 'Exact')
    for pid, p in g['pid'].items():
        if sup(p.get('facets', {}), claimed):
            return (pid, 'Exact')
    fk = FAMILY_KEYS.get(ind) or list(claimed)[:1]
    fam = {k: claimed[k] for k in fk if k in claimed}
    if fam:
        for pid, p in g['pid'].items():
            if sup(p.get('facets', {}), fam):
                return (pid, 'Coarse')
    if nid and nid in g['pid']:
        return (nid, 'Coarse')
    return (None, 'Gap')


# ── 整车系统中文名(Mode B 的 BOM System → 子系统层级)──────────────────────
SYS_CN = {'Engine': '动力系统(电池/电机/电控)', 'Body': '车身 & 外饰', 'Suspension': '悬架', 'Seats': '座椅',
          'Interior': '内饰', 'Transmission System': '传动(减速器/半轴)', 'Brakes Mechanism': '制动',
          'Electrical': '电气/线束', 'Heating System': '采暖', 'Accessories': '附件', 'Steering System': '转向',
          'Safety System': '安全系统', 'Air Conditioning System': '空调', 'Fluids': '油液',
          'Cooling System - Water': '冷却(水)', 'Pedals System': '踏板', '(none)': '未归类'}

# auto 受控 subsystem 的中文名(装配映射的跨车稳定主干)
AUTO_SS_CN = {'powertrain': '动力总成(电机/减速器/传动)', 'battery': '电池系统', 'body': '车身',
              'chassis': '底盘(悬架/制动/转向/车轮)', 'interior': '内饰/座椅', 'electrical_electronics': '电气电子',
              'exterior': '外饰', 'hvac': '空调', 'infotainment': '信息娱乐', 'safety_restraint': '安全约束',
              'thermal_management': '热管理', 'fluids': '油液', 'na': '其它/未归类'}

# ── 主 ─────────────────────────────────────────────────────────────────────
slug = sys.argv[1] if len(sys.argv) > 1 else 'tesla-model-x'
MODE_A = '--mode-a' in sys.argv
root_arg = None
if MODE_A:
    _i = sys.argv.index('--mode-a')
    if len(sys.argv) > _i + 1 and not sys.argv[_i + 1].startswith('-'):
        root_arg = sys.argv[_i + 1]

GRADE_MASS = defaultdict(float)
gaps_list = []

if MODE_A:
    # 根节点解析:支持两种形式
    #   ① GPID(经 registry/products.json 查 home_industry)── 历史用法,要求 root 已注册
    #   ② "industry::node_id"(如 shipping::P003)── 直读 docs/<industry>-name-graph.json,不依赖 registry
    # 后者解决新行业(shipping/aviation/...)的 GPID 尚未挂 registry 的现状。
    if root_arg:
        if '::' in root_arg:
            rind, rnode = root_arg.split('::', 1)
        else:
            rind, rnode = home_of(root_arg)
        if not rind or rind not in IDX or rnode not in IDX[rind]['pid']:
            sys.stderr.write(f"[err] root '{root_arg}' 无法解析:"
                             f"industry={rind} not in built graphs, 或 node={rnode} 不在该行业图。\n"
                             f"      可用行业: {', '.join(INDUSTRIES)}\n"
                             f"      用法: --mode-a <industry>::<node_id>  (例如 shipping::P003)\n"
                             f"           或 --mode-a <GPID>(需先在 registry/products.json 注册)\n")
            sys.exit(2)
    else:
        rind, rnode = 'auto', IDX['auto']['name2id'].get('整车, 纯电(BEV)')
    rootexp = expand(rind, rnode, 0)
    tree = {'label': slug, 'kind': 'root', 'mass': 0, 'children': rootexp.get('children', [rootexp])}
    TOTAL = 0
    ncomp = len(rootexp.get('children', []))
    mode_label = 'Mode A · 骨架原型驱动'
    mode_sub = '整车 → 总成 → 部件 → 材料 → 地基(子系统层级来自 auto 骨架装配拓扑)'
else:
    # 子系统层级来自真实 BOM 的 System;叶=探针映射到的骨架材料节点
    gr = json.load(open(f'docs/{slug}-probe-graded.json'))
    rows = gr['rows']
    TOTAL = gr.get('total_kg', sum(r['kg'] for r in rows))
    bpath = f'/tmp/{slug}-bom-buckets.json'
    if not os.path.exists(bpath):
        sys.stderr.write(f"[err] 缺 {bpath} —— 先跑 prep_bom_buckets.py 生成桶(含每桶 system)\n")
        sys.exit(2)
    bk = json.load(open(bpath))
    bsys = {b['bucket_id']: b.get('system', '(none)') for b in bk['buckets']}
    bparts = {b['bucket_id']: b.get('part_names', []) for b in bk['buckets']}
    # 装配映射(第二层):组件 → auto 受控 subsystem(跨车稳定主干);缺则回退 BOM system
    amap = {}
    apath = f'docs/{slug}-assembly-map.json'
    if os.path.exists(apath):
        amap = {it['bucket_id']: it for it in json.load(open(apath)).get('items', [])}
    used_amap = bool(amap)
    by_sys = defaultdict(list)
    for r in sorted(rows, key=lambda x: -x['kg']):
        ind = r['industry']
        nid, grade = resolve(ind, r.get('claimed'), r.get('node'))
        GRADE_MASS[grade] += r['kg']
        kids = [expand(ind, nid, 2)] if nid else []
        if not nid:
            gaps_list.append((ind, r.get('identity')))
        a = amap.get(r['bucket_id'])
        # 这个组件实际打包了哪些真实 BOM 零件 —— 展现为可展开列表(诚实:由这些整合而来)
        pnames = bparts.get(r['bucket_id'], [])
        if len(pnames) >= 2:
            kids = [{'kind': 'parts', 'ind': None, 'label': f'由 {len(pnames)} 个真实零件打包整合',
                     'children': [{'kind': 'part', 'ind': None, 'label': nm, 'children': []} for nm in pnames]}] + kids
        ident = r.get('identity') or r.get('node') or '(组件)'
        comp = {'ind': ind, 'label': short_label(ident), 'full': ident,
                'kind': 'comp', 'grade': grade, 'mass': r['kg'], 'n_parts': len(pnames), 'children': kids}
        if a and a.get('component_type'):
            comp['ctype'] = a['component_type']
        ss = (a.get('subsystem') if a else None) or ('__bom__:' + bsys.get(r['bucket_id'], '(none)'))
        by_sys[ss].append(comp)
    groups = []
    for s in sorted(by_sys, key=lambda x: -sum(c['mass'] for c in by_sys[x])):
        comps = sorted(by_sys[s], key=lambda c: -c['mass'])
        label = SYS_CN.get(s[6:], s[6:]) if s.startswith('__bom__:') else AUTO_SS_CN.get(s, s)
        groups.append({'ind': None, 'sys': s, 'label': label, 'kind': 'group',
                       'mass': sum(c['mass'] for c in comps), 'children': comps})
    tree = {'label': slug, 'kind': 'root', 'mass': TOTAL, 'children': groups}
    ncomp = len(rows)
    mode_label = 'Mode B · 真实 BOM 驱动'
    mode_sub = ('整车 → 子系统(auto 受控 subsystem)→ 组件 → 骨架材料 → 地基'
                if used_amap else '整车 → 子系统(BOM System 回退)→ 组件 → 骨架材料 → 地基')

total_nodes = sum(len(v) for v in ind_nodes.values())


_HEIGHT_CACHE = {}


def height(n):
    """子树高度,按对象身份(id)缓存——MEMO 后共享子树是同一对象,只算一次(否则每个出现处都重新递归)。"""
    key = id(n)
    if key in _HEIGHT_CACHE:
        return _HEIGHT_CACHE[key]
    kids = n.get('children', [])
    h = 1 + max((height(c) for c in kids), default=-1) if kids else 0
    _HEIGHT_CACHE[key] = h
    return h


md = height(tree)
nind = len([i for i in ind_nodes if ind_nodes[i]])
n_sub = len(tree['children'])
print(f"=== 产品系统模型(无数值)— {slug} · {mode_label} ===")
print(f"{mode_sub}")
print(f"子系统 {n_sub} · {'组件' if not MODE_A else '顶层总成'} {ncomp} · 展开骨架节点(去重) {total_nodes} · 树深 {md} · 跨 {nind} 行业图 · 断点/占位 {len(gaps_list) or stats.get('bg', 0)}")
if not MODE_A:
    print("组件级覆盖(质量): " + " · ".join(f"{g} {GRADE_MASS[g]:.0f}kg({100*GRADE_MASS[g]/TOTAL:.0f}%)"
                                        for g in ['Exact', 'Coarse', 'Gap', 'Unmatched'] if GRADE_MASS.get(g)))

# ── 交互式系统树 HTML(两模式同一渲染器;无 📊,组件带 E/C/G)─────────────────
COLOR = {'steel': '#8a8d91', 'power': '#e6b800', 'auto': '#c0392b', 'aluminium': '#5dade2', 'battery': '#27ae60',
         'electronics': '#8e44ad', 'chemicals': '#e67e22', 'copper': '#b9770e', 'plastics': '#16a085',
         'glass': '#48c9b0', 'rubber': '#5d6d7e', 'mining': '#7f8c8d', 'nonferrous_metals': '#a569bd',
         'magnesium': '#52be80', 'textiles': '#d98880', 'agriculture': '#82e0aa'}
GC = {'Exact': '#2ecc71', 'Coarse': '#e6b800', 'Gap': '#e07b39', 'Unmatched': '#888'}


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;')


def kg(m):
    return f'<span class=kg>{m:.1f} kg</span>' if m else ''


# 同一个 (ind,pid) 节点(fg/xlink,来自骨架 expand 的内容级 memo)可能在树里出现在很多位置——
# 只把【第一次出现】完整渲染 + 存进 <template> 库,其余出现处只放一个懒展开占位,
# 点开时用 JS 从模板克隆内容填进去(避免同一份子树在文件里被物理复制几百次)。
RENDERED = set()
TEMPLATES = {}


def hnode(n):
    k = n.get('kind')
    ind = n.get('ind', '')
    pid = n.get('pid')
    col = COLOR.get(ind, '#888')
    dot = f'<span class=dot style="background:{col}"></span>' if ind else ''
    lab = esc(n['label'])
    mass = kg(n.get('mass', 0))
    badge = ''
    if k == 'comp':
        gr_ = n.get('grade', '')
        badge = f'<span class=gb style="color:{GC.get(gr_, "#888")};border-color:{GC.get(gr_, "#888")}">{gr_}</span>'
        if n.get('ctype'):
            badge += f'<span class=ct>{esc(n["ctype"])}</span>'
        if n.get('n_parts', 0) >= 2:
            badge += f'<span class=np>{n["n_parts"]}件</span>'
    elif k == 'xlink':
        badge = f'<span class=x>⇒ {n.get("to")}</span>'
    elif k == 'bg':
        badge = '<span class=bg>⛰ 地基/背景</span>'
    elif k == 'gap':
        badge = '<span class=g>⬜ 无节点(断点)</span>'
    elif k == 'fold':
        badge = '<span class=f>↺</span>'
    elif k == 'fg' and n.get('routes', 0) > 1:
        badge = f'<span class=r>{n["routes"]}路线</span>'
    if k == 'root':
        lab = f'🚗 {lab}'
    if k == 'group':
        lab = f'<span class=sysmark>▣</span> <b>{lab}</b>'
    if k == 'comp':
        lab = f'<b>{lab}</b>'
    if k == 'parts':
        lab = f'📦 {lab}'
    kids = n.get('children', [])
    tip = (' title="' + esc(n['full']).replace('"', '&quot;') + '"') if n.get('full') else ''
    key = f'{ind}:{pid}' if (ind and pid and k in ('fg', 'xlink')) else None
    if key and kids:
        if key in RENDERED:
            return (f'<li class="k-{k} k-lazy" data-ref="{esc(key)}"><details><summary{tip}>'
                    f'{dot}{lab} {mass}{badge}<span class=lazytag>⇢ 点开展开</span></summary><ul></ul></details></li>')
        RENDERED.add(key)
        inner = "".join(hnode(c) for c in kids)
        TEMPLATES[key] = inner
        return f'<li class="k-{k}"><details><summary{tip}>{dot}{lab} {mass}{badge}</summary><ul>{inner}</ul></details></li>'
    if kids:
        op = ' open' if k in ('root', 'group') else ''
        return (f'<li class="k-{k}"><details{op}><summary{tip}>{dot}{lab} {mass}{badge}</summary>'
                f'<ul>{"".join(hnode(c) for c in kids)}</ul></details></li>')
    return f'<li class="k-{k} leaf"{tip}>{dot}{lab} {mass}{badge}</li>'


tree_html = hnode(tree)  # 副作用:填充 RENDERED / TEMPLATES(第一次出现渲染,重复出现处只留占位)
tpl_lib = ''.join(f'<template id="tpl-{esc(key)}">{inner}</template>' for key, inner in TEMPLATES.items())

legend = ''.join(f'<span class=lg><span class=dot style="background:{c}"></span>{i}</span>'
                 for i, c in COLOR.items() if ind_nodes.get(i))
gbars = ''
if not MODE_A and GRADE_MASS:
    gmax = max(GRADE_MASS.values())
    for gname in ['Exact', 'Coarse', 'Gap', 'Unmatched']:
        v = GRADE_MASS.get(gname, 0)
        if not v:
            continue
        gbars += (f'<div class=gr-row><span class=gr-l style="color:{GC[gname]}">{gname}</span>'
                  f'<span class=gr-bw><span class=gr-bar style="width:{100*v/gmax:.0f}%;background:{GC[gname]}"></span></span>'
                  f'<span class=gr-v>{v:.0f}kg · {100*v/TOTAL:.0f}%</span></div>')

mass_kpi = f'<span class=stat>整车 <b>{TOTAL:.0f}</b> kg</span>' if not MODE_A else ''
comp_kpi = (f'<span class=stat>{"组件" if not MODE_A else "顶层总成"} <b>{ncomp}</b></span>')
grade_blk = (f'<h3>组件级映射质量(质量加权)</h3>{gbars}') if not MODE_A else \
    '<h3>说明</h3><div class=note>Mode A 是骨架原型结构,无 BOM、无逐件质量、无 Exact/Coarse/Gap(那是 Mode B 喂真 BOM 才有的覆盖判级)。</div>'

html = f'''<!doctype html><html lang=zh><meta charset=utf-8><title>{slug} — 产品系统模型({mode_label})</title>
<style>
body{{font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;margin:0;background:#0f1115;color:#d8dadf}}
header{{padding:16px 24px;background:#161922;border-bottom:1px solid #262a35;position:sticky;top:0;z-index:9}}
h1{{margin:0 0 4px;font-size:18px}} .sub{{color:#9aa0ab;font-size:13px}}
.mode{{display:inline-block;font-size:11px;font-weight:700;color:#c9aee6;background:#1d1730;border:1px solid #3a2b56;border-radius:5px;padding:2px 8px;margin-left:6px}}
.stat{{display:inline-block;margin:8px 14px 0 0;background:#1c2030;padding:4px 10px;border-radius:6px;font-size:12px}} .stat b{{color:#fff;font-size:15px}}
.wrap{{display:flex}} .tree{{flex:1;padding:16px 24px;overflow:auto}}
.side{{width:330px;padding:16px;border-left:1px solid #262a35;background:#12151c;position:sticky;top:0;align-self:flex-start;max-height:100vh;overflow:auto}}
ul{{list-style:none;margin:0;padding-left:18px;border-left:1px dashed #2c313d}} li{{margin:2px 0}}
summary{{cursor:pointer;padding:1px 4px;border-radius:4px;list-style:none}} summary:hover{{background:#1c2030}}
summary::-webkit-details-marker{{display:none}} summary::before{{content:'▸';margin-right:5px;color:#6b7280;transition:.15s;display:inline-block}}
details[open]>summary::before{{transform:rotate(90deg)}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}}
.sysmark{{color:#5dade2;font-size:11px}}
.leaf{{padding-left:4px;color:#aeb4bf}}
.kg{{color:#7fd0ff;font-size:11px;margin-left:6px;font-variant-numeric:tabular-nums}}
.k-group>details>summary{{font-size:15px;background:#191d28;border-radius:5px}} .k-root>details>summary{{font-size:16px}}
.k-comp>details>summary,.k-comp.leaf{{background:#15191f;border-radius:5px}}
.gb{{font-size:10px;margin-left:6px;border:1px solid;border-radius:3px;padding:0 5px;font-weight:700}}
.ct{{font-size:10px;margin-left:5px;color:#9fb6c9;background:#14202b;border:1px solid #284055;border-radius:3px;padding:0 5px}}
.np{{font-size:10px;margin-left:5px;color:#d7b06a;background:#2a2410;border:1px solid #4a3f1c;border-radius:3px;padding:0 5px}}
.k-parts>details>summary{{color:#9aa0ab;font-size:12px}} .k-parts>details>summary:hover{{background:#1c2030}}
.k-part{{color:#8a909b;font-size:11.5px;padding-left:4px}} .k-part::before{{content:'· ';color:#555}}
.x{{color:#5dade2;font-size:11px;margin-left:4px}} .bg{{color:#7fe3bd;font-size:11px;margin-left:4px}}
.g{{color:#e07b39;font-size:11px;margin-left:4px}} .f{{color:#555}}
.r{{color:#27ae60;font-size:11px;margin-left:4px;border:1px solid #27ae60;border-radius:3px;padding:0 4px}}
.lg{{display:inline-block;margin:0 10px 4px 0;font-size:11px;color:#9aa0ab}}
h3{{font-size:13px;margin:14px 0 6px;color:#cfd3da}} .note{{font-size:11.5px;color:#8a909b;margin:6px 0}}
.kv{{display:flex;justify-content:space-between;font-size:12px;padding:3px 0;border-bottom:1px solid #20242e}} .kv b{{color:#fff}}
.gr-row{{display:flex;align-items:center;font-size:11.5px;margin:3px 0;gap:8px}} .gr-l{{width:74px;font-weight:700}}
.gr-bw{{flex:1;background:#1c2030;border-radius:3px;height:11px}} .gr-bar{{display:block;height:11px;border-radius:3px}}
.gr-v{{width:92px;text-align:right;color:#9aa0ab;font-variant-numeric:tabular-nums}}
.ctl{{font-size:12px;margin-bottom:8px}} .ctl a{{color:#5dade2;cursor:pointer;margin-right:12px}}
.lazytag{{font-size:10px;margin-left:6px;color:#6b7280}} .k-lazy>details>summary:hover .lazytag{{color:#5dade2}}
</style>
<header>
<h1>🌳 {slug} — 产品系统模型 <span class=mode>{mode_label}</span> <span class=sub>无数值 / 不挂 LCA 数据集</span></h1>
<div class=sub>{mode_sub}。徽章=组件映射质量 E/C/G;⛰=背景/地基叶;⇒=跨行业;↺=去重折叠</div>
{mass_kpi}{comp_kpi}<span class=stat>子系统 <b>{n_sub}</b></span><span class=stat>展开节点 <b>{total_nodes}</b></span>
<span class=stat>树深 <b>{md}</b></span><span class=stat>跨 <b>{nind}</b> 行业图</span>
<div style="margin-top:6px">{legend}</div>
</header>
<div class=wrap><div class=tree>
<div class=ctl><a onclick="document.querySelectorAll('.tree details').forEach(d=>d.open=true)">展开全部</a><a onclick="document.querySelectorAll('.tree details').forEach(d=>d.open=false)">折叠全部</a></div>
<ul style="border:none;padding-left:0">{tree_html}</ul>
</div><div class=side>
<h3>模型概况</h3>
<div class=kv><span>建模模式</span><b>{mode_label}</b></div>
{'<div class=kv><span>整车质量</span><b>'+f'{TOTAL:.0f} kg</b></div>' if not MODE_A else ''}
<div class=kv><span>子系统</span><b>{n_sub}</b></div>
<div class=kv><span>{'组件(BOM叶)' if not MODE_A else '顶层总成'}</span><b>{ncomp}</b></div>
<div class=kv><span>展开骨架节点(去重)</span><b>{total_nodes}</b></div>
<div class=kv><span>树深</span><b>{md}</b></div>
<div class=kv><span>跨行业图</span><b>{nind}</b></div>
{grade_blk}
<h3>形状说明</h3>
<div class=note><b>整车 → 子系统 → 组件 → 骨架材料 → 地基。</b> Mode A 子系统来自骨架装配拓扑、Mode B 来自真实 BOM,两者同形。本版只到身份层,不挂数据集、不算数值。</div>
</div></div>
<div id=tpl-lib style="display:none">{tpl_lib}</div>
<script>
document.addEventListener('toggle', function(e){{
  var det = e.target;
  if (det.tagName !== 'DETAILS' || !det.open) return;
  var li = det.parentElement;
  if (!li || !li.classList.contains('k-lazy')) return;
  var ul = det.querySelector('ul');
  if (!ul || ul.dataset.filled) return;
  var tpl = document.getElementById('tpl-' + li.getAttribute('data-ref'));
  if (tpl) {{ ul.innerHTML = tpl.innerHTML; ul.dataset.filled = '1'; }}
}}, true);
</script>
</html>'''
open(f'docs/{slug}-system-model.html', 'w').write(html)


# ── JSON 同样要避免重复子树物理膨胀:重复出现的 (ind,pid) 只留 {{'ref': key}} 占位 ──
JSEEN = set()


def jdedupe(n):
    ind, pid, k = n.get('ind'), n.get('pid'), n.get('kind')
    key = f'{ind}:{pid}' if (ind and pid and k in ('fg', 'xlink')) else None
    if key and key in JSEEN:
        return {'kind': k, 'ind': ind, 'pid': pid, 'label': n.get('label'), 'ref': key}
    if key:
        JSEEN.add(key)
    out = {kk: vv for kk, vv in n.items() if kk != 'children'}
    out['children'] = [jdedupe(c) for c in n.get('children', [])]
    return out


json.dump({'root': slug, 'total_kg': TOTAL, 'mode': 'A' if MODE_A else 'B',
           'stats': {'subsystems': n_sub, 'components': ncomp, 'nodes': total_nodes, 'depth': md, 'industries': nind},
           'grade_mass': dict(GRADE_MASS), 'tree': jdedupe(tree)},
          open(f'docs/{slug}-system-model.json', 'w'), ensure_ascii=False, indent=1)
print(f"→ docs/{slug}-system-model.html ({len(html)//1024}KB) + docs/{slug}-system-model.json")
