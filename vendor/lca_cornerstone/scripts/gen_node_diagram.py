#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""节点邻域工序图（《节点 Wiki 实施方案》§3.4）：从 edges 确定性派生的蓝图 ego-graph。
中心=节点大 sigil；邻居卡=干净类型icon + 家族色相条 + 可点击跳转(data-goto)；
边按角色着色(★参考金/◇共生/背景虚线)。图==边（模型不许改连线）。
兼容：活动中心(投入←活动→产出) 与 产品中心(产出它的活动←产品→消费它的活动)。
背景节点可附加跨行业母节点链：母行业产出活动 → 母行业产品 ⇢ 当前背景引用节点。
可被 build_wiki_bundle 直接 import：neighborhood_svg(node_id, graph_tuple, external_link=None)。
"""
import html
from gen_sigil import product_sigil, activity_sigil, type_icon_inner, hue_of, is_activity

def _inner(svg): return svg[svg.index('>')+1: svg.rindex('</svg>')]
def esc(s): return html.escape(str(s or ''), quote=False)
def _trunc(s,n=17): return s if len(s)<=n else s[:n]+'…'

W=1180; CARDW=270; CARDH=56; ROWH=70; INX=46; TOP=112; CAP=13
OUTX=W-46-CARDW; CX=W/2

def neighborhood_svg(nid, graph, external_link=None):
    d,P,A,Pby = graph
    node = P.get(nid) or A.get(nid)
    if not node: return ''
    act = is_activity(node)
    E=d['edges']
    if act:
        left=[(e['to'],None) for e in E if e['from']==nid and e['type']=='CONSUMES']
        right=[(e['to'],e.get('role')) for e in E if e['from']==nid and e['type']=='PRODUCES']
        Lhdr,Rhdr=f"投入 CONSUMES（{len(left)}）",f"产出 PRODUCES（{len(right)}）"
        get=lambda i:P.get(i)
    else:
        left=[(e['from'],e.get('role')) for e in E if e['to']==nid and e['type']=='PRODUCES']
        right=[(e['from'],None) for e in E if e['to']==nid and e['type']=='CONSUMES']
        Lhdr,Rhdr=f"产出它的活动（{len(left)}）",f"消费它的活动（{len(right)}）"
        get=lambda i:A.get(i)

    lo,lcut=left[:CAP],max(0,len(left)-CAP)
    ro,rcut=right[:CAP],max(0,len(right)-CAP)
    nmax=max(len(lo),len(ro),1); contentH=nmax*ROWH
    cy=TOP+contentH/2
    base_h=int(TOP+contentH+46)
    ext_producers=(external_link or {}).get('producers') or []
    ext_target=(external_link or {}).get('target')
    has_external=bool(ext_target)
    ext_rows=max(1,min(len(ext_producers),2)) if has_external else 0
    ext_top=base_h+54
    H=base_h+(ext_rows*ROWH+86 if has_external else 0)

    def tag_mark(n,role,side):
        bg=(n.get('boundary')=='background')
        if act:
            tag=('投入'+('·背景' if bg else '')) if side=='L' else ('参考产品' if role=='reference' else '共生品')
        else:
            tag=('产出'+('·参考' if role=='reference' else '·共生')) if side=='L' else '消费'
        mark='★' if role=='reference' else ('◇' if role=='coproduct' else '')
        return tag,mark,bg

    def card(x,y,n,role,side):
        if not n: return ''
        tag,mark,bg=tag_mark(n,role,side)
        hue=hue_of(n,Pby)
        edge='#ffcf6b' if role=='reference' else ('#5b6b85' if bg else '#27496e')
        dash=' stroke-dasharray="5 3"' if bg else ''
        op='.66' if bg else '1'
        ic=type_icon_inner(n,hue)
        title=f'<title>{esc(n["name"])}</title>'
        markel=(f'<text x="{x+CARDW-19}" y="{y+22}" fill="#ffcf6b" font-size="14">★</text>' if mark=='★'
                else (f'<text x="{x+CARDW-18}" y="{y+22}" fill="#7fd4ff" font-size="12">◇</text>' if mark=='◇' else ''))
        return (f'<g data-goto="{n["id"]}" class="nbcard" style="cursor:pointer" opacity="{op}">{title}'
          f'<rect x="{x}" y="{y}" width="{CARDW}" height="{CARDH}" rx="10" fill="#0e2138" stroke="{edge}" stroke-width="1.3"{dash}/>'
          f'<rect x="{x}" y="{y+8}" width="4" height="{CARDH-16}" rx="2" fill="{hue}"/>'
          f'<circle cx="{x+32}" cy="{y+CARDH/2}" r="16" fill="#0a1a2e" stroke="{hue}" stroke-width="1" opacity=".9"/>'
          f'<g transform="translate({x+16},{y+CARDH/2-16}) scale(0.32)">{ic}</g>'
          f'<text x="{x+58}" y="{y+24}" fill="#e6f3ff" font-size="13" font-weight="600">{esc(_trunc(n["name"]))}</text>'
          f'<text x="{x+58}" y="{y+41}" fill="#7892b0" font-size="10.5">{esc(n["id"])} · {esc(tag)}</text>{markel}</g>')

    def conn(x1,y1,x2,y2,role,bg):
        col='#ffcf6b' if role=='reference' else ('#3a567a' if bg else '#46698f')
        wd=2 if role=='reference' else 1.4
        dash=' stroke-dasharray="4 3"' if bg else ''
        mx=(x1+x2)/2
        return f'<path d="M{x1} {y1} C{mx} {y1} {mx} {y2} {x2} {y2}" fill="none" stroke="{col}" stroke-width="{wd}" opacity=".8"{dash} marker-end="url(#nbar)"/>'

    def external_card(x,y,n,kind):
        """跨行业卡使用 data-href，避免把外部行业 ID 当成本地 ID 跳转。"""
        if not n: return ''
        accent='#65e6b4' if kind=='product' else '#8bbcff'
        label='母行业产品' if kind=='product' else '母行业产出活动'
        href=esc(n.get('href',''))
        industry=esc(n.get('industry',''))
        title=f'<title>{esc(n.get("name"))} · {industry}</title>'
        return (f'<g data-href="{href}" data-external-industry="{industry}" class="nbcard nbexternal" '
          f'style="cursor:pointer" opacity="1">{title}'
          f'<rect x="{x}" y="{y}" width="{CARDW}" height="{CARDH}" rx="10" fill="#0a2024" '
          f'stroke="{accent}" stroke-width="1.4" stroke-dasharray="6 3"/>'
          f'<rect x="{x}" y="{y+8}" width="4" height="{CARDH-16}" rx="2" fill="{accent}"/>'
          f'<text x="{x+18}" y="{y+23}" fill="#e8fff8" font-size="13" font-weight="650">{esc(_trunc(n.get("name","")))}</text>'
          f'<text x="{x+18}" y="{y+41}" fill="{accent}" font-size="10.5">{industry}::{esc(n.get("id"))} · {label} ↗</text></g>')

    rows=[]; conns=[]
    l_y0=TOP+(nmax-len(lo))*ROWH/2; r_y0=TOP+(nmax-len(ro))*ROWH/2
    for i,(iid,role) in enumerate(lo):
        n=get(iid); y=l_y0+i*ROWH; rows.append(card(INX,y,n,role,'L'))
        conns.append(conn(INX+CARDW,y+CARDH/2,CX-76,cy, role, (n or {}).get('boundary')=='background'))
    for i,(iid,role) in enumerate(ro):
        n=get(iid); y=r_y0+i*ROWH; rows.append(card(OUTX,y,n,role,'R'))
        conns.append(conn(CX+76,cy,OUTX,y+CARDH/2, role, (n or {}).get('boundary')=='background'))

    chue=hue_of(node,Pby); csig=_inner(activity_sigil(node,Pby) if act else product_sigil(node))
    center=(f'<g><rect x="{CX-76}" y="{cy-84}" width="152" height="168" rx="16" fill="#0b2540" stroke="{chue}" stroke-width="2.2" filter="url(#nbglow)"/>'
      f'<g transform="translate({CX-47},{cy-72}) scale(0.94)">{csig}</g>'
      f'<text x="{CX}" y="{cy+58}" fill="#eaf6ff" font-size="13" font-weight="700" text-anchor="middle">{esc(_trunc(node["name"],15))}</text>'
      f'<text x="{CX}" y="{cy+76}" fill="#8fc2e6" font-size="10.5" text-anchor="middle">{esc(nid)} · {"活动" if act else "产品"}</text></g>'
      f'<text x="{INX}" y="{TOP-26}" fill="#7596b6" font-size="12.5" font-weight="600">▸ {esc(Lhdr)}</text>'
      f'<text x="{OUTX+CARDW}" y="{TOP-26}" fill="#7596b6" font-size="12.5" font-weight="600" text-anchor="end">{esc(Rhdr)} ◂</text>')
    more=''
    if lcut: more+=f'<text x="{INX+12}" y="{l_y0+len(lo)*ROWH+16}" fill="#5a7596" font-size="11">… 另 {lcut} 项（见名称图）</text>'
    if rcut: more+=f'<text x="{OUTX+CARDW-12}" y="{r_y0+len(ro)*ROWH+16}" fill="#5a7596" font-size="11" text-anchor="end">… 另 {rcut} 项</text>'

    external=''
    if has_external:
        target_x=CX-CARDW/2
        target_y=ext_top+(ext_rows-1)*ROWH/2
        producer_cards=[]; producer_conns=[]
        for i,n in enumerate(ext_producers[:2]):
            py=ext_top+i*ROWH
            producer_cards.append(external_card(INX,py,n,'activity'))
            producer_conns.append(
                f'<path d="M{INX+CARDW} {py+CARDH/2} C{INX+CARDW+56} {py+CARDH/2} '
                f'{target_x-56} {target_y+CARDH/2} {target_x} {target_y+CARDH/2}" fill="none" '
                f'stroke="#65e6b4" stroke-width="1.5" opacity=".84" marker-end="url(#extbar)"/>')
        if not ext_producers:
            producer_cards.append(
                f'<text x="{INX}" y="{target_y+34}" fill="#668a83" font-size="11">母行业产出活动待补</text>')
        relation=esc((external_link or {}).get('verdict') or 'linked')
        industry=esc((external_link or {}).get('industry') or ext_target.get('industry') or '')
        bridge_y=target_y
        external=(
          f'<rect x="30" y="{base_h+16}" width="{W-60}" height="{H-base_h-32}" rx="14" '
          f'fill="#071a1c" stroke="#1c544b" stroke-width="1" stroke-dasharray="7 5"/>'
          f'<text x="{INX}" y="{base_h+40}" fill="#65e6b4" font-size="12.5" font-weight="700">'
          f'↗ 跨行业来源 · {industry} · {relation}</text>'
          f'{"".join(producer_conns)}{external_card(target_x,target_y,ext_target,"product")}{"".join(producer_cards)}'
          f'<path d="M{target_x+CARDW/2} {target_y} C{target_x+CARDW/2} {target_y-28} '
          f'{CX} {cy+112} {CX} {cy+84}" fill="none" stroke="#65e6b4" stroke-width="1.7" '
          f'opacity=".9" stroke-dasharray="7 4" marker-end="url(#extbar)"/>'
          f'<text x="{target_x+CARDW/2}" y="{bridge_y-10}" fill="#65e6b4" font-size="10.5" '
          f'text-anchor="middle">resolves_to ⇢ 当前背景引用</text>')

    return (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">'
      f'<defs><marker id="nbar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M1 1 L9 5 L1 9" fill="none" stroke="#6a90b8" stroke-width="1.4"/></marker>'
      f'<marker id="extbar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M1 1 L9 5 L1 9" fill="none" stroke="#65e6b4" stroke-width="1.5"/></marker>'
      f'<filter id="nbglow" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="3.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
      f'<pattern id="nbgrid" width="36" height="36" patternUnits="userSpaceOnUse"><path d="M36 0 H0 V36" fill="none" stroke="#0e2138" stroke-width="1"/></pattern></defs>'
      f'<rect x="0" y="0" width="{W}" height="{H}" fill="url(#nbgrid)"/>{"".join(conns)}{center}{more}{"".join(rows)}{external}</svg>')

if __name__=="__main__":
    import sys
    from gen_sigil import load_graph
    g=load_graph(sys.argv[1]); nid=sys.argv[2]
    open(sys.argv[3] if len(sys.argv)>3 else f'/tmp/nb-{nid}.svg','w').write(neighborhood_svg(nid,g))
    print('wrote', nid)
