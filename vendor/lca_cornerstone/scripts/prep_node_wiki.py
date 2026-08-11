#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""generate-node-wiki 的历史试点入口物料化脚本(确定性,零 LLM)。

把一个骨架节点(+ repair 模式下它现有的 wiki 正文)物料化成一个自包含 run-script,
再由 Workflow({scriptPath}) 跑 SearchFetch/Verify。数据 embed 进 run-script——
本仓库实测教训:Workflow 的 args 形参不注入全局,传参必须 embed。

用法:
  python3 scripts/prep_node_wiki.py <ind> <node_id> --mode extract   # 新节点:模型自报断言+来源
  python3 scripts/prep_node_wiki.py <ind> <node_id> --mode repair    # 已有正文:核验老断言(默认)
  python3 scripts/prep_node_wiki.py <ind> <node_id> --mode repair --reverify-all
                                                                    # source 已 verified 也逐断言复核

默认产出: runs/wiki/<ind>/<node_id>/<mode>/workflow.run.js
Workflow DSL 由 validate_wiki_workflow.py 校验，不使用会误报顶层 return 的 node --check。
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from validate_wiki_workflow import validate_workflow

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WF_DIR = os.path.join(ROOT, ".claude", "workflows")

IND_CN = {'steel': '钢铁', 'power': '电力', 'auto': '汽车', 'aluminium': '铝', 'copper': '铜',
          'battery': '电池', 'electronics': '电子', 'chemicals': '化学品', 'plastics': '塑料',
          'glass': '玻璃', 'rubber': '橡胶'}


def find_node(d, node_id):
    for n in d.get("products", []) + d.get("activities", []):
        if n.get("id") == node_id:
            return n
    return None


def clean(s):
    s = re.sub(r'\[\^[a-z0-9\-]+\]', '', s)      # 去引用标记
    s = re.sub(r'\*\*([^*]+)\*\*', r'\1', s)      # 去粗体
    s = re.sub(r'`([^`]+)`', r'\1', s)            # 去行内代码
    s = re.sub(r'^[\-\*]\s+', '', s.strip())      # 去列表符
    s = re.sub(r'\s*(?:✅已核实(?:\([^)]*\))?|〔(?:图谱事实|建模判断|证据缺口|未核实(?:·模型回忆)?)〕)\s*', ' ', s)
    return s.strip()


def atomize(body, reg, verified, skip_verified=True):
    """把已有正文按句子拆成原子断言:每个尾部挂 [^tag] 行内引用的句子=一条待核验断言。
    默认跳过全部标签已 verified(ku-*) 的句子以保持幂等；--reverify-all 时不跳过，
    用于修复“source 级 verified 被误当成 claim 级 verified”的历史页面。
    无 [^tag] 的句子(纯建模判断)不进核验。"""
    body = re.split(r'\n##\s*出处', body)[0]        # 砍掉脚注定义区
    claims, section = [], ''
    for line_number, raw in enumerate(body.split('\n'), start=1):
        line = raw.strip()
        h = re.match(r'^##\s+(.*)$', line)
        if h:
            section = h.group(1).strip(); continue
        if not line or line.startswith('>'):
            continue
        # Markdown 常把脚注写在句号之后：“断言。[^src]”。若直接按句号切分，
        # 引用会被拆成一个独立短片段，导致真正的断言被静默漏掉。
        # 仅为原子化临时把连续脚注移到句末标点之前，再按标点切分。
        line_tags = re.findall(r'\[\^([a-z0-9\-]+)\](?!:)', line)
        parse_line = re.sub(r'([。！？；])((?:\[\^[a-z0-9\-]+\])+)', r'\2\1', line)
        for sent in re.split(r'(?<=[。！？；])', parse_line):
            tags = re.findall(r'\[\^([a-z0-9\-]+)\](?!:)', sent)
            citation_scope = 'direct'
            # 现有 Wiki 中常见“一段多句、段末统一脚注”。为避免只核验最后一句，
            # 段内没有独立脚注的句子继承该行的段末标签并进入 Verify。
            if not tags and line_tags:
                tags = line_tags
                citation_scope = 'paragraph_inherited'
            if not tags:
                continue
            if skip_verified and all(t in verified for t in tags):
                continue
            text = clean(sent)
            if len(text) < 8:
                continue
            if '〔图谱事实〕' in sent:
                claim_kind = 'internal_graph_fact'
                src = 'LCA-CORNERSTONE_GRAPH'
            elif '〔建模判断〕' in sent:
                claim_kind = 'modeling_judgment'
                src = 'INTERNAL_MODELING_JUDGMENT'
            elif '〔证据缺口〕' in sent:
                claim_kind = 'evidence_gap'
                src = 'INTERNAL_MODELING_JUDGMENT'
            else:
                claim_kind = 'external_fact'
                src = ' 或 '.join(reg.get(t, {}).get('title', t) for t in tags)
            claims.append({'section': section, 'old_tags': tags,
                           'claim_text': text, 'claim_kind': claim_kind,
                           'believed_source': src,
                           # 受控回写只自动处理“单断言独占一行且引用直接挂在该句”的情形；
                           # 其余情况保留行级锚，交给 merge_wiki_ku.py 转 manual_review。
                           'source_line': raw,
                           'source_line_number': line_number,
                           'source_line_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                           'citation_scope': citation_scope})
    return claims


def splice(wf_path, const_name, data, out_path):
    txt = open(wf_path, encoding='utf-8').read()
    block = ("/* DATA-BINDING:START — injected by prep_node_wiki.py */\n"
             f"const {const_name} = " + json.dumps(data, ensure_ascii=False, indent=2) +
             "\n/* DATA-BINDING:END */")
    new = re.sub(r'/\* DATA-BINDING:START.*?DATA-BINDING:END \*/', lambda m: block, txt, flags=re.S)
    assert new != txt, f'DATA-BINDING 标记未在 {wf_path} 找到——workflow 模板可能被改动'
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, 'w', encoding='utf-8').write(new)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('industry')
    parser.add_argument('node_id')
    parser.add_argument('--mode', choices=('extract', 'repair'), default='repair')
    parser.add_argument('--reverify-all', action='store_true')
    parser.add_argument('--output', type=Path, help='物料化 Workflow 的输出路径')
    args = parser.parse_args()
    ind, node_id, mode = args.industry, args.node_id, args.mode
    reverify_all = args.reverify_all
    if mode == 'extract' and reverify_all:
        parser.error('--reverify-all 只适用于 repair 模式')

    graph = os.path.join(ROOT, 'docs', f'{ind}-name-graph.json')
    d = json.load(open(graph, encoding='utf-8'))
    node = find_node(d, node_id)
    assert node, f'{node_id} 不在 {graph}'
    industry_cn = IND_CN.get(ind) or d.get('_meta', {}).get('industry') or ind
    out_path = args.output or Path(ROOT, 'runs', 'wiki', ind, node_id, mode, 'workflow.run.js')
    out = str(out_path.resolve())

    if mode == 'extract':
        nodes = [{'node_id': node_id, 'industry': ind, 'industry_cn': industry_cn,
                  'name': node['name'], 'facets': node.get('facets', {}),
                  'boundary': node.get('boundary')}]
        splice(os.path.join(WF_DIR, 'wiki-ku-provenance.js'), 'NODES', nodes, out)
        print(f'[extract] {ind}::{node_id}「{node["name"]}」→ {out}')
        print(f'  1 个节点待 Extract→SearchFetch→Verify')
    else:
        reg = json.load(open(os.path.join(ROOT, 'sources', ind, 'registry.json'), encoding='utf-8'))['sources']
        verified = {sid for sid, s in reg.items() if s.get('status') == 'verified'}
        pages = (glob.glob(os.path.join(ROOT, 'wiki', ind, 'products', f'{node_id}--*.md')) +
                 glob.glob(os.path.join(ROOT, 'wiki', ind, 'activities', f'{node_id}--*.md')))
        assert pages, f'wiki/{ind} 下找不到 {node_id} 的页面'
        txt = open(pages[0], encoding='utf-8').read()
        m = re.search(r'<!-- BODY:START -->(.*?)<!-- BODY:END -->', txt, re.S)
        assert m, f'{pages[0]} 无 BODY 区块'
        raw = atomize(m.group(1), reg, verified, skip_verified=not reverify_all)
        body_sha256 = hashlib.sha256(m.group(1).encode('utf-8')).hexdigest()
        claims = []
        for index, claim in enumerate(raw):
            claim_id = hashlib.sha1(
                f'{node_id}|{claim["section"]}|{claim["claim_text"]}'.encode('utf-8')
            ).hexdigest()[:16]
            claims.append({
                'claim_id': f'{node_id}-{claim_id}',
                'node_id': node_id,
                'industry': ind,
                'body_sha256': body_sha256,
                **claim,
            })
        if not claims:
            reason = '正文无带引用的外部断言' if reverify_all else '可能已全部 verified,或正文无带引用的外部断言'
            print(f'[repair] {ind}::{node_id}: 无待核验老断言({reason})——无需跑 workflow')
            sys.exit(0)
        splice(os.path.join(WF_DIR, 'wiki-ku-provenance-repair.js'), 'CLAIMS', claims, out)
        print(f'[repair] {ind}::{node_id}「{node["name"]}」→ {out}')
        scope = '包含 source 已 verified 的断言' if reverify_all else '已跳过 verified 的'
        print(f'  拆出 {len(claims)} 条待核验老断言({scope}):')
        for c in claims:
            print(f'    · [{c["section"]}] tags={c["old_tags"]} {c["claim_text"][:42]}…')

    report = validate_workflow(Path(out))
    print(f'  Workflow 协议校验通过: {report}')
    print(f'\n下一步:')
    print(f'  python3 scripts/validate_wiki_workflow.py workflow {out}')
    print(f'  Workflow({{scriptPath: "{out}"}})       # 后台跑 LLM 核验')
    print(f'  → 完成后 python3 scripts/wiki_pipeline.py finalize {ind} {node_id} <task-output.json>')


if __name__ == '__main__':
    main()
