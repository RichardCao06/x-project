#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""grade_bom_matches.py — BOM 探针的【确定性裁决 = GATE】(无 LLM,可原子重放)。

读 LLM 冻结的 match 表 + BOM 桶(取质量)+ 真母图,逐条查 facet 定 Exact/Coarse/Gap,质量加权汇总。
定级口径(对真图查 facet):
  Exact  = ∃ 节点 facets ⊇ claimed_facets(牌号/形态/化学全吻合)
  Coarse = 非 Exact,但 ∃ 节点匹配 claimed 在【主族键 FAMILY_KEYS】上的子集(同族代理在,GWP 会偏)
  Gap    = 同族都没有 → 真·结构缺口

**LLM 的 verdict_hint 完全不读**——只入审计列。代码独立查真图说了算(可复现、可证伪、可当 gate)。
补节点后【只重跑本脚本(零 LLM)】即可让相应桶确定性翻档。

用法: python3 scripts/grade_bom_matches.py <matches.json|wf-return.json> <vehicle> [buckets.json]
退出码 0。
"""
import json, os, sys
from collections import defaultdict

INDS = ['steel', 'aluminium', 'battery', 'electronics', 'chemicals', 'copper', 'plastics', 'glass', 'rubber',
        'textiles', 'nonferrous_metals', 'magnesium']
G = {i: json.load(open(f"docs/{i}-name-graph.json")) for i in INDS if os.path.exists(f"docs/{i}-name-graph.json")}

# 主族键(同族代理判据):exact 全键吻合;否则 claimed 在这些键上的子集若有节点 → Coarse
FAMILY_KEYS = {
    'battery': ['chemistry'], 'aluminium': ['alloy_series'], 'steel': ['form_state'],
    'plastics': ['polymer_family'], 'glass': ['glass_type'], 'rubber': ['elastomer_type'],
    'copper': ['copper_form'], 'electronics': ['component_class'], 'chemicals': ['chemical_family'],
    'textiles': ['fiber_class'], 'nonferrous_metals': ['metal'], 'magnesium': ['magnesium_form'],
}
NULLISH = (None, '', 'na', 'n/a', 'none', 'null')


def prods(ind):
    return G[ind]['products'] if ind in G else []


def node_by_id(ind, nid):
    for p in prods(ind):
        if p.get('id') == nid:
            return p
    return None


def superset(fac, need):
    fac = fac or {}
    return bool(need) and all(fac.get(k) == v for k, v in need.items())


def clean(facets):
    return {k: v for k, v in (facets or {}).items() if str(v).strip().lower() not in NULLISH}


def has_any(ind, need):
    return any(superset(p.get('facets', {}), need) for p in prods(ind))


def grade(m):
    ind = m.get('industry')
    if ind not in G:
        return ('Unmatched', f'母图 {ind} 不存在')
    claimed = clean(m.get('claimed_facets'))
    nid = m.get('candidate_node_id') or ''
    node = node_by_id(ind, nid) if nid else None
    # Exact:提名节点全吻合,或全图任一节点 facets⊇claimed
    if node and superset(node.get('facets', {}), claimed):
        return ('Exact', node['name'])
    if claimed and has_any(ind, claimed):
        return ('Exact', '(facet 全匹配)')
    # Coarse:主族键子集存在
    fk = FAMILY_KEYS.get(ind) or list(claimed.keys())[:1]
    fam = {k: claimed[k] for k in fk if k in claimed}
    if fam and has_any(ind, fam):
        desc = ', '.join(f'{k}={v}' for k, v in fam.items())
        return ('Coarse', f'同族在({desc}),牌号/形态/化学不符')
    return ('Gap', f'同族缺(候选 {nid or "—"})')


def main():
    mpath = sys.argv[1]
    vehicle = sys.argv[2] if len(sys.argv) > 2 else 'tesla-model-x'
    bpath = sys.argv[3] if len(sys.argv) > 3 else f"/tmp/{vehicle}-bom-buckets.json"

    raw = json.load(open(mpath))
    matches = raw['matches'] if isinstance(raw, dict) and 'matches' in raw else raw
    if not isinstance(matches, list):
        sys.stderr.write("[grade] ❌ 找不到 matches 列表\n")
        return 2

    bk = json.load(open(bpath))
    TOTAL = bk['total_kg']
    mass = {b['bucket_id']: b['mass_kg'] for b in bk['buckets']}

    by_grade = defaultdict(float)
    by_ind = defaultdict(lambda: defaultdict(float))
    rows = []
    seen = set()
    for m in matches:
        bidv = m.get('bucket_id')
        w = mass.get(bidv, 0.0)
        seen.add(bidv)
        gr, note = grade(m)
        by_grade[gr] += w
        by_ind[m.get('industry')][gr] += w
        rows.append(dict(bucket_id=bidv, kg=round(w, 2), grade=gr, industry=m.get('industry'),
                         identity=m.get('inferred_identity'), node=m.get('candidate_node_id'),
                         hint=m.get('verdict_hint'), note=note, claimed=m.get('claimed_facets')))

    miss = sum(w for b, w in mass.items() if b not in seen)
    tail = bk.get('tail_kg', 0.0)
    by_grade['Unmatched'] += miss + tail

    E, C, Gp = by_grade['Exact'], by_grade['Coarse'], by_grade['Gap']
    print(f"=== BOM 探针裁决 — {vehicle} ({TOTAL:.0f} kg) — LLM 提名 + 代码裁决 ===")
    for g in ['Exact', 'Coarse', 'Gap', 'Unmatched']:
        v = by_grade.get(g, 0.0)
        print(f"  {g:9s} {v:7.0f} kg  ({100*v/TOTAL:4.1f}%)")
    print(f"\n  诚实覆盖(仅 Exact): {100*E/TOTAL:.1f}%   |   含同族代理(Exact+Coarse): {100*(E+C)/TOTAL:.1f}%")
    print(f"  对照: build_tesla_x 自报 84.7% / detect 牌号级 Exact 47.9%")

    for sect, tag in [('Coarse', '同族代理, 牌号/形态/化学不符 — 旧口径误记成已覆盖的'), ('Gap', '同族都没有 — 真·结构缺口')]:
        items = sorted([r for r in rows if r['grade'] == sect], key=lambda x: -x['kg'])
        print(f"\n=== {sect} ({tag}) ===")
        for r in items:
            print(f"  {r['kg']:6.1f} kg  [{r['industry']}] {r['identity']}  → {r['note']}  (node={r['node']}, hint={r['hint']})")

    out = dict(vehicle=vehicle, total_kg=TOTAL, by_grade={k: round(v, 2) for k, v in by_grade.items()},
               honest_coverage_pct=round(100 * E / TOTAL, 1),
               with_proxy_pct=round(100 * (E + C) / TOTAL, 1),
               by_industry={i: {k: round(v, 2) for k, v in d.items()} for i, d in by_ind.items()},
               rows=rows)
    json.dump(out, open(f"docs/{vehicle}-probe-graded.json", "w"), ensure_ascii=False, indent=1)
    print(f"\n→ docs/{vehicle}-probe-graded.json")
    return 0


if __name__ == '__main__':
    sys.exit(main())
