#!/usr/bin/env python3
"""wiki 肉层确定性 lint（硬 gate，退出码）。在 validate_graph(脊层) 之上。
检查:页覆盖与 frontmatter 同步、引用与数量防火墙、证据 schema/表归属/值标签/流类型、脊边对账和 reviewed 溯源。
用法: python3 scripts/wiki_lint.py docs/steel-name-graph.json wiki/steel sources/steel/registry.json"""
import json, sys, os, re, hashlib, glob
from collections import defaultdict
from pathlib import Path
def h12(s): return hashlib.md5(s.encode('utf-8')).hexdigest()[:12]

def spine_hashes(d):
    P=d["products"]; A=d["activities"]; E=d["edges"]
    pid={p["id"]:p for p in P}; aid={a["id"]:a for a in A}
    eba=defaultdict(list); ebp=defaultdict(list)
    for e in E:
        if e["from"] in aid: eba[e["from"]].append(e)
        ebp[e["to"]].append(e)
    H={}
    for a in A:
        ea=eba[a["id"]]
        prod=[e["to"] for e in ea if e["type"]=="PRODUCES"]; cons=[e["to"] for e in ea if e["type"]=="CONSUMES"]
        ref=next((pid[e["to"]]["name"] for e in ea if e["type"]=="PRODUCES" and e.get("role")=="reference"),"")
        H[a["id"]]=h12("|".join([a["id"],a.get("boundary",""),ref or "",
            ";".join(f"{k}={v}" for k,v in sorted((a.get("facets") or {}).items())),
            "P:"+",".join(sorted(prod)),"C:"+",".join(sorted(cons))]))
    for p in P:
        ep=ebp[p["id"]]
        pb=[e["from"] for e in ep if e["type"]=="PRODUCES"]; cb=[e["from"] for e in ep if e["type"]=="CONSUMES"]
        H[p["id"]]=h12("|".join([p["id"],p.get("boundary",""),
            ";".join(f"{k}={v}" for k,v in sorted((p.get("facets") or {}).items())),
            "by:"+",".join(sorted(set(pb))),"to:"+",".join(sorted(set(cb)))]))
    return H

# 数量防火墙：数字 + 计量单位（聚焦核算量：质量/能量/功率/体积/温度/浓度/压强/电）
UNIT=r'(?:kg|kt|Mt|mg|µg|ug|MJ|GJ|kJ|kWh|MWh|GWh|kW|MW|GW|Nm³|Nm3|m³|m3|kPa|MPa|GPa|bar|ppm|ppb|kmol|mol|kV|MVA|wt%|vol%|%|‰|°C|℃|t|kg|吨|千克|公斤|兆焦|千瓦时|摄氏度|百分点)'
QTY=re.compile(r'(?<![A-Za-z0-9])\d[\d.,]*\s?'+UNIT+r'\b', re.I)
IDREF=re.compile(r'\b([PA]\d{3})\b')
CITE=re.compile(r'\[\^([a-z0-9\-]+)\]')

def parse(path):
    t=Path(path).read_text(encoding='utf-8')
    fm=re.search(r'^---\n(.*?)\n---', t, re.S)
    f=fm.group(1) if fm else ""
    def g(k):
        m=re.search(rf'^{k}:\s*(.*)$', f, re.M); return m.group(1).strip() if m else ""
    body=re.search(r'<!-- BODY:START -->(.*?)<!-- BODY:END -->', t, re.S)
    ev=re.search(r'<!-- EVIDENCE:START -->(.*?)<!-- EVIDENCE:END -->', t, re.S)   # 旧:单一合并表(兼容)
    typed={}                                                                      # 新:按作用拆表
    for kind in ('flows','emissions','indicators','quality','props','params'):
        m=re.search(rf'<!-- EV:{kind}:START -->(.*?)<!-- EV:{kind}:END -->', t, re.S)
        if m: typed[kind]=m.group(1)
    return {"id":g("id"),"node_type":g("node_type"),"spine_hash":g("spine_hash").strip('"'),
            "schema_version":g("schema_version"),"body_status":g("body_status"),
            "structure_status":g("structure_status"),"provenance_status":g("provenance_status"),
            "claim_verification_status":g("claim_verification_status"),"quantity_status":g("quantity_status"),
            "dataset_readiness":g("dataset_readiness"),"change_log_status":g("change_log_status"),
            "prov_refs":re.findall(r'[a-z0-9\-]+', g("provenance_refs")),
            "body":(body.group(1) if body else ""),"evidence":(ev.group(1) if ev else ""),
            "ev_typed":typed,"file":os.path.basename(path)}

# §11 拆表 schema:规范列名是数据契约；保留旧“值+双源”表，但不允许近义表头静默绕门。
TSPEC={
 'flows': {'dual':True,'headers':[
     ['流','方向','单位','basis','国际值 INT','国际源 INT','中国值 CN','中国源 CN','pedigree'],
     ['流','方向','单位','basis','值','国际源 INT','中国源 CN','pedigree']]},
 'emissions': {'dual':True,'headers':[
     ['substance','CAS','compartment','unit','basis','国际值 INT','国际源 INT','中国值 CN','中国源 CN','pedigree'],
     ['substance','CAS','compartment','unit','basis','值','国际源 INT','中国源 CN','pedigree']]},
 'indicators': {'dual':True,'headers':[
     ['indicator','medium','unit','basis','国际值 INT','国际源 INT','中国值 CN','中国源 CN','mapping_status','pedigree']]},
 'quality': {'dual':False,'headers':[
     ['field','unit','basis','中国项目值 CN','中国源 CN','proxy_policy','pedigree']]},
 'props': {'dual':False,'headers':[
     ['property','condition','unit','中国项目值 CN','中国源 CN','pedigree'],
     ['property','condition','unit','值','源','pedigree']]},
 'params': {'dual':True,'headers':[
     ['parameter','geo','unit','basis','国际值 INT','国际源 INT','中国值 CN','中国源 CN','pedigree'],
     ['parameter','geo','unit','basis','值','国际源 INT','中国源 CN','pedigree']]},
}
def typed_table(block):
    rows=[]
    for ln in block.splitlines():
        ln=ln.strip()
        if not ln.startswith('|'): continue
        c=[x.strip() for x in ln.strip('|').split('|')]
        if set(''.join(c))<=set('-: '): continue
        rows.append(c)
    return (rows[0],rows[1:]) if rows else ([],[])

def col(header, pred):
    return next((i for i,h in enumerate(header) if pred(h.strip())), None)

# §11 证据层受控词表
DATA_TYPES={'process_structure','material_energy_input','emissions','stoichiometry_thermophysical',
            'background_link','regional_param','operating_allocation'}
BASES={'measured_average','industry_average','bref_range','bat_ael','cp_benchmark_intl',
       'cp_benchmark_domestic','cp_benchmark_access','energy_quota_limit','water_quota_limit',
       'emission_standard_limit','emission_factor','calculated','proxy','estimate','reference','standard_spec'}
NULLV={'待采','待核','待算','—','-','','na','n/a','not_populated','tbd'}

def is_null_value(value):
    """Treat explicit evidence gaps as null values, not fake population.

    A row may explain why a value is unavailable and still cite the audited
    source set.  Such text is valuable to readers but must not inflate the
    international/China value-population score.
    """
    normalized=str(value).strip().lower()
    return (normalized in NULLV or normalized.startswith('缺口：')
            or normalized.startswith('缺口:') or normalized.startswith('未公开'))
VALUE_TAG=re.compile(r'^〔(实测值|代理值|定义值)〕')
NON_ELEMENTARY_LABEL=re.compile(
    r'(?:\bCOD\b|化学需氧量|石油类|危险废物|废物合计|污泥|废水|污水|废碱|酸泥|碱渣|'
    r'总\s*VOC|无组织\s*VOC|报告口径)', re.I)
def ev_rows(block):
    rows=[]
    for ln in block.splitlines():
        ln=ln.strip()
        if not ln.startswith('|'): continue
        cells=[c.strip() for c in ln.strip('|').split('|')]
        if len(cells)<8: continue
        if cells[0]=='data_type' or set(''.join(cells))<=set('-: '): continue  # 表头/分隔行
        rows.append(cells)
    return rows

def coverage_proof_errors(pages, coverage):
    """Validate that privileged frontmatter states are derived from coverage.

    Kept as a small pure helper so batch/control-plane tests can exercise the
    anti-handfill gate without constructing a complete graph and sigil tree.
    """
    from wiki_claim_coverage import validate_artifact as validate_coverage_artifact
    validate_coverage_artifact(coverage)
    c_nodes=coverage.get('nodes',[])
    c_by_id={str(n.get('node_id','')):n for n in c_nodes}
    c_dup=[nid for nid in set(str(n.get('node_id','')) for n in c_nodes)
           if sum(str(x.get('node_id',''))==nid for x in c_nodes)>1]
    bad=[]
    if c_dup: bad.append(('artifact','duplicate_nodes:'+str(c_dup[:3])))
    allowed={'confirmed','controlled_internal'}
    for p in pages:
        claims_complete=(p['provenance_status']=='claim_verified'
                         or p['claim_verification_status']=='complete')
        proof=c_by_id.get(p['id'])
        # Coverage is batch-scoped.  Historical privileged pages outside the
        # selected node set are audited by their own migration batch and must
        # not prevent an unrelated one-node release.
        if not proof:
            continue
        if not proof.get('eligible_for_reviewed'):
            bad.append((p['id'],'coverage_not_eligible'))
        if proof.get('body_sha256')!=hashlib.sha256(p['body'].encode('utf-8')).hexdigest():
            bad.append((p['id'],'coverage_body_hash_drift'))
        updates=proof.get('frontmatter_updates') or {}
        expected={'schema_version':'wiki-v2','body_status':'reviewed',
                  'content_maturity':'research_ready','provenance_status':'claim_verified',
                  'claim_verification_status':'complete'}
        if updates!=expected:
            bad.append((p['id'],'invalid_frontmatter_upgrade_plan'))
        dispositions={str(c.get('disposition','')) for c in proof.get('claims',[])}
        if not dispositions or not dispositions<=allowed:
            bad.append((p['id'],'noncovered_claim_disposition:'+str(sorted(dispositions-allowed))))
        counts=proof.get('counts') or {}
        if any(counts.get(k,0) for k in ('missing','unresolved','contradicted','manual_review','hash_drift')):
            bad.append((p['id'],'blocking_coverage_count'))
    return bad

def main(graph, wikidir, regpath, coverage_path=None):
    d=json.load(open(graph)); reg=json.load(open(regpath))["sources"]
    allids={n["id"] for n in d["products"]+d["activities"]}
    pidset={p["id"] for p in d["products"]}; aidset={a["id"] for a in d["activities"]}
    H=spine_hashes(d)
    pages=[parse(p) for p in glob.glob(f"{wikidir}/products/*.md")+glob.glob(f"{wikidir}/activities/*.md")]
    R=[]; ok=lambda b,m:R.append((b,m))
    # 1 覆盖(双向)
    page_ids=[p["id"] for p in pages]
    dup=[i for i in set(page_ids) if page_ids.count(i)>1]
    missing=sorted(allids-set(page_ids)); extra=sorted(set(page_ids)-allids)
    ok(not missing and not extra and not dup, f"页覆盖双向 (缺{missing[:3]} 多{extra[:3]} 重{dup[:3]})")
    # 2 spine_hash 同步
    drift=[p["id"] for p in pages if p["id"] in H and p["spine_hash"]!=H[p["id"]]]
    ok(not drift, f"frontmatter spine_hash 同步 ({len(drift)}漂移 e.g.{drift[:4]})")
    bad_node_type=[(p["id"],p["node_type"],"product" if p["id"] in pidset else "activity")
                   for p in pages if p["id"] in allids
                   and p["node_type"]!=("product" if p["id"] in pidset else "activity")]
    ok(not bad_node_type, f"frontmatter node_type 与权威图一致 ({len(bad_node_type)}漂移 e.g.{bad_node_type[:3]})")
    # 仅对已填肉页做 3/4/5
    filled=[p for p in pages if p["body_status"]!="empty" and p["body"].strip() and "正文待 workflow 填肉" not in p["body"]]
    # 3 引用解析 registry + 已填页≥1引用
    bad_cite=[]; nocite=[]
    for p in filled:
        cs=CITE.findall(p["body"])
        if not cs: nocite.append(p["id"])
        for c in cs:
            if c not in reg: bad_cite.append((p["id"],c))
    ok(not bad_cite, f"引用解析到 registry ({len(bad_cite)}未解析 e.g.{bad_cite[:3]})")
    ok(not nocite, f"已填页含≥1引用 ({len(nocite)}页无引用 e.g.{nocite[:4]})")
    # 4 数量防火墙
    leaks=[]
    for p in filled:
        # Footnote definitions are provenance excerpts, not node-level
        # quantitative assertions.  Their frozen source text may legitimately
        # contain operating conditions; keep those excerpts traceable while
        # enforcing the firewall on the narrative and evidence presentation.
        narrative = re.sub(r'(?m)^\[\^[a-z0-9-]+\]:.*$', '', p["body"])
        for m in QTY.finditer(narrative):
            leaks.append((p["id"],m.group(0)))
    ok(not leaks, f"数量防火墙(正文无带单位数字) ({len(leaks)}泄漏 e.g.{leaks[:5]})")
    # 5 id 引用整数性
    badid=[]
    for p in filled:
        for ref in set(IDREF.findall(p["body"])):
            if ref not in allids: badid.append((p["id"],ref))
    ok(not badid, f"正文 id 引用整数性 ({len(badid)}悬空 e.g.{badid[:4]})")
    # 6 视觉层（§3）:sigil 覆盖 + 确定性(==g(当前键)) + hash 互异
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from gen_sigil import product_sigil, activity_sigil, sigil_hash
        Pby={p["name"]:p for p in d["products"]}; aids={a["id"] for a in d["activities"]}
        sigdir=os.path.join(wikidir,"sigil")
        miss=[]; stale=[]; hashes={}
        for n in d["products"]+d["activities"]:
            nid=n["id"]; fp=os.path.join(sigdir,f"{nid}.svg")
            want=activity_sigil(n,Pby) if nid in aids else product_sigil(n)
            hashes[nid]=sigil_hash(n)
            if not os.path.exists(fp): miss.append(nid)
            elif open(fp).read()!=want: stale.append(nid)
        vals=list(hashes.values()); duph=[h for h in set(vals) if vals.count(h)>1]
        ok(not miss, f"sigil 覆盖 (每节点有 sigil/{{id}}.svg) ({len(miss)}缺 e.g.{miss[:4]})")
        ok(not stale, f"sigil == g(当前键)(键变即重生) ({len(stale)}过期 e.g.{stale[:4]})")
        ok(not duph, f"sigil-hash 互异 ({len(duph)}组重复)")
    except Exception as ex:
        ok(False, f"视觉层 lint 异常: {ex}")
    # 7 证据层(§11):受控 + 源解析 + 数量防火墙2.0 + 投入产出↔脊边对账 + 中国覆盖记分卡
    # 跨「旧合并表」与「新拆表」统一收集 (val, basis, [srcs]);老 data_type 表用 DATA_TYPES/BASES 校验
    bad_dt=[]; bad_schema=[]; bad_basis=[]; bad_src=[]; bad_region=[]; firewall2=[]; cn_cov=[]; recon=[]
    bad_owner=[]; legacy_heading=[]; bad_value_tag=[]; bad_tag_basis=[]; bad_compartment=[]
    consE=defaultdict(set); prodE=defaultdict(set)
    for e in d["edges"]:
        if e["from"] in aidset:
            (consE if e["type"]=="CONSUMES" else prodE)[e["from"]].add(e["to"])
    PID=re.compile(r'[PA]\d{3}')
    def chk(nid, val, basis, srcs, label, track=None):
        # basis 受控(若有);源解析;防火墙2.0。每条值轨只接受同地域来源。
        if basis is not None and basis not in BASES: bad_basis.append((nid,basis))
        isnull = is_null_value(val)
        # 尚无数值的收集占位不应被计成国际/中国的实测来源，也不应因占位来源
        # 的登记地域触发错轨；一旦填值，下面的来源解析、地域与防火墙检查仍完整执行。
        if isnull:
            return
        live=[s for s in srcs if not is_null_value(s)]
        for s in live:
            sid=s.split('#')[0].strip()
            if sid not in reg:
                bad_src.append((nid,sid))
                continue
            region=str(reg[sid].get("region") or "").upper()
            if track=='cn' and not region.startswith('CN'): bad_region.append((nid,label,sid,region))
            if track=='int' and region.startswith('CN'): bad_region.append((nid,label,sid,region))
        if not isnull and basis!='reference' and not live: firewall2.append((nid,label,val))
    ev_pages=set()
    for p in pages:
        # 产品只保存产品身份/物性/规格/交接质量；活动级流、排放和监测指标归生产活动。
        if p["id"] in pidset:
            forbidden=sorted(set(p["ev_typed"]) & {'flows','emissions','indicators'})
            if forbidden: bad_owner.append((p["id"],forbidden))
        # 通用方法学统一继承规则页，节点正文只写差异字段。
        if re.search(r'(?m)^#{2,6}\s+中国区 LCI 数据要求\s*$',p["body"]):
            legacy_heading.append(p["id"])
        cn_src_hit=cn_val_hit=cn_measured=cn_proxy=cn_defined=cn_tot=0
        # 旧合并表(steel A016 兼容)
        if p["evidence"].strip():
            ev_pages.add(p["id"])
            for c in ev_rows(p["evidence"]):
                if c[0] not in DATA_TYPES: bad_dt.append((p["id"],c[0]))
                chk(p["id"],c[4],c[3],[c[5],c[6]],c[1])
                cn_src_hit += 0 if is_null_value(c[6]) else 1
                cn_tot += 1
        # 新拆表
        for kind,block in p["ev_typed"].items():
            ev_pages.add(p["id"]); spec=TSPEC[kind]
            header,rows=typed_table(block)
            if header not in spec['headers']:
                bad_schema.append((p["id"],kind,"非规范表头",header))
                continue
            width_bad=[(i+1,len(c),len(header)) for i,c in enumerate(rows) if len(c)!=len(header)]
            if width_bad:
                bad_schema.append((p["id"],kind,"行宽不一致",width_bad[:3]))
                rows=[c for c in rows if len(c)==len(header)]
            basis_i=col(header,lambda h:h=='basis' or h.startswith('口径'))
            legacy_val_i=col(header,lambda h:h=='值')
            int_val_i=col(header,lambda h:'国际值' in h)
            cn_val_i=col(header,lambda h:'中国值' in h or '中国项目值' in h)
            int_src_i=col(header,lambda h:'国际源' in h)
            cn_src_i=col(header,lambda h:'中国源' in h)
            generic_src_i=col(header,lambda h:h=='源' or (h.endswith('源') and '国际源' not in h and '中国源' not in h))
            compartment_i=col(header,lambda h:h=='compartment')
            # 旧拆表的“值 + 国际源 + 中国源”仍可读；新表则强制值与同地域来源成对校验。
            for c in rows:
                basis=c[basis_i] if basis_i is not None and basis_i<len(c) else None
                label=c[0]
                # 地域数值必须显式声明实测/代理/定义；标签与口径不能自相矛盾。
                for vi in (int_val_i,cn_val_i):
                    if vi is None or is_null_value(c[vi]): continue
                    mt=VALUE_TAG.match(c[vi].strip())
                    if not mt:
                        bad_value_tag.append((p["id"],kind,label,c[vi]))
                    elif mt.group(1)=='实测值' and basis not in {'measured_average','industry_average'}:
                        bad_tag_basis.append((p["id"],label,'实测值',basis))
                    elif mt.group(1)=='代理值' and basis in {
                            'measured_average','industry_average','reference','standard_spec'}:
                        bad_tag_basis.append((p["id"],label,'代理值',basis))
                    elif mt.group(1)=='定义值' and basis not in {'reference','standard_spec'}:
                        bad_tag_basis.append((p["id"],label,'定义值',basis))
                # 基本流表只接受自然环境 compartment；废物与分析指标必须留在 flows/indicators。
                if kind=='emissions' and compartment_i is not None:
                    comp=c[compartment_i].strip().lower()
                    root=re.split(r'[/>:]',comp,1)[0].strip()
                    if root not in {'air','water','soil','resource'}:
                        bad_compartment.append((p["id"],label,c[compartment_i]))
                    elif NON_ELEMENTARY_LABEL.search(label):
                        bad_compartment.append((p["id"],label,"聚合指标/技术系统物流不得作基本流"))
                if int_val_i is not None:
                    chk(p["id"],c[int_val_i],basis,[c[int_src_i]] if int_src_i is not None else [],f"{label}/INT",'int')
                if cn_val_i is not None:
                    chk(p["id"],c[cn_val_i],basis,[c[cn_src_i]] if cn_src_i is not None else [],f"{label}/CN",'cn')
                if legacy_val_i is not None:
                    srcs=[c[i] for i in (int_src_i,cn_src_i,generic_src_i) if i is not None]
                    chk(p["id"],c[legacy_val_i],basis,srcs,label)
                if spec['dual']:
                    cn_tot+=1
                    if cn_src_i is not None and not is_null_value(c[cn_src_i]): cn_src_hit+=1
                    if cn_val_i is not None and not is_null_value(c[cn_val_i]):
                        cn_val_hit+=1
                        tag=re.match(r'^〔(实测值|代理值|定义值)〕',c[cn_val_i].strip())
                        if tag:
                            if tag.group(1)=='实测值': cn_measured+=1
                            elif tag.group(1)=='代理值': cn_proxy+=1
                            elif tag.group(1)=='定义值': cn_defined+=1
            # 投入产出表 ↔ 脊 consumes/produces 边对账(仅活动页)
            if kind=='flows' and p["id"] in aidset:
                ins=set(); outs=set()
                flow_i=col(header,lambda h:h=='流')
                dir_i=col(header,lambda h:h in ('方向','direction'))
                for c in rows:
                    ids=set(PID.findall(c[flow_i])) if flow_i is not None else set()
                    d_=c[dir_i].strip().lower() if dir_i is not None else ''
                    (ins if d_ in ('in','投入','输入') else outs).update(ids)
                mc=consE[p["id"]]-ins; ec=ins-consE[p["id"]]; mp=prodE[p["id"]]-outs; ep=outs-prodE[p["id"]]
                if mc or ec or mp or ep:
                    recon.append((p["id"],f"投入缺{sorted(mc)}多{sorted(ec)}/产出缺{sorted(mp)}多{sorted(ep)}"))
        if cn_tot: cn_cov.append((p["id"],cn_src_hit,cn_val_hit,cn_measured,cn_proxy,cn_defined,cn_tot))
    ok(not bad_dt, f"证据 data_type 受控(旧表) ({len(bad_dt)}越界 e.g.{bad_dt[:3]})")
    ok(not bad_schema, f"拆表 schema 规范(表头精确+值源成对唯一+行宽一致) ({len(bad_schema)}违规 e.g.{bad_schema[:2]})")
    ok(not bad_owner, f"产品/活动证据表归属正确 ({len(bad_owner)}越界 e.g.{bad_owner[:3]})")
    ok(not legacy_heading, f"节点只写特定采集字段(无重复通用规则标题) ({len(legacy_heading)}旧标题 e.g.{legacy_heading[:3]})")
    ok(not bad_basis, f"证据 口径(basis) 受控 ({len(bad_basis)}越界 e.g.{bad_basis[:3]})")
    ok(not bad_value_tag, f"地域数值含实测/代理/定义标签 ({len(bad_value_tag)}缺标签 e.g.{bad_value_tag[:3]})")
    ok(not bad_tag_basis, f"数值标签与口径一致 ({len(bad_tag_basis)}冲突 e.g.{bad_tag_basis[:3]})")
    ok(not bad_compartment, f"基本流环境介质受控(废物/指标不混入) ({len(bad_compartment)}违规 e.g.{bad_compartment[:3]})")
    ok(not bad_src, f"证据 源解析到 registry ({len(bad_src)}未解析 e.g.{bad_src[:3]})")
    ok(not bad_region, f"国际/中国值轨与来源地域一致 ({len(bad_region)}错轨 e.g.{bad_region[:3]})")
    ok(not firewall2, f"数量防火墙2.0(有值必有源+口径) ({len(firewall2)}泄漏 e.g.{firewall2[:3]})")
    ok(not recon, f"投入产出表↔脊边对账 ({len(recon)}不符 e.g.{recon[:2]})")
    # 中国覆盖记分卡(advisory,只统计双轨表行)
    if cn_cov:
        tot=sum(t for *_,t in cn_cov)
        src_hit=sum(x[1] for x in cn_cov); val_hit=sum(x[2] for x in cn_cov)
        measured=sum(x[3] for x in cn_cov); proxy=sum(x[4] for x in cn_cov); defined=sum(x[5] for x in cn_cov)
        print(f"\n  〔中国覆盖记分卡 advisory〕双轨表 CN 值 {val_hit}/{tot} ({100*val_hit//max(tot,1)}%)"
              f" · 其中实测 {measured} / 代理 {proxy} / 定义 {defined}"
              f" · CN 源 {src_hit}/{tot} ({100*src_hit//max(tot,1)}%) · 含证据页 {len(ev_pages)}")
        for nid,s,v,m,p,d,t in cn_cov:
            if s<t or v<t:
                print(f"     {nid}: CN 值 {v}/{t} (实测{m}/代理{p}/定义{d}) · 源 {s}/{t}"
                      f"  (待采值 {t-v} 行/待补本土源 {t-s} 行;单轨物性表不计)")
    # 8 溯源门(Provenance gates)——把 body_status=reviewed 从"可手填标签"升级为"受核验保证"的状态。
    #   设计:draft 页允许挂 seed-unverified 老标签(诚实的"未核实"态,不破坏现存2245页);reviewed 才是被门把守的升级态。
    VERIFIED={sid for sid,s in reg.items() if s.get("status")=="verified"}
    controlled_citations={}
    if coverage_path:
        try:
            coverage_for_citations=json.load(open(coverage_path,encoding='utf-8'))
            for node in coverage_for_citations.get('nodes',[]):
                allowed=set()
                for claim in node.get('claims',[]):
                    if claim.get('disposition')=='controlled_internal':
                        allowed.update(str(x) for x in claim.get('citations',[]) if x)
                controlled_citations[str(node.get('node_id',''))]=allowed
        except Exception:
            controlled_citations={}
    INLINE_CITE=re.compile(r'\[\^([a-z0-9\-]+)\](?!:)')   # 行内引用(负向lookahead排除脚注定义 [^x]: )
    # 8a 全局:声称 verified 的源必须带真实证据(locator非空 + excerpt_seeds有内容)——防"空心verified"
    hollow=[]
    for sid in sorted(VERIFIED):
        s=reg[sid]; loc=(s.get("locator") or "").strip()
        seeds=[x for x in (s.get("excerpt_seeds") or []) if (x or "").strip()]
        if not loc or not seeds: hollow.append(sid)
    ok(not hollow, f"verified源必带证据(locator+摘录非空) ({len(hollow)}空心 e.g.{hollow[:3]})")
    # 8b reviewed页:所有行内引用必须解析到 status=verified 源(禁装饰性引用——堵死"结构引用≠已核实"失败模式)
    rv_bad=[]
    for p in pages:
        if p["body_status"]!="reviewed": continue
        for c in sorted(set(INLINE_CITE.findall(p["body"]))):
            if c not in VERIFIED and c not in controlled_citations.get(p["id"],set()):
                rv_bad.append((p["id"],c))
    ok(not rv_bad, f"reviewed页行内引用为verified或coverage受控内部来源 ({len(rv_bad)}违规 e.g.{rv_bad[:3]})")
    # 8c reviewed页:至少1条 verified 行内引用(不许空口升级 reviewed)
    rv_empty=[p["id"] for p in pages if p["body_status"]=="reviewed"
              and not any(c in VERIFIED for c in set(INLINE_CITE.findall(p["body"])))]
    ok(not rv_empty, f"reviewed页含≥1条verified引用 ({len(rv_empty)}空 e.g.{rv_empty[:3]})")
    # 8d 可选 claim coverage 强门。旧 CLI 不传 artifact 时保持兼容；一旦传入，
    # reviewed/claim_verified/complete 不能靠手填，必须有当前 BODY 的完整覆盖证明。
    if coverage_path:
        try:
            coverage=json.load(open(coverage_path,encoding='utf-8'))
            coverage_bad=coverage_proof_errors(pages,coverage)
            ok(not coverage_bad,
               f"reviewed/claim_verified页有当前BODY完整覆盖证明 ({len(coverage_bad)}违规 e.g.{coverage_bad[:3]})")
        except Exception as ex:
            ok(False,f"claim coverage artifact 无效: {ex}")
    # 9 wiki-v2 收口门：旧页继续兼容迁移；声明 v2 的页必须真正满足冻结结构、
    #    证据表职责、来源同步和状态分层，不能只改一个 schema 标签。
    REQUIRED_HEADINGS={
        "product":[
            "定义与产品身份","性质与形态","参考流与交接边界","规格与相邻节点区分",
            "在系统中的角色","分类与适用范围","节点特定采集字段","区域化补充要求",
            "数据适用状态与缺口","出处",
        ],
        "activity":[
            "定义与参考活动","参考产品与参考单位","单元过程边界","技术路线与相邻活动区分",
            "投入产出与脊边对账","直接排放、废物与监测指标边界","节点特定采集字段",
            "区域化补充要求","数据适用状态与缺口","出处",
        ],
    }
    REQUIRED_TABLES={
        "product":{"props","params","quality"},
        "activity":{"flows","emissions","indicators","params","quality"},
    }
    OPTIONAL_TABLES={"product":set(),"activity":{"props"}}
    coverage_ids=set()
    if coverage_path:
        try:
            coverage_ids={str(n.get('node_id','')) for n in json.load(open(coverage_path,encoding='utf-8')).get('nodes',[])}
        except Exception:
            coverage_ids=set()
    privileged_v1=[p["id"] for p in pages if p["id"] in coverage_ids and (
        p["body_status"]=="reviewed"
        or p["provenance_status"]=="claim_verified"
        or p["claim_verification_status"]=="complete"
    ) and p["schema_version"]!="wiki-v2"]
    ok(not privileged_v1,
       f"reviewed/claim_verified/complete 页面强制 wiki-v2 ({len(privileged_v1)}违规 e.g.{privileged_v1[:3]})")
    v2=[p for p in pages if p["schema_version"]=="wiki-v2"]
    v2_headings=[]; v2_tables=[]; v2_refs=[]; v2_status=[]
    for p in v2:
        headings=re.findall(r'(?m)^##\s+(.+?)\s*$',p["body"])
        if headings!=REQUIRED_HEADINGS.get(p["node_type"],[]):
            v2_headings.append((p["id"],headings))
        actual=set(p["ev_typed"])
        required=REQUIRED_TABLES.get(p["node_type"],set())
        allowed=required|OPTIONAL_TABLES.get(p["node_type"],set())
        if not required<=actual or not actual<=allowed:
            v2_tables.append((p["id"],sorted(actual)))
        used=set(INLINE_CITE.findall(p["body"]))
        for block in p["ev_typed"].values():
            header,rows=typed_table(block)
            source_cols=[i for i,h in enumerate(header) if h=="源" or h.endswith("源") or "源 " in h]
            for row in rows:
                for i in source_cols:
                    if i>=len(row) or is_null_value(row[i]): continue
                    used.add(row[i].split("#",1)[0].strip())
        declared=set(p["prov_refs"])
        if used!=declared:
            v2_refs.append((p["id"],"缺"+str(sorted(used-declared)),"多"+str(sorted(declared-used))))
        required_status={
            "structure_status":"conformant",
            "dataset_readiness":None,
            "change_log_status":"recorded",
        }
        bad=[k for k,want in required_status.items()
             if not p.get(k) or (want is not None and p.get(k)!=want)]
        if p["provenance_status"] not in {"source_verified","claim_verified"}:
            bad.append("provenance_status")
        if p["claim_verification_status"] not in {"not_started","partial","complete"}:
            bad.append("claim_verification_status")
        if p["quantity_status"] not in {"not_populated","partial","populated"}:
            bad.append("quantity_status")
        if p["body_status"]=="reviewed" and (
                p["provenance_status"]!="claim_verified"
                or p["claim_verification_status"]!="complete"):
            bad.append("reviewed_requires_complete_claim_verification")
        if "未核实·模型回忆" in p["body"]:
            bad.append("model_recall_forbidden_in_v2_body")
        if bad: v2_status.append((p["id"],sorted(set(bad))))
    ok(not v2_headings, f"wiki-v2 固定章节完整且有序 ({len(v2_headings)}违规 e.g.{v2_headings[:2]})")
    ok(not v2_tables, f"wiki-v2 固定证据表齐全 ({len(v2_tables)}违规 e.g.{v2_tables[:2]})")
    ok(not v2_refs, f"wiki-v2 正文/证据来源与frontmatter同步 ({len(v2_refs)}违规 e.g.{v2_refs[:2]})")
    ok(not v2_status, f"wiki-v2 结构/溯源/断言/数量状态分层有效 ({len(v2_status)}违规 e.g.{v2_status[:2]})")
    # 报告
    npass=sum(1 for b,_ in R if b)
    print(f"\n{'='*60}\n  wiki lint · {wikidir}  (已填肉 {len(filled)}/{len(pages)} 页)\n{'='*60}")
    for b,m in R: print(("  ✅ " if b else "  ❌ ")+m)
    print(f"  → {npass}/{len(R)} 通过")
    return npass==len(R)

if __name__=="__main__":
    # 向后兼容原三位置参数；新增既接受第4位置参数，也接受 --coverage FILE。
    argv=sys.argv[1:]
    coverage=None
    if '--coverage' in argv:
        i=argv.index('--coverage')
        if i+1>=len(argv):
            print('❌ --coverage 缺文件路径',file=sys.stderr); sys.exit(2)
        coverage=argv[i+1]; del argv[i:i+2]
    if len(argv)==4 and coverage is None:
        coverage=argv.pop()
    if len(argv)!=3:
        print('用法: wiki_lint.py <graph.json> <wiki_dir> <registry.json> [--coverage coverage.json]',file=sys.stderr)
        sys.exit(2)
    okall=main(argv[0],argv[1],argv[2],coverage)
    sys.exit(0 if okall else 1)
