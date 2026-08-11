#!/usr/bin/env python3
"""KU + Provenance 确定性蒸馏(零 LLM)。对 wiki-ku-provenance workflow 冻结的裁决结果按规则落库,
不信任何 verdict 之外的自我声明——authority 完全由 verify.verdict 派生,不可绕过。

用法: python3 scripts/ku_distill.py <frozen_claims.json> <out_ku.json>
<frozen_claims.json> 取自 workflow 输出的 .result.claims(同 bom-skeleton-probe 的"冻结提名表"模式,
先落盘再裁决,裁决可在冻结文件上无限重放)。
"""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def ku_id(node_id, claim_text):
    # 小写 ku- 前缀:必须匹配 wiki_lint 的 CITE 正则 [a-z0-9\-]+,否则渲染出的 [^KU-xxx] 解析不到
    return 'ku-' + hashlib.sha1(f'{node_id}|{claim_text}'.encode('utf-8')).hexdigest()[:16]


def distill(claims):
    kus = []
    for c in claims:
        claim = c['claim']
        fetch = c.get('fetchResult') or {}
        verify = c['verify']
        v = verify['verdict']
        claim_kind = claim.get('claim_kind')
        if claim_kind not in {'external_fact', 'internal_graph_fact', 'modeling_judgment', 'evidence_gap'}:
            raise ValueError(f"{claim.get('claim_id','?')} 缺少或使用非法 claim_kind")
        if claim_kind != 'external_fact' and v != 'NOT_FOUND':
            raise ValueError(f"{claim.get('claim_id','?')} 内部 claim 不得蒸馏为外部核验 verdict={v}")

        if v == 'CONFIRMED':
            authority = 'reviewed'
            prov = {
                'kind': 'WEB_URL',
                'ref': fetch.get('url', ''),
                'locator': claim.get('believed_locator', ''),
                'quote': verify.get('supporting_quote') or fetch.get('excerpt', ''),
                'retrievable': True,
            }
        elif v == 'CONTRADICTED':
            # 不是"引用失败"，是"抓到的材料明确唱反调"——这可能意味着节点本身的
            # facet/断言有问题，必须进人工台账，不能悄悄归零或悄悄采纳
            authority = 'contradicted'
            prov = {
                'kind': 'WEB_URL',
                'ref': fetch.get('url', ''),
                'locator': claim.get('believed_locator', ''),
                'quote': fetch.get('excerpt', ''),
                'retrievable': True,
            }
        else:  # NOT_FOUND / INSUFFICIENT
            authority = 'draft'
            if claim_kind == 'internal_graph_fact':
                prov = {
                    'kind': 'INTERNAL_GRAPH', 'ref': 'LCA-CORNERSTONE_GRAPH',
                    'locator': claim.get('believed_locator', ''), 'quote': None,
                    'retrievable': True,
                }
            elif claim_kind in {'modeling_judgment', 'evidence_gap'}:
                prov = {
                    'kind': 'INTERNAL_REVIEW', 'ref': 'INTERNAL_MODELING_JUDGMENT',
                    'locator': claim.get('believed_locator', ''), 'quote': None,
                    'retrievable': True,
                }
            else:
                prov = {
                    'kind': 'UNVERIFIED_RECALL',
                    'ref': claim.get('believed_source', ''),
                    'locator': claim.get('believed_locator', ''),
                    'quote': None,
                    'retrievable': False,
                }

        kus.append({
            'ku_id': ku_id(claim['node_id'], claim['claim_text']),
            'claim_id': claim.get('claim_id', ''),
            'requirement_id': claim.get('requirement_id', ''),
            'node_id': claim['node_id'],
            'industry': claim['industry'],
            'section': claim['section'],
            'claim_text': claim['claim_text'],
            'claim_kind': claim_kind,
            'claim_role': claim.get('claim_role', 'research_claim'),
            'evidence_claim_ids': claim.get('evidence_claim_ids', []),
            'rhetorical_role': claim.get('rhetorical_role', ''),
            'paragraph_focus': claim.get('paragraph_focus', ''),
            'authority': authority,
            # repair 模式的老断言没有 attribution_confidence(那是 Extract 模式模型自评字段),缺省即可
            'attribution_confidence': claim.get('attribution_confidence', 'n/a'),
            'old_tags': claim.get('old_tags', []),
            'believed_source': claim.get('believed_source', ''),
            'source_anchor': {
                'body_sha256': claim.get('body_sha256', ''),
                'source_line': claim.get('source_line', ''),
                'source_line_number': claim.get('source_line_number'),
                'source_line_sha256': claim.get('source_line_sha256', ''),
                'citation_scope': claim.get('citation_scope', ''),
            },
            'provenance': prov,
            'verify': {'verdict': v, 'reasoning': verify.get('reasoning', '')},
        })
    return kus


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('frozen_claims', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    data = json.loads(args.frozen_claims.read_text(encoding='utf-8'))
    if isinstance(data.get('result'), dict):
        data = data['result']
    claims = data['claims']
    kus = distill(claims)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'kus': kus}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    auth = Counter(k['authority'] for k in kus)
    verd = Counter(k['verify']['verdict'] for k in kus)
    print(f'{len(kus)} 条 KU 已蒸馏 → {args.output}')
    print('authority 分布:', dict(auth))
    print('verify 分布:', dict(verd))
    contradicted = [k for k in kus if k['authority'] == 'contradicted']
    if contradicted:
        print(f'\n⚠ {len(contradicted)} 条 CONTRADICTED,需要人工看(可能是节点本身facet有问题,不是引用问题):')
        for k in contradicted:
            print(f"  {k['node_id']}: {k['claim_text']!r} vs {k['provenance']['ref']}")
