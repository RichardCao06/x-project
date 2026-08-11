#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨行业 GPID 绑定:Step 1 (prep) —— 给某行业的所有 background 节点准备提名输入。

输出 /tmp/<slug>-cross-link-input.json,内含:
  · pending  : 待绑 bg 节点(剔除已 home_status=linked 的)
                每条:{src_id, name, cpc, hs, target_industry, provenance, facets, prov_hint}
                prov_hint = 从 provenance 字符串里解析出的候选目标 GPID(零 LLM 启发式)
  · index    : 各 target_industry 的【前景节点精简索引】(id+name+facets),供 LLM 在该范围内提名
  · summary  : 统计(by industry / by prov_hit)

用法:python3 scripts/prep_cross_link.py <slug>      # 例 shipping
"""
import json, sys, os, re
from collections import defaultdict

slug = sys.argv[1] if len(sys.argv) > 1 else 'shipping'
src_path = f'docs/{slug}-name-graph.json'
if not os.path.exists(src_path):
    sys.exit(f'❌ {src_path} 不存在')

src = json.load(open(src_path))

# --- 1. 收集待绑 bg 节点 ---------------------------------------------------
bg = [p for p in src['products']
      if p.get('boundary') == 'background' and p.get('home_status') != 'linked']
# 字符串启发式:provenance 里若出现 "<industry>-name-graph P\d+" 就提取
PROV_RE = re.compile(r'([a-z_]+)-name-graph[ ]+(P\d+)')
def prov_hint(prov_list):
    for s in prov_list or []:
        m = PROV_RE.search(s)
        if m:
            return f'{m.group(1)}::{m.group(2)}'
    return None

# 按 target_industry 分组(供切片只导出用到的)
by_target = defaultdict(list)
pending = []
for p in bg:
    ti = p.get('home_industry')
    if not ti:
        continue  # 没指定目标,跳(应该 0,但稳)
    by_target[ti].append(p)
    pending.append({
        'src_id': p['id'],
        'name': p['name'],
        'cpc': p.get('cpc'),
        'hs': p.get('hs'),
        'target_industry': ti,
        'provenance': p.get('provenance', []),
        'facets_src': p.get('facets', {}),
        'prov_hint': prov_hint(p.get('provenance')),
    })

# --- 2. 各 target_industry 的精简索引(只前景产品)-------------------------
def slim_index(ind_slug):
    p = f'docs/{ind_slug}-name-graph.json'
    if not os.path.exists(p):
        return []
    g = json.load(open(p))
    out = []
    for prod in g['products']:
        if prod.get('boundary') == 'background':
            continue
        out.append({
            'id': prod['id'],
            'name': prod['name'],
            'facets': prod.get('facets', {}),
        })
    return out

index = {ti: slim_index(ti) for ti in by_target}

# --- 3. 统计 ---------------------------------------------------------------
prov_hit = sum(1 for p in pending if p['prov_hint'])
summary = {
    'slug': slug,
    'total_bg_pending': len(pending),
    'by_target': {ti: len(v) for ti, v in sorted(by_target.items(), key=lambda x: -len(x[1]))},
    'prov_hint_hits': prov_hit,
    'prov_hint_rate': f'{prov_hit*100/len(pending):.0f}%' if pending else '0%',
    'index_sizes': {ti: len(v) for ti, v in index.items()},
}

out = {'pending': pending, 'index': index, 'summary': summary}
op = f'/tmp/{slug}-cross-link-input.json'
json.dump(out, open(op, 'w'), ensure_ascii=False, indent=1)

print(f'✅ {op} ({os.path.getsize(op)//1024}KB)')
print(f'   待绑 bg: {len(pending)} · provenance 命中: {prov_hit} ({summary["prov_hint_rate"]})')
for ti, n in summary['by_target'].items():
    nidx = summary['index_sizes'][ti]
    print(f'   · {ti}: {n} 待绑 vs {nidx} 候选前景')
