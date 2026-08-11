#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名称图『精简索引』生成器 —— 给多 agent 编排的播种/评审 agent 喂轻量上下文。

动机:全图 docs/*-name-graph.json 约 250-313KB,大头是每个节点的 provenance(300-440B)
+ external_mapping(mapped/gaps 各 4-8KB) + scorecard + synonym_table。fan-out 时若把全图
下发给每个播种/评审 agent,每个 agent 的缓存读会被这些重字段撑大(实测 ~34-66K/条)。
而播种(查漏)/评审(查完整性)只需要『节点身份』:id / name / 身份刻面 / 分类码 / confidence。
本脚本只保留身份,典型把 ~300KB 压到 ~30-60KB,fan-out 缓存读随之锐减。

谁该用全图、谁该用索引:
  - 播种 agent(阶段二)/ 评审 agent(阶段六)   → 索引(本脚本输出)
  - 共产品/分类交叉评审                          → 索引 + --with-io
  - 总集成者(阶段一/七)/ 闭合不变量校验(阶段三) → 仍用全图(需要 provenance/edges/mapping)

用法:
  python3 scripts/graph_index.py docs/power-name-graph.json             # 精简文本 → stdout
  python3 scripts/graph_index.py docs/power-name-graph.json --json      # 精简 JSON → stdout
  python3 scripts/graph_index.py docs/power-name-graph.json --with-io   # 附活动 inputs/outputs(共产品评审用)
  python3 scripts/graph_index.py docs/steel-name-graph.json --out /tmp/steel.idx.md
  python3 scripts/graph_index.py --all                                  # 四行业 → docs/<ind>-name-graph.index.md

退出码 0=成功, 1=出错。
"""
import json, sys, os, argparse


def _short_vals(controlled):
    """controlled_values 形如 ['high_voltage(高压…)', …] —— 取括号前的码值。"""
    out = []
    for x in (controlled or []):
        out.append(str(x).split("(")[0].split("（")[0].strip())
    return [v for v in out if v]


def build_index(g, with_io=False, with_edges=False, with_prov=False, with_mapping=False):
    """从整图 dict 抽出精简索引 dict。返回 (idx, product_facet_order, activity_facet_order)。"""
    conv = g.get("conventions", {}) or {}
    pf = [f["name"] for f in conv.get("product_facets", []) if "name" in f]
    af = [f["name"] for f in conv.get("activity_facets", []) if "name" in f]

    def prod(p):
        fac = p.get("facets") or {}
        o = {
            "id": p.get("id"),
            "name": p.get("name"),
            "facets": {k: fac.get(k) for k in pf} if pf else fac,
            "cpc": p.get("cpc"),
            "hs": p.get("hs"),
            "confidence": p.get("confidence"),
        }
        if p.get("boundary"):
            o["boundary"] = p.get("boundary")
        if with_prov:
            o["provenance"] = p.get("provenance")
        return o

    def act(a):
        fac = a.get("facets") or {}
        o = {
            "id": a.get("id"),
            "name": a.get("name"),
            "facets": {k: fac.get(k) for k in af} if af else fac,
            # isic/bref 在 power 是顶层、在 steel 进 facets —— 两处都兜
            "isic": a.get("isic") or fac.get("isic"),
            "bref": a.get("bref") or fac.get("bref"),
            "confidence": a.get("confidence"),
        }
        if with_io:
            o["inputs"] = a.get("inputs")
            o["outputs"] = a.get("outputs")
        if with_prov:
            o["provenance"] = a.get("provenance")
        return o

    meta = g.get("_meta", {}) or {}
    idx = {
        "_meta": {
            **{k: meta.get(k) for k in ("title", "industry", "scope", "method", "generated")},
            "source_is": "index (provenance/external_mapping/scorecard/synonym_table 已剔除)",
            "counts": {
                "products": len(g.get("products", [])),
                "activities": len(g.get("activities", [])),
                "edges": len(g.get("edges", [])),
            },
        },
        "identity": {
            "product_facets": pf,
            "activity_facets": af,
            "product_naming_template": conv.get("product_naming_template"),
            "activity_naming_template": conv.get("activity_naming_template"),
            "controlled_values": {
                **{f["name"]: f.get("controlled_values") for f in conv.get("product_facets", []) if "name" in f},
                **{f["name"]: f.get("controlled_values") for f in conv.get("activity_facets", []) if "name" in f},
            },
        },
        "products": [prod(p) for p in g.get("products", [])],
        "activities": [act(a) for a in g.get("activities", [])],
    }
    if with_edges:
        idx["edges"] = g.get("edges")
    if with_mapping:
        idx["external_mapping"] = g.get("external_mapping")
    return idx, pf, af


def render_text(idx, pf, af, with_io):
    """渲染为对 LLM 最省 token 的紧凑文本(每节点一行)。"""
    L = []
    m = idx["_meta"]
    c = m["counts"]
    L.append(f"# {m.get('title') or m.get('industry') or '名称图'} · 精简索引")
    L.append(f"# 产品 {c['products']} · 活动 {c['activities']} · 边 {c['edges']}  (已剔除 provenance/external_mapping/scorecard)")
    L.append("")
    cv = idx["identity"]["controlled_values"]
    L.append("## 身份刻面(命名键,按顺序;查重/查漏以此为准)")
    if pf:
        L.append(f"产品键: {', '.join(pf)}")
        for k in pf:
            vs = _short_vals(cv.get(k))
            if vs:
                L.append(f"  {k} ∈ {{{', '.join(vs)}}}")
    if af:
        L.append(f"活动键: {', '.join(af)}")
        for k in af:
            vs = _short_vals(cv.get(k))
            if vs:
                L.append(f"  {k} ∈ {{{', '.join(vs)}}}")
    if idx["identity"].get("product_naming_template"):
        L.append(f"产品命名模板: {idx['identity']['product_naming_template']}")
    if idx["identity"].get("activity_naming_template"):
        L.append(f"活动命名模板: {idx['identity']['activity_naming_template']}")
    L.append("")

    L.append(f"## 产品 ({c['products']})  —— 行格式: ID  名称  [cpc/hs/confidence]  {{身份刻面值}}")
    for p in idx["products"]:
        codes = []
        if p.get("cpc"):
            codes.append(f"cpc={p['cpc']}")
        if p.get("hs"):
            codes.append(f"hs={p['hs']}")
        if p.get("boundary") and p["boundary"] != "foreground":
            codes.append(p["boundary"])
        if p.get("confidence"):
            codes.append(p["confidence"])
        fv = "|".join(str(p["facets"].get(k, "") or "") for k in pf) if pf else ""
        L.append(f"{p['id']}  {p['name']}  [{' '.join(codes)}]  {{{fv}}}")
    L.append("")

    L.append(f"## 活动 ({c['activities']})  —— 行格式: ID  名称  [isic/confidence]  {{身份刻面值}}")
    for a in idx["activities"]:
        codes = []
        if a.get("isic"):
            codes.append(f"isic={a['isic']}")
        if a.get("confidence"):
            codes.append(a["confidence"])
        fv = "|".join(str(a["facets"].get(k, "") or "") for k in af) if af else ""
        L.append(f"{a['id']}  {a['name']}  [{' '.join(codes)}]  {{{fv}}}")
        if with_io:
            def ofmt(o):
                if isinstance(o, dict):
                    return f"{o.get('product')}({o.get('role')})"
                return str(o)
            ins = a.get("inputs") or []
            outs = a.get("outputs") or []
            L.append(f"    in: {', '.join(str(x) for x in ins)}")
            L.append(f"    out: {', '.join(ofmt(o) for o in outs)}")
    return "\n".join(L) + "\n"


def process(path, args):
    with open(path) as f:
        g = json.load(f)
    idx, pf, af = build_index(
        g,
        with_io=args.with_io,
        with_edges=args.with_edges,
        with_prov=args.with_provenance,
        with_mapping=args.with_external_mapping,
    )
    if args.json:
        out = json.dumps(idx, ensure_ascii=False, indent=(None if args.compact else 2))
        if not out.endswith("\n"):
            out += "\n"
    else:
        out = render_text(idx, pf, af, args.with_io)

    src_bytes = os.path.getsize(path)
    out_bytes = len(out.encode("utf-8"))
    pct = (1 - out_bytes / src_bytes) * 100 if src_bytes else 0.0
    sys.stderr.write(
        f"[graph_index] {os.path.basename(path)}: 原图 {src_bytes/1024:.0f}KB → 索引 {out_bytes/1024:.0f}KB "
        f"(省 {pct:.0f}%) | 产品 {len(idx['products'])} 活动 {len(idx['activities'])}\n"
    )
    return out


def main():
    ap = argparse.ArgumentParser(
        description="名称图精简索引生成器(给 fan-out 播种/评审 agent 喂轻量上下文)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("graph", nargs="?", help="名称图 JSON 路径(不传则需 --all)")
    ap.add_argument("--all", action="store_true", help="对 INDUSTRIES 四行业各生成 docs/<ind>-name-graph.index.<ext>")
    ap.add_argument("--json", action="store_true", help="输出精简 JSON(默认输出紧凑文本)")
    ap.add_argument("--compact", action="store_true", help="JSON 模式单行紧凑(配合 --json)")
    ap.add_argument("--with-io", action="store_true", help="附活动 inputs/outputs(共产品/分类交叉评审用)")
    ap.add_argument("--with-edges", action="store_true", help="附 edges(默认剔除)")
    ap.add_argument("--with-provenance", action="store_true", help="附 provenance(默认剔除;一般不需要)")
    ap.add_argument("--with-external-mapping", action="store_true", help="附 external_mapping(默认剔除)")
    ap.add_argument("--out", help="单图输出到文件(默认 stdout)")
    args = ap.parse_args()

    ext = "json" if args.json else "md"

    if args.all:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from assign_gpid import INDUSTRIES
        except Exception as e:
            sys.stderr.write(f"[error] 无法从 assign_gpid 读取 INDUSTRIES: {e}\n")
            return 1
        rc = 0
        for ind, path in INDUSTRIES.items():
            if not os.path.exists(path):
                sys.stderr.write(f"[skip] {path} 不存在\n")
                rc = 1
                continue
            out = process(path, args)
            dst = path[:-5] + f".index.{ext}" if path.endswith(".json") else path + f".index.{ext}"
            with open(dst, "w") as f:
                f.write(out)
            sys.stderr.write(f"  → {dst}\n")
        return rc

    if not args.graph:
        ap.error("需要 graph 路径,或用 --all")
    if not os.path.exists(args.graph):
        sys.stderr.write(f"[error] {args.graph} 不存在\n")
        return 1
    out = process(args.graph, args)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out)
        sys.stderr.write(f"  → {args.out}\n")
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
