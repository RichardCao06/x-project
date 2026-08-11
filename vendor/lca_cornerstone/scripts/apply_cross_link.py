#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨行业 GPID 绑定:Step 3 (apply) —— 把 LLM 提名结果代码裁决 + 写回源图。

输入:
  /tmp/<slug>-cross-link-nominations.json  (LLM 输出,见 prep_cross_link.py)
  docs/<slug>-name-graph.json              (要写回的源图)
  docs/<target>-name-graph.json            (各 target 行业图,用其 product.gpid 字段查 GPID)

逻辑(代码裁决,零 LLM):
  for each nomination:
    · verdict_hint = no_node          → skip (留 placeholder/null)
    · target_id 不在 target 图        → skip (verdict='Gap',LLM 提了不存在的)
    · target 节点的 gpid 为空         → 标记 'pending_target_gpid'(target 行业未跑 assign_gpid)
    · gpid 有                         → 写回 home_status='linked' + resolves_to=gpid

用法:python3 scripts/apply_cross_link.py <slug> [--dry-run]
"""
import json, sys, os
from collections import Counter

slug = sys.argv[1] if len(sys.argv) > 1 else 'shipping'
DRY = '--dry-run' in sys.argv

nom_path = f'/tmp/{slug}-cross-link-nominations.json'
src_path = f'docs/{slug}-name-graph.json'
if not os.path.exists(nom_path):
    sys.exit(f'❌ {nom_path} 不存在 —— 先派 LLM 提名 agent')
if not os.path.exists(src_path):
    sys.exit(f'❌ {src_path} 不存在')

noms = json.load(open(nom_path))['nominations']
src = json.load(open(src_path))
src_by_id = {p['id']: p for p in src['products']}

# 缓存 target 行业图(按需懒加载)
target_cache = {}
def target_node(ti, tid):
    if ti not in target_cache:
        p = f'docs/{ti}-name-graph.json'
        if not os.path.exists(p):
            target_cache[ti] = None
            return None
        g = json.load(open(p))
        target_cache[ti] = {prod['id']: prod for prod in g['products']}
    if not target_cache[ti]:
        return None
    return target_cache[ti].get(tid)

stats = Counter()
fail_examples = []

for n in noms:
    src_id = n['src_id']
    if src_id not in src_by_id:
        stats['bad_src_id'] += 1
        continue
    src_node = src_by_id[src_id]
    ti = src_node.get('home_industry')
    target_id = n.get('target_id')
    hint = n.get('verdict_hint', 'no_node')

    if hint == 'no_node' or not target_id:
        stats['Gap_no_node'] += 1
        continue
    if not ti:
        stats['Gap_no_target_industry'] += 1
        continue

    tn = target_node(ti, target_id)
    if tn is None:
        stats['Gap_bad_target_id'] += 1
        if len(fail_examples) < 5:
            fail_examples.append(f'{src_id} → {ti}::{target_id} (不存在)')
        continue
    if tn.get('boundary') == 'background':
        stats['Gap_target_is_background'] += 1
        continue

    gpid = tn.get('gpid')
    if not gpid:
        # 目标行业未跑 assign_gpid → 留 placeholder + 记 pending
        src_node['home_status'] = 'placeholder'
        src_node['resolves_to'] = None
        src_node['pending_target_gpid'] = f'{ti}::{target_id}'
        stats['pending_target_gpid'] += 1
        continue

    # 成功绑
    verdict = 'Coarse' if hint == 'proxy' else 'Exact'
    src_node['home_status'] = 'linked'
    src_node['resolves_to'] = gpid
    src_node['cross_link_verdict'] = verdict
    src_node.pop('pending_target_gpid', None)  # 清掉旧 pending
    stats[verdict] += 1

# 报告
print(f'── 裁决统计({slug} · 共 {sum(stats.values())} 条提名 · {"DRY-RUN" if DRY else "WRITE"})──')
for k, v in sorted(stats.items(), key=lambda x: -x[1]):
    print(f'  {k:30s} {v:3d}')
linked = stats.get('Exact', 0) + stats.get('Coarse', 0)
total_noms = sum(stats.values())
print(f'\n  ✅ 实际可绑: {linked}/{total_noms} ({linked*100//total_noms if total_noms else 0}%)')
print(f'  ⏸ pending(target 行业无 gpid,待 assign_gpid): {stats.get("pending_target_gpid", 0)}')
print(f'  ❌ Gap: {stats.get("Gap_no_node", 0) + stats.get("Gap_bad_target_id", 0)}')

if fail_examples:
    print(f'\n  Gap 示例:')
    for ex in fail_examples:
        print(f'    {ex}')

if DRY:
    print(f'\n  [dry-run] 不写回 {src_path}')
else:
    # 备份 + 写回
    backup = src_path + '.bak'
    if not os.path.exists(backup):
        json.dump(json.load(open(src_path)), open(backup, 'w'), ensure_ascii=False, indent=1)
        print(f'\n  💾 备份: {backup}')
    json.dump(src, open(src_path, 'w'), ensure_ascii=False, indent=1)
    print(f'  ✏️  已写回: {src_path}')
    print(f'\n  下一步:跑 build_product_model.py oceanic-bulker-capesize --mode-a shipping::P003 看效果对比')
