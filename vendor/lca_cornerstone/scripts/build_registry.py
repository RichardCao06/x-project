#!/usr/bin/env python3
"""从名称图 provenance 字符串半自动抽取源注册表种子（v1 bootstrap，需人工核定）。
用法: python3 scripts/build_registry.py docs/steel-name-graph.json sources/steel/registry.json"""
import json, sys, re

# 受控源词典：pattern(小写) -> (src-id, 元数据)。顺序优先。
CATALOG = [
 ("bref",        ("bref-is-2013", {"title":"Iron & Steel Production BREF (Best Available Techniques Reference Document)","type":"BREF","version":"2013","locator":"European Commission JRC / IPPC","authority":"regulatory-technical"})),
 ("worldsteel",  ("worldsteel-lci",{"title":"worldsteel LCI Methodology Report / Life Cycle Inventory study","type":"methodology","version":"2017","locator":"World Steel Association","authority":"industry-methodology"})),
 ("ecoinvent",   ("ecoinvent",    {"title":"ecoinvent database — 活动|参考产品 命名与覆盖参照","type":"LCI-database","version":"v3.x","locator":"ecoinvent Association","authority":"database-naming"})),
 ("uslci",       ("uslci",        {"title":"US Life Cycle Inventory Database (USLCI)","type":"LCI-database","version":"NREL","locator":"NREL/US DOE","authority":"database-naming"})),
 ("us lci",      ("uslci",        {"title":"US Life Cycle Inventory Database (USLCI)","type":"LCI-database","version":"NREL","locator":"NREL/US DOE","authority":"database-naming"})),
 ("gabi",        ("gabi",         {"title":"GaBi / Sphera LCA database — 命名参照","type":"LCI-database","version":"Sphera","locator":"Sphera","authority":"database-naming"})),
 ("ullmann",     ("ullmanns",     {"title":"Ullmann's Encyclopedia of Industrial Chemistry","type":"handbook","version":"-","locator":"Wiley-VCH","authority":"engineering-handbook"})),
 ("kirk-othmer", ("kirk-othmer",  {"title":"Kirk-Othmer Encyclopedia of Chemical Technology","type":"handbook","version":"-","locator":"Wiley","authority":"engineering-handbook"})),
 ("hs ",         ("hs",           {"title":"Harmonized System (HS) Nomenclature","type":"classification","version":"WCO","locator":"World Customs Organization","authority":"statistical-classification"})),
 ("hs=",         ("hs",           {"title":"Harmonized System (HS) Nomenclature","type":"classification","version":"WCO","locator":"World Customs Organization","authority":"statistical-classification"})),
 ("cpc",         ("cpc",          {"title":"UN Central Product Classification (CPC)","type":"classification","version":"UN","locator":"United Nations Statistics","authority":"statistical-classification"})),
 ("isic",        ("isic",         {"title":"UN ISIC Rev.4 (International Standard Industrial Classification)","type":"classification","version":"Rev.4","locator":"United Nations Statistics","authority":"statistical-classification"})),
 ("synonym_table",("synonym-table",{"title":"项目内受控词表 / 同义词表 (conventions.synonym_table)","type":"internal","version":"name-graph-v2","locator":"docs/steel-name-graph.json","authority":"internal-controlled-vocab"})),
]
INTERNAL=("internal-review",{"title":"名称图采集SOP内部评审 / 外部对表 / 建模判断笔记","type":"internal","version":"name-graph-v2","locator":"采集与制作方案.md / reconciliation-worksheet.md","authority":"internal-methodology"})

def src_ids_for(prov_str):
    s=prov_str.lower(); hits=set()
    for pat,(sid,_) in CATALOG:
        if pat in s: hits.add(sid)
    # 内部评审/对表/建模判断/不变量
    if any(k in s for k in ["评审","对表","建模","不变量","synonym","规范名","schema"]):
        hits.add("internal-review")
    if not hits: hits.add("internal-review")
    return hits

def main(graph_path, out_path):
    d=json.load(open(graph_path))
    nodes=d["products"]+d["activities"]
    reg={}; used=set(); excerpt_idx={}
    for n in nodes:
        for s in (n.get("provenance") or []):
            for sid in src_ids_for(s):
                used.add(sid)
                excerpt_idx.setdefault(sid,[]).append(s)
    # 物化 registry（仅纳入实际被引用的源）
    meta={sid:m for pat,(sid,m) in CATALOG}; meta["internal-review"]=INTERNAL[1]
    for sid in sorted(used):
        m=dict(meta[sid])
        # 抽样摘录（去重，最多保留代表性若干条 provenance 原文作 excerpt 占位）
        seen=[]; 
        for s in excerpt_idx.get(sid,[]):
            if s not in seen: seen.append(s)
        m["hash"]=""  # 待人工：摘录文件 hash
        m["ref_count"]=len(excerpt_idx.get(sid,[]))
        m["excerpt_seeds"]=seen[:8]  # v1 种子，人工核定后落 excerpts/
        m["status"]="seed-unverified"  # 人工核定后改 verified
        reg[sid]=m
    out={"_meta":{"industry":"steel","note":"v1 半自动种子，需人工核定后将 status 改 verified；excerpt_seeds 为 provenance 原文，正式摘录落 excerpts/。版权：只存摘录+定位，不存全文。","schema":"src-id -> {title,type,version,locator,authority,hash,excerpt_seeds,status}"},
         "sources":reg}
    json.dump(out,open(out_path,"w"),ensure_ascii=False,indent=2)
    print(f"registry: {len(reg)} 源 -> {out_path}")
    for sid in sorted(used): print(f"  · {sid:18s} refs={reg[sid]['ref_count']:3d}  {reg[sid]['title'][:48]}")

if __name__=="__main__":
    main(sys.argv[1], sys.argv[2])
