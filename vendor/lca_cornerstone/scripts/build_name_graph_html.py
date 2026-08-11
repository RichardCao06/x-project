#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成行业名称图的整图浏览器 HTML(深蓝蓝图风:概览/约定/产品/活动/外部对表/记分卡 六标签 + 点击节点内嵌 wiki 抽屉)。
用干净模板 scripts/templates/name-graph.tpl.html(占位符 __DATA__/__TITLE__/__WIKI__),
按输出文件名派生 wiki viewer = <slug>-wiki.html → 每个行业的整图各链各自的 wiki。
正式名称图可通过 ``wiki/<slug>/draft-preview.json`` 把指定节点路由到独立 preview viewer；
这只开放草稿可见性，不覆盖 production bundle，也不授予 reviewed/publish 权限。

用法:
  python3 scripts/build_name_graph_html.py docs/auto-name-graph.json [docs/auto-name-graph.html]   # 单文件
  python3 scripts/build_name_graph_html.py docs/auto-name-graph.json --preview                     # 草稿预览图
  python3 scripts/build_name_graph_html.py --all                                                    # 批跑(幂等:html 比 json 新则跳过)
  python3 scripts/build_name_graph_html.py --all --force                                            # 强制重生
"""
import argparse, json, sys, os, glob, re


def draft_preview_nodes(slug):
    path = draft_preview_path(slug)
    if not os.path.exists(path):
        return []
    payload = json.load(open(path, encoding='utf-8'))
    if payload.get('schema_version') != 'wiki-draft-preview-v1':
        raise SystemExit(f'❌ {path}: schema_version 必须是 wiki-draft-preview-v1')
    nodes = payload.get('nodes')
    if not isinstance(nodes, list) or any(
        not isinstance(node, str) or not re.fullmatch(r'[PA]\d{3}', node)
        for node in nodes
    ):
        raise SystemExit(f'❌ {path}: nodes 必须是 P/A + 三位数字的数组')
    if len(nodes) != len(set(nodes)):
        raise SystemExit(f'❌ {path}: nodes 不得重复')
    return nodes


def draft_preview_path(slug):
    return f'wiki/{slug}/draft-preview.json'


def build_inputs(graph_path, template):
    slug = os.path.basename(graph_path).replace('-name-graph.json', '')
    inputs = [graph_path, template]
    overlay = draft_preview_path(slug)
    if os.path.exists(overlay):
        inputs.append(overlay)
    return inputs


def output_overlay_matches(out_path, slug):
    if not os.path.exists(out_path):
        return False
    marker = 'wikiPreviewIds=new Set(' + json.dumps(
        draft_preview_nodes(slug), ensure_ascii=False
    ) + ');'
    return marker in open(out_path, encoding='utf-8').read()


def build(graph_path, out_path, template='scripts/templates/name-graph.tpl.html', preview=False,
          preview_nodes=None):
    d = json.load(open(graph_path))
    slug = os.path.basename(graph_path).replace('-name-graph.json', '')
    wiki = f'{slug}-wiki-preview.html' if preview else f'{slug}-wiki.html'
    wiki_preview = f'{slug}-wiki-preview.html'
    preview_nodes = [] if preview else (
        draft_preview_nodes(slug) if preview_nodes is None else list(preview_nodes)
    )
    title = (d.get('_meta') or {}).get('title', '名称图')
    tpl = open(template, encoding='utf-8').read()
    if '__DATA__' not in tpl:
        raise SystemExit('❌ 模板缺 __DATA__ 占位符')
    tpl = tpl.replace('__TITLE__', title).replace('__WIKI__', wiki)
    tpl = tpl.replace('__WIKI_PREVIEW__', wiki_preview)
    tpl = tpl.replace('__WIKI_PREVIEW_IDS__', json.dumps(preview_nodes, ensure_ascii=False))
    tpl = tpl.replace('__DATA__', json.dumps(d, ensure_ascii=False))   # str.replace 不解释反斜杠,放最后
    open(out_path, 'w', encoding='utf-8').write(tpl)
    m = (d.get('_meta') or {}).get('counts', {})
    mode = 'preview' if preview else 'production'
    overlay = f' · draft overlay={preview_nodes}' if preview_nodes else ''
    print(f"✅ {out_path} ({len(tpl)}B) · {mode} · 标题={title} · wiki→{wiki}{overlay} · 产品{m.get('products','?')}/活动{m.get('activities','?')}")


def derive_out(json_path):
    return json_path[:-5] + '.html' if json_path.endswith('.json') else json_path + '.html'


def main():
    ap = argparse.ArgumentParser(
        description='行业名称图整图 HTML 渲染器(单文件或 --all 批跑)',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('graph', nargs='?', help='单文件模式:输入 *-name-graph.json 路径')
    ap.add_argument('out', nargs='?', help='单文件模式:输出 *-name-graph.html 路径(可省略,自动派生)')
    ap.add_argument('--all', action='store_true', help='批跑:扫描 docs/*-name-graph.json,缺/旧则生成')
    ap.add_argument('--force', action='store_true', help='强制重生(默认幂等:html 比 json 新则跳过)')
    ap.add_argument('--preview', action='store_true', help='生成独立草稿预览图并链接 preview Wiki；不覆盖 production')
    ap.add_argument('--template', default='scripts/templates/name-graph.tpl.html')
    args = ap.parse_args()

    if args.all:
        if args.preview:
            ap.error('--preview 当前只支持单文件模式，避免批量生成未请求的草稿页面')
        n_built = n_skip = n_err = 0
        for f in sorted(glob.glob('docs/*-name-graph.json')):
            out = derive_out(f)
            inputs = build_inputs(f, args.template)
            slug = os.path.basename(f).replace('-name-graph.json', '')
            if (
                not args.force
                and os.path.exists(out)
                and os.path.getmtime(out) >= max(os.path.getmtime(path) for path in inputs)
                and output_overlay_matches(out, slug)
            ):
                n_skip += 1
                continue
            try:
                build(f, out, args.template)
                n_built += 1
            except Exception as exc:
                sys.stderr.write(f'❌ {f}: {exc}\n')
                n_err += 1
        print(f'── 批跑完毕: {n_built} 已生成 · {n_skip} 跳过(html 已是最新) · {n_err} 错')
        return 0 if n_err == 0 else 1

    if not args.graph:
        ap.error('单文件模式需要 graph 路径,或用 --all')
    out = args.out or (derive_out(args.graph).replace('.html', '-preview.html')
                       if args.preview else derive_out(args.graph))
    if args.preview and not out.endswith('-name-graph-preview.html'):
        ap.error('preview 输出必须以 -name-graph-preview.html 结尾')
    build(args.graph, out, args.template, preview=args.preview)
    return 0


if __name__ == '__main__':
    sys.exit(main())
