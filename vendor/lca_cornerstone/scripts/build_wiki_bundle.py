#!/usr/bin/env python3
"""把 wiki/<industry> 的节点页打包成浏览器可用的数据包（离线，<script src> 加载）。
预渲染正文 markdown -> HTML；mermaid 邻域图单独存。
production 数据包仍由 release gate 后的 publish 命令生成；``--mode preview`` 使用独立文件名和
全局变量，允许查看 draft，但绝不覆盖 production bundle。

用法:
  python3 scripts/build_wiki_bundle.py wiki/steel docs/steel-wiki-data.js STEEL_WIKI
  python3 scripts/build_wiki_bundle.py wiki/steel docs/steel-wiki-preview-data.js STEEL_WIKI_PREVIEW --mode preview
"""
import argparse, sys, os, re, glob, json, html
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_sigil import load_graph, product_sigil, activity_sigil
from gen_node_diagram import neighborhood_svg

def esc(s): return html.escape(s, quote=False)

def source_url(source):
    """Return the canonical original URL stored by the evidence registry.

    New registry records have a dedicated ``url`` field.  Older records kept
    the URL at the start of ``locator``; retain that as a compatibility
    fallback, but never discard an explicit URL merely because the locator is
    a human-readable section name.
    """
    explicit=str(source.get("url") or "").strip()
    if re.match(r'^https?://', explicit):
        return explicit
    locator=str(source.get("locator") or "")
    match=re.search(r'https?://[^\s；，]+', locator)
    return match.group(0) if match else ""

def md_inline(s):
    # ``modeling_judgment`` remains fully classified in Markdown/KU/coverage,
    # but repeating its badge and the same internal-review footnote after
    # every sentence makes the production reader unusable.  Collapse only
    # this exact paired presentation marker.  External ``ku-*`` citations,
    # graph facts and evidence-gap warnings remain visible.
    s=re.sub(r'\s*〔建模判断〕\s*\[\^internal-review\]', '', s)
    # Reviewed authority stays in the Markdown/KU ledger.  In the reader the
    # adjacent source link already conveys and opens that authority, so the
    # repeated green badge is redundant visual noise.
    s=re.sub(r'\s*✅已核实(?=\s*\[\^ku-[a-z0-9\-]+\])', '', s)
    s=esc(s)
    s=re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    s=re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
    s=re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)',
             r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    # 建模判断 标记
    s=re.sub(r'〔建模判断〕', r'<span class="judg">〔建模判断〕</span>', s)
    # 脚注仍解析到真实 source id，但阅读面只显示短标签；完整 id
    # 保留在 href/title 中，避免长 ku 哈希打断段落换行。
    def citation(match):
        source_id=match.group(1)
        label=('来源' if source_id.startswith('ku-') else
               '图谱' if source_id=='internal-graph' else
               '方法说明' if source_id=='internal-review' else '来源')
        return (f'<sup class="fnref"><a href="#source-{source_id}" '
                f'title="{source_id}">{label}</a></sup>')
    s=re.sub(r'\[\^([a-z0-9\-]+)\]', citation, s)
    # 节点 id 高亮（P000/A000）
    s=re.sub(r'\b([PA]\d{3})\b', r'<span class="nid" data-goto="\1">\1</span>', s)
    return s

def md_to_html(body):
    # 脚注定义只用于证据契约与来源注册表对账。阅读器已有结构化的
    # “引用原文与核验记录”面板，不再重复渲染一套“出处”。
    lines=body.split('\n'); out=[]; ul=None
    def closeul():
        nonlocal ul
        if ul is not None: out.append('</ul>'); ul=None
    in_foot=False
    for ln in lines:
        raw=ln.rstrip()
        mfoot=re.match(r'^\[\^([a-z0-9\-]+)\]:\s*(.*)$', raw)
        if mfoot:
            continue
        if re.match(r'^##\s*出处', raw):  # 出处标题：跳过(脚注单独渲染)
            in_foot=True; continue
        m4=re.match(r'^####\s+(.*)', raw); m3=re.match(r'^###\s+(.*)', raw); m2=re.match(r'^##\s+(.*)', raw)
        if m2 or m3 or m4:
            closeul()
            t=(m2 or m3 or m4).group(1)
            tag='h3' if m2 else ('h4' if m3 else 'h5')
            out.append(f'<{tag}>{md_inline(t)}</{tag}>'); continue
        mb=re.match(r'^[-*]\s+(.*)', raw)
        if mb:
            if ul is None: out.append('<ul>'); ul=True
            out.append(f'<li>{md_inline(mb.group(1))}</li>'); continue
        closeul()
        if raw.strip()=='':
            continue
        out.append(f'<p>{md_inline(raw)}</p>')
    closeul()
    return '\n'.join(out)

def md_with_tables(body):
    """Render the small Markdown subset used by LCA dataset association blocks."""
    lines=body.splitlines()
    out=[]
    plain=[]
    def flush_plain():
        if plain:
            out.append(md_to_html('\n'.join(plain)))
            plain.clear()
    i=0
    while i < len(lines):
        if (lines[i].lstrip().startswith('|') and i+1 < len(lines)
                and re.match(r'^\s*\|(?:\s*:?-+:?\s*\|)+\s*$', lines[i+1])):
            flush_plain()
            head=[x.strip() for x in lines[i].strip().strip('|').split('|')]
            i+=2
            rows=[]
            while i < len(lines) and lines[i].lstrip().startswith('|'):
                rows.append([x.strip() for x in lines[i].strip().strip('|').split('|')])
                i+=1
            thead=''.join(f'<th>{md_inline(x)}</th>' for x in head)
            tbody=''.join(
                '<tr>'+''.join(f'<td>{md_inline(x)}</td>' for x in row)+'</tr>'
                for row in rows
            )
            out.append(
                '<div class="lca-assoc-table"><table><thead><tr>'+thead+
                '</tr></thead><tbody>'+tbody+'</tbody></table></div>'
            )
            continue
        plain.append(lines[i])
        i+=1
    flush_plain()
    style=(
        '<style>.lca-assoc{margin-top:22px;border-top:1px solid var(--line);padding-top:8px}'
        '.lca-assoc-table{overflow-x:auto;margin:10px 0 14px}'
        '.lca-assoc-table table{width:100%;min-width:860px;border-collapse:collapse;font-size:12px}'
        '.lca-assoc-table th{color:var(--dim);font-weight:600;text-align:left;padding:6px 8px;'
        'border-bottom:1px solid var(--cyan2)}'
        '.lca-assoc-table td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}'
        '.lca-assoc-table a{color:var(--cyan2)}</style>'
    )
    return style+'<div class="lca-assoc">'+''.join(out)+'</div>'

_EVNULL={'待采','待核','待评','待算','—','-','','na','n/a','tbd','not_populated'}
def ev_is_null(value):
    normalized=value.strip().lower()
    return (normalized in _EVNULL or normalized.startswith('缺口：')
            or normalized.startswith('缺口:') or normalized.startswith('未公开'))
def render_evidence(block):
    """§11 证据表 markdown -> HTML(双轨:国际源青/中国源琥珀;值默认待采)。纯展示,不影响 lint。"""
    rows=[]
    for ln in block.splitlines():
        ln=ln.strip()
        if not ln.startswith('|'): continue
        c=[x.strip() for x in ln.strip('|').split('|')]
        if len(c)<8 or c[0]=='data_type' or set(''.join(c))<=set('-: '): continue
        rows.append(c[:8])
    if not rows: return ''
    cn=sum(1 for c in rows if c[6].strip().lower() not in _EVNULL)
    head=''.join(f'<th>{h}</th>' for h in ['数据类型','流/项','单位','口径','值','国际源 INT','中国源 CN','质量'])
    def srccell(s,var):
        s=s.strip()
        return f'<span class="evgap">—</span>' if s.lower() in _EVNULL else f'<code style="color:var(--{var})">{esc(s)}</code>'
    trs=[]
    for dt,item,unit,basis,val,si,sci,ped in rows:
        valc='<span class="evnull">待采</span>' if val.strip().lower() in _EVNULL else f'<b>{esc(val)}</b>'
        trs.append('<tr>'+f'<td><span class="evpill">{esc(dt)}</span></td><td>{md_inline(item)}</td>'
            f'<td class="evdim">{esc(unit)}</td><td><span class="evbasis">{esc(basis)}</span></td>'
            f'<td>{valc}</td><td>{srccell(si,"cyan")}</td><td>{srccell(sci,"amber")}</td>'
            f'<td class="evdim">{esc(ped)}</td></tr>')
    cov=f' <span class="evcov">中国轨 {cn}/{len(rows)}</span>'
    return (f'<div class="evtbl"><h4>证据表 · 消耗/排放（双轨）{cov}</h4>'
        f'<table class="evtab"><thead><tr>{head}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>')

_EV_STYLE=('.evsec{margin-top:22px}.evsec>h3{display:flex;align-items:center;gap:9px;margin-bottom:4px}'
    '.evtbl{margin-top:14px;overflow-x:auto}.evtbl h4{margin:0 0 6px;display:flex;align-items:center;flex-wrap:wrap;gap:7px;color:var(--cyan2);font-size:13px;font-weight:500}'
    '.evcov{font:500 11px/1 monospace;border:1px solid var(--line);border-radius:8px;padding:3px 8px}'
    '.evcov-int{color:var(--cyan);background:color-mix(in srgb,var(--cyan) 7%,transparent)}'
    '.evcov-cn{color:var(--amber);background:color-mix(in srgb,var(--amber) 7%,transparent)}'
    '.evnote{color:var(--faint);font-size:12px;margin:2px 0 6px}'
    '.evtab{width:100%;min-width:960px;border-collapse:collapse;font-size:12px}'
    '.evtab th{color:var(--dim);font-weight:500;text-align:left;padding:5px 7px;border-bottom:1px solid var(--cyan2);white-space:nowrap}'
    '.evtab td{padding:5px 7px;border-bottom:1px solid var(--line);vertical-align:top}'
    '.evth-int,.evcell-int{border-left:1px solid color-mix(in srgb,var(--cyan) 40%,var(--line));background:color-mix(in srgb,var(--cyan) 4%,transparent)}'
    '.evth-cn,.evcell-cn{border-left:1px solid color-mix(in srgb,var(--amber) 40%,var(--line));background:color-mix(in srgb,var(--amber) 4%,transparent)}'
    '.evth-int{color:var(--cyan)!important}.evth-cn{color:var(--amber)!important}'
    '.evval-int{color:var(--cyan)}.evval-cn{color:var(--amber)}'
    '.evtag{display:inline-block;margin:0 5px 2px 0;padding:2px 6px;border-radius:999px;font:600 10px/1 monospace;white-space:nowrap}'
    '.evtag-measured{color:var(--green);border:1px solid color-mix(in srgb,var(--green) 45%,transparent);background:color-mix(in srgb,var(--green) 10%,transparent)}'
    '.evtag-proxy{color:var(--amber);border:1px solid color-mix(in srgb,var(--amber) 45%,transparent);background:color-mix(in srgb,var(--amber) 10%,transparent)}'
    '.evtag-defined{color:var(--violet);border:1px solid color-mix(in srgb,var(--violet) 45%,transparent);background:color-mix(in srgb,var(--violet) 10%,transparent)}'
    '.evpill{background:var(--panel2);border:1px solid var(--line);border-radius:5px;padding:1px 6px;font:11px monospace;color:var(--dim)}'
    '.evbasis{color:var(--violet);font:11px monospace}.evnull{color:var(--faint)}.evgap{color:var(--faint)}.evdim{color:var(--faint)}')
_EV_TITLE={'flows':'投入产出表 · 技术系统产品/废物流（与脊边对账）',
           'emissions':'直接环境排放表 · 基本流（物质×介质）',
           'indicators':'废气/废水监测指标表 · 待映射',
           'quality':'数据质量与代表性字段 · 不允许代理',
           'props':'物性/常数表 · 来源限定','params':'规格/工艺参数表'}
_EV_VALUE_TAGS={'实测值':('measured','实测值'),'代理值':('proxy','代理值'),'定义值':('defined','定义值')}
def render_value(cell, valcls=''):
    """值单元格前缀 `〔实测值|代理值|定义值〕` -> 可视标签；标记仍保留在 Markdown 数据契约中。"""
    s=cell.strip()
    m=re.match(r'^〔(实测值|代理值|定义值)〕\s*(.*)$', s)
    if not m:
        return f'<b class="{valcls}">{esc(s)}</b>' if valcls else f'<b>{esc(s)}</b>'
    kind,label=_EV_VALUE_TAGS[m.group(1)]
    value=m.group(2).strip()
    b=f'<b class="{valcls}">{esc(value)}</b>' if valcls else f'<b>{esc(value)}</b>'
    return f'<span class="evtag evtag-{kind}">{label}</span>{b}'

def render_typed(t):
    """§11 拆表 markdown -> HTML;国际值/源青、中国值/源琥珀，数值归属显式。纯展示。"""
    blocks=[]
    node_type_match=re.search(r'(?m)^node_type:\s*(product|activity)\s*$', t)
    node_type=node_type_match.group(1) if node_type_match else ''
    # wiki-v2 的页面顺序按节点职责固定：产品先解释物性和规格，再呈现代表性；
    # 活动先呈现技术系统/基本流，再呈现工艺参数和活动数据质量。
    if node_type=='product':
        kinds=('props','params','quality')
    elif node_type=='activity':
        kinds=('flows','props','params','emissions','indicators','quality')
    else:
        kinds=('flows','emissions','indicators','props','params','quality')
    for kind in kinds:
        m=re.search(rf'<!-- EV:{kind}:START -->(.*?)<!-- EV:{kind}:END -->', t, re.S)
        if not m: continue
        rows=[]
        for ln in m.group(1).splitlines():
            ln=ln.strip()
            if not ln.startswith('|'): continue
            c=[x.strip() for x in ln.strip('|').split('|')]
            if set(''.join(c))<=set('-: '): continue
            rows.append(c)
        if len(rows)<2: continue
        header,data=rows[0],rows[1:]
        roles=[
            'int_val' if '国际值' in h else
            'cn_val' if ('中国值' in h or '中国项目值' in h) else
            'int_src' if '国际源' in h else
            'cn_src' if '中国源' in h else
            'src' if h.endswith('源') else
            'val' if h=='值' else
            'basis' if 'basis' in h or h.startswith('口径') else ''
            for h in header
        ]
        counts={k:{'value':0,'source':0,'gap_source':0,'total':0,'measured':0,'proxy':0,'defined':0} for k in ('int','cn')}; trs=[]
        tracked={r.split('_',1)[0] for r in roles if r in ('int_val','cn_val','int_src','cn_src')}
        for track in tracked:
            counts[track]['total']=len(data)
        for c in data:
            tds=[]
            for i in range(len(header)):
                cell=c[i] if i<len(c) else ''; r=roles[i]; isnull=ev_is_null(cell)
                if r in ('int_val','cn_val'):
                    track=r.split('_',1)[0]
                    counts[track]['value']+=(0 if isnull else 1)
                    mt=re.match(r'^〔(实测值|代理值|定义值)〕', cell.strip())
                    if mt:
                        counts[track][_EV_VALUE_TAGS[mt.group(1)][0]]+=1
                    cls=f'evcell-{track}'
                    valcls=f'evval-{track}'
                    null_label=cell.strip() or '待采'
                    tds.append(f'<td class="{cls}">'+(f'<span class="evnull">{esc(null_label)}</span>' if isnull else render_value(cell,valcls))+'</td>')
                elif r=='val':
                    null_label=cell.strip() or '待采'
                    tds.append('<td>'+(f'<span class="evnull">{esc(null_label)}</span>' if isnull else render_value(cell))+'</td>')
                elif r in ('int_src','cn_src','src'):
                    track=r.split('_',1)[0] if '_' in r else ''
                    if track in counts and not isnull:
                        value_role=f'{track}_val'
                        value_i=roles.index(value_role) if value_role in roles else None
                        value_is_null=(value_i is None or value_i>=len(c) or ev_is_null(c[value_i]))
                        counts[track]['gap_source' if value_is_null else 'source']+=1
                    col={'int_src':'--cyan','cn_src':'--amber','src':'--dim'}[r]
                    sid=cell.split('#',1)[0].strip()
                    cls=f' class="evcell-{track}"' if track else ''
                    tds.append(f'<td{cls}>'+('<span class="evgap">—</span>' if isnull else
                        f'<a href="#source-{esc(sid)}"><code style="color:var({col})">{esc(cell)}</code></a>')+'</td>')
                elif r=='basis':
                    tds.append(f'<td><span class="evbasis">{esc(cell)}</span></td>')
                elif i==0:
                    tds.append(f'<td>{md_inline(cell)}</td>')
                else:
                    tds.append(f'<td class="evdim">{esc(cell)}</td>')
            trs.append('<tr>'+''.join(tds)+'</tr>')
        badges=[]
        for track,label in (('int','国际'),('cn','中国')):
            co=counts[track]
            if co['total']:
                kinds=''.join(
                    f' · {label_} {co[key]}' for key,label_ in
                    (('measured','实测'),('proxy','代理'),('defined','定义')) if co[key])
                evidence=(f' · {label}数值源 {co["source"]}/{co["total"]}' if co['source'] else '')
                gaps=(f' · 缺口证据 {co["gap_source"]}/{co["total"]}' if co['gap_source'] else '')
                badges.append(
                    f'<span class="evcov evcov-{track}">{label}值 {co["value"]}/{co["total"]}'
                    f'{kinds}{evidence}{gaps}</span>')
        cov=(' '+''.join(badges)) if badges else ''
        th=''.join(
            f'<th scope="col" class="{"evth-int" if r.startswith("int_") else "evth-cn" if r.startswith("cn_") else ""}">{esc(h)}</th>'
            for h,r in zip(header,roles))
        blocks.append(f'<div class="evtbl"><h4>{_EV_TITLE[kind]}{cov}</h4>'
            f'<table class="evtab"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>')
    return ''.join(blocks)   # 各表分别展示,无统一 umbrella

def assemble_body(body, full_text):
    """按作用重排版；出处由独立、可跳转的来源面板统一呈现。"""
    parts=re.split(r'(?m)^##[ \t]+(.+?)[ \t]*$', body)
    # wiki-v2 的固定章节既是 Markdown 数据契约，也是用户可见的信息架构。
    # v1 继续走下方兼容分桶；v2 必须保持章节原顺序，不能被标题关键词重排。
    if re.search(r'(?m)^schema_version:\s*wiki-v2\s*$', full_text):
        ordered=[]
        it=parts[1:]
        for i in range(0,len(it)-1,2):
            title=it[i].strip(); content=it[i+1]
            if title=="出处":
                continue
            html=f'<h3>{esc(title)}</h3>'+md_to_html(content)
            ordered.append((title,html))
        dt=render_typed(full_text)
        data_html=(f'<style>{_EV_STYLE}</style>'+dt) if dt else ''
        node_type_match=re.search(r'(?m)^node_type:\s*(product|activity)\s*$', full_text)
        node_type=node_type_match.group(1) if node_type_match else ''
        diagram_after=("技术路线与相邻活动区分" if node_type=="activity" else "在系统中的角色")
        data_after=("直接排放、废物与监测指标边界" if node_type=="activity" else "区域化补充要求")
        out=[]
        for title,html in ordered:
            out.append(html)
            if title==diagram_after: out.append('<!--DIAGRAM-->')
            if title==data_after and data_html: out.append(data_html)
        return ''.join(out)
    secs=[]
    it=parts[1:]
    for i in range(0,len(it)-1,2):
        title=it[i].strip(); content=it[i+1]
        if title.startswith('出处'):
            continue
        secs.append((title, content))
    b={'def':[],'proc':[],'io':[],'sec':[],'back':[]}
    for title,content in secs:
        html=f'<h3>{esc(title)}</h3>'+md_to_html(content)
        if any(k in title for k in ('定义','性质','形态')): z='def'
        elif ('工艺' in title or '制造' in title): z='proc'
        elif any(k in title for k in ('投入','产出','角色')): z='io'
        elif any(k in title for k in ('监测','检测','对表','对照','分类')): z='sec'
        elif any(k in title for k in ('建模','笔记','开放','问题','讨论')): z='back'
        else: z='def'
        b[z].append(html)
    dt=render_typed(full_text)
    if not dt:
        evm=re.search(r'<!-- EVIDENCE:START -->(.*?)<!-- EVIDENCE:END -->', full_text, re.S)
        dt=render_evidence(evm.group(1)) if evm else ''
    data_html=(f'<style>{_EV_STYLE}</style>'+dt) if dt else ''
    return (''.join(b['def'])+''.join(b['proc'])+'<!--DIAGRAM-->'+''.join(b['io'])
            +data_html+''.join(b['sec'])+''.join(b['back']))

def parse_fm(fm):
    d={}
    for line in fm.split('\n'):
        m=re.match(r'^(\w+):\s*(.*)$', line)
        if m: d[m.group(1)]=m.group(2).strip()
    return d

def lca_block(l, via=None):
    """LCA 背景库挂载块。三类:活动=工序级 / 背景叶=摇篮到门直挂 / 前景产品=经由活动组合。"""
    if l and l.get('url'):
        kind=l.get('kind')
        title=('🔗 背景数据集 · 工序级(本活动的单元过程)' if kind=='process'
               else '🔗 背景数据集 · 摇篮到门(背景叶子 · 直挂)' if kind=='cradle_to_gate'
               else '🔗 背景数据集')
        badge='✅ 可信' if l.get('tier')=='ok' else '◐ 待复核'
        return ('<div class="lca-ref"><h3>'+title+' ('+badge+')</h3>'
                '<p><a href="'+esc(l.get('url',''))+'" target="_blank" rel="noopener">'+esc(l.get('name',''))+'</a>'
                ' · <code>'+esc(l.get('source',''))+' '+esc(l.get('version',''))+' / cut_off</code>'
                ' · 相似度 '+esc(str(l.get('score','')))+'</p></div>')
    if via and via.get('via_activity'):
        ds=via.get('activity_dataset',''); dsurl=via.get('activity_dataset_url','')
        link=('<a href="'+esc(dsurl)+'" target="_blank" rel="noopener">'+esc(ds)+'</a>'
              if dsurl and ds else '<em>该活动暂无工序数据集(链路缺口,待 ISIC 精绑)</em>')
        return ('<div class="lca-ref"><h3>🔗 背景 = 经由生产活动组合(前景/中间产品 · 非直挂)</h3>'
                '<p>本产品不直挂数据集;背景足迹 = 生产活动的工序数据 + 上游输入的背景(待按数量装配):<br>'
                '生产活动:<code>'+esc(via.get('via_activity',''))+'</code> '+esc(via.get('via_activity_name',''))+'<br>'
                '工序数据集:'+link+'<br>上游输入:'+esc(via.get('inputs',''))+'</p></div>')
    return ''

def parse_page(path):
    t=open(path).read()
    fmm=re.search(r'^---\n(.*?)\n---', t, re.S); fm=parse_fm(fmm.group(1)) if fmm else {}
    bodym=re.search(r'<!-- BODY:START -->(.*?)<!-- BODY:END -->', t, re.S)
    body=bodym.group(1).strip() if bodym else ''
    lcam=re.search(r'<!-- LCA_ASSOCIATION:START -->(.*?)<!-- LCA_ASSOCIATION:END -->', t, re.S)
    if not lcam:  # 兼容迁移前页面，便于旧行业分阶段升级
        lcam=re.search(r'<!-- LCA_BINDING:START -->(.*?)<!-- LCA_BINDING:END -->', t, re.S)
    lca_dataset=lcam.group(1).strip() if lcam else ''
    changem=re.search(r'<!-- CHANGELOG:START -->(.*?)<!-- CHANGELOG:END -->', t, re.S)
    change_log=changem.group(1).strip() if changem else ''
    mer=re.search(r'```mermaid\n(.*?)```', t, re.S); mermaid=mer.group(1).strip() if mer else ''
    def fmap(k):  # 解析 YAML flow 映射 {k: "v", ...}（键无引号，非合法JSON）
        s=fm.get(k,'').strip()
        if not (s.startswith('{') and s.endswith('}')): return {}
        inner=s[1:-1]; parts=[]; buf=''; q=False
        for ch in inner:
            if ch=='"': q=not q; buf+=ch
            elif ch==',' and not q: parts.append(buf); buf=''
            else: buf+=ch
        if buf.strip(): parts.append(buf)
        d={}
        for p in parts:
            if ':' in p:
                kk,vv=p.split(':',1); d[kk.strip()]=vv.strip().strip('"')
        return d
    def flist(k):  # 解析 YAML flow 列表 [A001, P002]
        s=fm.get(k,'').strip()
        if not (s.startswith('[') and s.endswith(']')): return []
        inner=s[1:-1].strip()
        return [x.strip().strip('"') for x in inner.split(',') if x.strip()] if inner else []
    declared_refs=flist("provenance_refs")
    inline_refs=re.findall(r'\[\^([a-z0-9\-]+)\](?!:)', body)
    evidence_refs=[]
    for kind in ('flows','emissions','indicators','quality','props','params'):
        evm=re.search(rf'<!-- EV:{kind}:START -->(.*?)<!-- EV:{kind}:END -->', t, re.S)
        if evm:
            evidence_refs.extend(re.findall(r'\bku-[a-z0-9\-]+\b', evm.group(1)))
    # 旧页的 frontmatter 可能尚未完成来源同步，但构建结果不能因此隐藏正文/
    # 证据表已经使用的原文与核验记录。wiki-v2 页再由 lint 强制两侧完全同步。
    prov_refs=list(dict.fromkeys(declared_refs+inline_refs+evidence_refs))
    return {
        "id": fm.get("id",""), "type": fm.get("node_type",""),
        "name": (fm.get("display_name","") or "").strip('"'),
        "boundary": fm.get("boundary",""), "confidence": fm.get("confidence",""),
        "schema_version": fm.get("schema_version",""),
        "body_status": fm.get("body_status",""),
        "structure_status": fm.get("structure_status",""),
        "provenance_status": fm.get("provenance_status",""),
        "claim_verification_status": fm.get("claim_verification_status",""),
        "quantity_status": fm.get("quantity_status",""),
        "evidence_status": fm.get("evidence_status",""),
        "dataset_readiness": fm.get("dataset_readiness",""),
        "change_log_status": fm.get("change_log_status",""),
        "reference_product": (fm.get("reference_product","") or "").strip('"'),
        "facets": fmap("facets"),
        "external": fmap("external"),
        "produces": flist("produces"),
        "consumes": flist("consumes"),
        "produced_by": flist("produced_by"),
        "consumed_by": flist("consumed_by"),
        "prov_refs": prov_refs,
        "prov_refs_declared": declared_refs,
        "prov_refs_derived": list(dict.fromkeys(inline_refs+evidence_refs)),
        "lca": fmap("lca"),
        "lca_via": fmap("lca_via"),
        "body_html": (assemble_body(body, t)
                      +(md_with_tables(lca_dataset) if lca_dataset else '')
                      +lca_block(fmap("lca"), fmap("lca_via"))),
        "change_log_html": md_to_html(change_log) if change_log else "",
        "mermaid": mermaid,
        "has_body": bool(body and "正文待 workflow 填肉" not in body),
    }

def cross_link_context(node, industry, product_registry, graph_cache):
    """将 background resolves_to 解析为母行业产品及其生产活动，供 Wiki 工艺图展示。
    这里只投影既有跨行业绑定，不创建或修改任何图边。
    """
    if not node or node.get("boundary")!="background":
        return None
    gpid=node.get("resolves_to") or (node.get("facets") or {}).get("resolves_to")
    status=node.get("home_status") or (node.get("facets") or {}).get("home_status")
    if status!="linked" or not gpid:
        return None
    reg=product_registry.get(gpid)
    if not reg:
        return None
    home=reg.get("home_industry")
    target_id=reg.get("home_node")
    if not home or not target_id or home==industry:
        return None
    if home not in graph_cache:
        path=f"docs/{home}-name-graph.json"
        graph_cache[home]=load_graph(path) if os.path.exists(path) else None
    target_graph=graph_cache.get(home)
    if not target_graph:
        return None
    d,P,A,_=target_graph
    target=P.get(target_id)
    if not target:
        return None
    producers=[]
    for e in d.get("edges",[]):
        if e.get("type")!="PRODUCES" or e.get("to")!=target_id:
            continue
        act=A.get(e.get("from"))
        if act:
            producers.append({
                "id":act["id"], "name":act["name"], "industry":home,
                "href":f"{home}-wiki.html?id={act['id']}"
            })
    return {
        "industry":home,
        "gpid":gpid,
        "verdict":node.get("cross_link_verdict") or "linked",
        "target":{
            "id":target["id"], "name":target["name"], "industry":home,
            "href":f"{home}-wiki.html?id={target['id']}"
        },
        "producers":producers
    }

def main(wikidir, outjs, varname, mode="production"):
    industry=os.path.basename(wikidir.rstrip('/'))
    if mode not in {"production", "preview"}:
        raise SystemExit(f"不支持的 bundle mode: {mode}")
    suffix="_WIKI_PREVIEW" if mode=="preview" else "_WIKI"
    expected_var=f"{industry.upper()}{suffix}"
    if varname != expected_var:
        raise SystemExit(
            f"数据变量名不一致: {industry} {mode} viewer 读取 window.{expected_var}，"
            f"不能生成 window.{varname}"
        )
    expected_name=(f"{industry}-wiki-preview-data.js" if mode=="preview"
                   else f"{industry}-wiki-data.js")
    if os.path.basename(outjs) != expected_name:
        raise SystemExit(
            f"{mode} bundle 必须写入 {expected_name}，不能写入 {os.path.basename(outjs)}"
        )
    reg_path=f"sources/{industry}/registry.json"
    registry={}
    if os.path.exists(reg_path):
        registry=json.load(open(reg_path)).get("sources",{})
    graph_path=next((c for c in [f"docs/{industry}-name-graph.json",
                                  os.path.join(os.path.dirname(os.path.dirname(wikidir.rstrip('/'))),'docs',f'{industry}-name-graph.json'),
                                  f"{industry}-name-graph.json"] if os.path.exists(c)), None)
    graph=load_graph(graph_path) if graph_path else None
    if graph: _,Pg,Ag,Pby=graph
    product_registry={}
    registry_path="registry/products.json"
    if os.path.exists(registry_path):
        product_registry=json.load(open(registry_path)).get("products",{})
    graph_cache={industry:graph}
    data={}
    for f in glob.glob(f"{wikidir}/products/*.md")+glob.glob(f"{wikidir}/activities/*.md"):
        p=parse_page(f)
        if not p["id"]: continue
        p["bundle_mode"]=mode
        p["body_marked_reviewed"]=(p.get("body_status")=="reviewed")
        # bundle builder 不读取 coverage/Go-No-Go，不能自行授予发布资格。
        # preview 更必须恒为 false，防止把旧 reviewed 标记误读成当前 run 已放行。
        p["publication_eligible"]=(False if mode=="preview" else None)
        nid=p["id"]
        if graph:
            g=Pg.get(nid) or Ag.get(nid)
            if g:
                p["sigil"]=activity_sigil(g,Pby) if nid in Ag else product_sigil(g)
                p["cross_link"]=cross_link_context(g,industry,product_registry,graph_cache)
                p["diagram_svg"]=neighborhood_svg(nid,graph,p["cross_link"])   # 本行业脊边 + 跨行业绑定投影
        p["source_refs"]=[]
        for sid in p.get("prov_refs",[]):
            s=registry.get(sid)
            if not s:
                p["source_refs"].append({
                    "id":sid, "title":"来源注册缺失", "type":"registry-gap",
                    "version":"", "authority":"", "region":"",
                    "status":"missing-registry", "locator":"", "url":"",
                    "excerpt_seeds":[], "verified_via":""
                })
                continue
            locator=str(s.get("locator") or "")
            url=source_url(s)
            if s.get("status")=="verified" and s.get("type")=="web" and not url:
                raise ValueError(f"已核验 Web 来源缺少可跳转原文 URL: {sid}")
            p["source_refs"].append({
                "id":sid,
                "title":s.get("title",""),
                "type":s.get("type",""),
                "version":s.get("version",""),
                "authority":s.get("authority",""),
                "region":s.get("region",""),
                "status":s.get("status",""),
                "locator":locator,
                "url":url,
                "excerpt_seeds":s.get("excerpt_seeds") or [],
                "verified_via":s.get("verified_via","")
            })
        diag=f'<h3>工艺图 · 邻域工序</h3><div class="diagram">{p["diagram_svg"]}</div>' if p.get("diagram_svg") else ''
        p["body_html"]=p["body_html"].replace('<!--DIAGRAM-->', diag)   # 工艺图就位于定性核心与数据表之间
        data[nid]=p
    js=f"window.{varname}=" + json.dumps(data, ensure_ascii=False) + ";\n"
    open(outjs,"w").write(js)
    print(f"bundle[{mode}]: {len(data)} 节点 -> {outjs} ({os.path.getsize(outjs)} bytes); graph={graph_path}")
    print("  has_body:", sum(1 for v in data.values() if v['has_body']),
          "| sigil+diagram:", sum(1 for v in data.values() if v.get('sigil')))

if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wikidir")
    ap.add_argument("outjs")
    ap.add_argument("varname")
    ap.add_argument("--mode", choices=("production", "preview"), default="production")
    args=ap.parse_args()
    main(args.wikidir, args.outjs, args.varname, args.mode)
