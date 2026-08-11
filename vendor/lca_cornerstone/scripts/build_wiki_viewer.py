#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""由 steel-wiki.html 模板生成某行业的 wiki 查看器页。
查看器是薄壳(~9KB):<script src="<ind>-wiki-data.js"> + 读 window.<IND>_WIKI。
build_wiki_bundle.py 只产数据包(*-wiki-data.js),查看器页需本脚本另生成(否则 *-name-graph.html 的 wiki 链接 404)。
``--preview`` 生成完全独立的 ``<ind>-wiki-preview.html``，不会覆盖正式查看器。

用法:
  python3 scripts/build_wiki_viewer.py <ind> "<中文名>"
  python3 scripts/build_wiki_viewer.py <ind> "<中文名>" --preview
"""
import argparse
import hashlib
import json
import re
import sys

ZH = {'steel':'钢铁','power':'电力','auto':'汽车','aluminium':'铝','copper':'铜',
      'battery':'动力电池','electronics':'电子','chemicals':'化学品','plastics':'塑料',
      'glass':'玻璃','rubber':'橡胶','nonferrous_metals':'有色金属','textiles':'纺织',
      'magnesium':'镁','agriculture':'农林','oil_refining':'石油炼制',
      'ict_equipment':'信息与通信技术设备'}

def main(ind, zh=None, preview=False, start_node=None):
    if start_node and not re.fullmatch(r"[PA]\d{3}", start_node):
        raise ValueError(f"非法 start_node: {start_node}")
    zh = zh or ZH.get(ind, ind)
    tpl = open('docs/steel-wiki.html').read()
    data_name = f'{ind}-wiki-preview-data.js' if preview else f'{ind}-wiki-data.js'
    data_path = f'docs/{data_name}'
    # Local ``file:`` viewers may retain a stale JS asset after the Wiki body
    # changes.  Bind the viewer to the bundle content so a rebuilt page always
    # requests the current data package.
    data_version = hashlib.sha256(open(data_path, 'rb').read()).hexdigest()[:12]
    data_src = f'{data_name}?v={data_version}'
    variable = f'{ind.upper()}_WIKI_PREVIEW' if preview else f'{ind.upper()}_WIKI'
    graph_name = f'{ind}-name-graph-preview.html' if preview else f'{ind}-name-graph.html'
    viewer_name = (
        f'{ind}-wiki-{start_node}-preview.html' if preview and start_node
        else f'{ind}-wiki-{start_node}.html' if start_node
        else f'{ind}-wiki-preview.html' if preview
        else f'{ind}-wiki.html'
    )
    out = tpl.replace('steel-wiki-data.js', data_src)
    out = out.replace('STEEL_WIKI', variable)
    out = out.replace('steel-name-graph.html', graph_name)
    out = out.replace('../wiki/steel/index.md', f'../wiki/{ind}/index.md')
    out = out.replace('钢铁', zh)
    if start_node:
        marker = "const start = qp.get('id') || ORDER[0];"
        replacement = f"const start = qp.get('id') || {json.dumps(start_node)};"
        if marker not in out:
            raise ValueError("viewer 模板缺少 start-node 锚点")
        out = out.replace(marker, replacement, 1)
    path = f'docs/{viewer_name}'
    open(path, 'w').write(out)
    mode = 'preview' if preview else 'production'
    entry = f' · 默认节点 {start_node}' if start_node else ''
    print(f'✅ {path} ({len(out)//1024}KB) · {mode}{entry} · 载入 {data_name} · window.{variable} · 标题 {zh}节点 Wiki')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('industry')
    ap.add_argument('chinese_name', nargs='?')
    ap.add_argument('--preview', action='store_true')
    ap.add_argument('--start-node', help='生成无需 query 参数即可直达节点的真实 HTML 文件')
    args = ap.parse_args()
    main(args.industry, args.chinese_name, args.preview, args.start_node)
