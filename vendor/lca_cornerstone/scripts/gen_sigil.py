#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""产品/活动 sigil 生成器（《节点 Wiki 实施方案》§3.2/3.3）。
sigil = g(canonical_key)：确定性、键派生、逐枚唯一、零文生图、可 lint。
- 产品 = 圆角方形 + 成分族色相 + 材料/形态母题 + md5(键) 镜像 identicon。
- 活动 = 六边形 + 设备 glyph + 取自参考产物的色相 + 流向箭头 + identicon。
可被 build_wiki_bundle 直接 import（product_sigil / activity_sigil / sigil_for / sigil_hash），
也可独立运行写出 wiki/<industry>/sigil/{id}.svg。
用法: python3 scripts/gen_sigil.py docs/steel-name-graph.json wiki/steel/sigil
"""
import sys, os, json, hashlib

META={'cpc','hs','isic','bref','nace','route_family','ipcc_category'}

# ---------- 确定性键哈希 ----------
def key_str(node):
    # identity_scope: 背景节点名为键(它们刻面常相同),前景节点刻面为键。保证 sigil 全局唯一。
    if node.get('boundary')=='background':
        return 'bg::'+(node.get('name') or '').strip()
    f={k:v for k,v in (node.get('facets') or {}).items() if k not in META}
    return '|'.join(f'{k}={f[k]}' for k in sorted(f))
def hbytes(node): return list(hashlib.md5(key_str(node).encode()).digest())
def sigil_hash(node): return hashlib.md5(key_str(node).encode()).hexdigest()[:12]

# ---------- 色相 ----------
GRADE_HUE={'unalloyed':'#7fb4ff','low_alloy':'#5fe0c8','low_alloy_engineering':'#5fe0c8','high_alloy':'#8fe36b',
 'cr_stainless':'#5fd0ff','crni_stainless':'#79ecff','si_electrical':'#b79bff','high_carbon_tool':'#ffce6b'}
BASE_HUE={'process_gas':'#ffd56b','slag':'#cbb46e','dust_sludge':'#b08e79','coke_carbon':'#e08a44','hot_metal':'#ff7a4a',
 'iron_ore_agglomerate':'#cf9258','reduced_iron':'#9fb6cf','scale_byproduct':'#b89a84','flux_refractory_byproduct':'#cfd6e0',
 'na_energy_carrier':'#9fe3ff','cast_iron':'#c0855a','carbon_steel':'#7fb4ff','low_alloy_steel':'#5fe0c8','alloy_steel':'#8fe36b',
 'stainless_steel':'#79ecff','electrical_steel':'#b79bff','tool_high_speed_steel':'#ffce6b',
 'electricity':'#9fe3ff','heat_steam':'#ff9b6b','captured_co2':'#9fb0c4','syngas':'#ffd56b','hydrogen':'#9fe8d0','oxygen':'#bfe0ff'}
def prod_hue(p):
    f=p.get('facets') or {}; g=f.get('composition_grade')
    if g and g in GRADE_HUE: return GRADE_HUE[g]
    return BASE_HUE.get(f.get('base_material') or f.get('energy_carrier_or_output'),'#9fb0c4')

# ---------- 产品母题 ----------
def _prod_motif(p,c):
    f=p.get('facets') or {}; bm=f.get('base_material'); fs=f.get('form_state')
    if bm=='process_gas': return f'<path d="M30 58 q0 -14 20 -14 q20 0 20 14 M34 50 q0 -10 16 -10 q16 0 16 10 M38 43 q0 -7 12 -7 q12 0 12 7" fill="none" stroke="{c}" stroke-width="3" stroke-linecap="round"/>'
    if bm=='slag': return f'<circle cx="40" cy="54" r="11" fill="{c}" opacity=".85"/><circle cx="58" cy="50" r="9" fill="{c}" opacity=".7"/><circle cx="52" cy="62" r="7" fill="{c}" opacity=".6"/>'
    if bm in('dust_sludge','scale_byproduct'): return ''.join(f'<circle cx="{34+(i*7)%34}" cy="{38+(i*11)%30}" r="2.6" fill="{c}" opacity="{.5+.05*(i%6)}"/>' for i in range(14))
    if bm=='coke_carbon': return f'<path d="M30 60 L36 36 L52 42 L46 64 Z" fill="none" stroke="{c}" stroke-width="3"/><path d="M50 58 L56 38 L70 44 L64 62 Z" fill="{c}" opacity=".75"/>'
    if bm=='hot_metal' or fs=='molten': return f'<path d="M50 30 C64 50 64 64 50 70 C36 64 36 50 50 30 Z" fill="{c}" opacity=".9"/><ellipse cx="46" cy="52" rx="3" ry="6" fill="#0a1522" opacity=".4"/>'
    if bm=='iron_ore_agglomerate': return ''.join(f'<circle cx="{cx}" cy="{cy}" r="7" fill="none" stroke="{c}" stroke-width="2.6"/>' for cx,cy in [(40,44),(60,42),(50,60),(64,60)])
    if bm=='reduced_iron': return f'<path d="M50 34 l13 7 v15 l-13 7 l-13 -7 v-15 Z" fill="none" stroke="{c}" stroke-width="2.6"/>'
    if bm=='na_energy_carrier' or fs=='na': return f'<path d="M54 30 L38 54 L49 54 L46 70 L62 46 L51 46 Z" fill="{c}"/>'
    if fs in('hot_rolled_coil','cold_rolled_coil','coated_coil'): return f'<g fill="none" stroke="{c}" stroke-width="2.6"><circle cx="50" cy="50" r="18"/><circle cx="50" cy="50" r="12"/><circle cx="50" cy="50" r="6"/></g>'
    if fs=='slab': return f'<rect x="30" y="42" width="40" height="16" rx="2" fill="{c}" opacity=".85"/>'
    if fs in('billet','bloom','beam_blank'): return f'<rect x="36" y="36" width="28" height="28" rx="2" fill="none" stroke="{c}" stroke-width="3"/>'
    if fs=='hot_rolled_plate': return f'<rect x="28" y="44" width="44" height="11" rx="1.5" fill="{c}" opacity=".85"/>'
    if fs=='hot_rolled_long': return f'<g stroke="{c}" stroke-width="3.4" stroke-linecap="round"><line x1="40" y1="34" x2="40" y2="66"/><line x1="50" y1="34" x2="50" y2="66"/><line x1="60" y1="34" x2="60" y2="66"/></g>'
    if fs=='wire_tube_pipe': return f'<circle cx="50" cy="50" r="15" fill="none" stroke="{c}" stroke-width="6"/>'
    if fs=='ingot': return f'<path d="M38 64 L42 38 L58 38 L62 64 Z" fill="none" stroke="{c}" stroke-width="3"/>'
    if fs in('powder_granule','lump_pellet_sinter'): return ''.join(f'<circle cx="{38+(i%4)*8}" cy="{40+(i//4)*9}" r="3" fill="{c}" opacity=".8"/>' for i in range(12))
    return f'<rect x="36" y="36" width="28" height="28" rx="4" fill="none" stroke="{c}" stroke-width="3"/>'

# ---------- 活动设备 glyph ----------
GLY={
 'oven':'<rect x="-15" y="-11" width="30" height="24" rx="2" fill="none" stroke="{c}" stroke-width="2"/><line x1="-7" y1="-11" x2="-7" y2="13" stroke="{c}" stroke-width="1.3"/><line x1="0" y1="-11" x2="0" y2="13" stroke="{c}" stroke-width="1.3"/><line x1="7" y1="-11" x2="7" y2="13" stroke="{c}" stroke-width="1.3"/>',
 'strand':'<path d="M-16 8 L16 8 M-16 8 L-12 -6 L12 -6 L16 8" fill="none" stroke="{c}" stroke-width="2"/><line x1="-8" y1="-6" x2="-8" y2="8" stroke="{c}" stroke-width="1.2"/><line x1="0" y1="-6" x2="0" y2="8" stroke="{c}" stroke-width="1.2"/><line x1="8" y1="-6" x2="8" y2="8" stroke="{c}" stroke-width="1.2"/>',
 'kiln':'<ellipse cx="0" cy="0" rx="17" ry="9" fill="none" stroke="{c}" stroke-width="2" transform="rotate(-12)"/><line x1="-14" y1="-3" x2="14" y2="3" stroke="{c}" stroke-width="1.2"/>',
 'furnace':'<path d="M-12 16 L-12 4 Q-12 -2 -7 -5 L-8 -15 L8 -15 L7 -5 Q12 -2 12 4 L12 16 Z" fill="none" stroke="{c}" stroke-width="2"/><line x1="-12" y1="5" x2="12" y2="5" stroke="{c}" stroke-width="1"/><circle cx="0" cy="10" r="3" fill="{c}" opacity=".5"/>',
 'shaft':'<path d="M-9 16 L-9 -6 L-13 -6 L0 -16 L13 -6 L9 -6 L9 16 Z" fill="none" stroke="{c}" stroke-width="2"/>',
 'bof':'<path d="M-11 -10 Q-13 8 0 14 Q13 8 11 -10 Z" fill="none" stroke="{c}" stroke-width="2"/><ellipse cx="0" cy="-10" rx="11" ry="3" fill="none" stroke="{c}" stroke-width="1.6"/>',
 'eaf':'<path d="M-13 14 Q-13 3 0 3 Q13 3 13 14 Z" fill="none" stroke="{c}" stroke-width="2"/><path d="M-15 3 L15 3 L12 -2 L-12 -2 Z" fill="none" stroke="{c}" stroke-width="1.5"/><line x1="-5" y1="-2" x2="-5" y2="-17" stroke="{c}" stroke-width="2.2"/><line x1="0" y1="-2" x2="0" y2="-17" stroke="{c}" stroke-width="2.2"/><line x1="5" y1="-2" x2="5" y2="-17" stroke="{c}" stroke-width="2.2"/>',
 'ladle':'<path d="M-11 -8 L-11 8 Q-11 14 0 14 Q11 14 11 8 L11 -8 Z" fill="none" stroke="{c}" stroke-width="2"/><rect x="-13" y="-11" width="26" height="4" rx="1" fill="none" stroke="{c}" stroke-width="1.5"/>',
 'caster':'<path d="M-8 -15 L8 -15 L8 -4 Q8 14 -10 16" fill="none" stroke="{c}" stroke-width="2"/><rect x="-9" y="-16" width="18" height="5" fill="none" stroke="{c}" stroke-width="1.4"/><circle cx="-2" cy="2" r="3" fill="none" stroke="{c}" stroke-width="1.3"/>',
 'mill':'<circle cx="0" cy="-7" r="7" fill="none" stroke="{c}" stroke-width="2"/><circle cx="0" cy="8" r="7" fill="none" stroke="{c}" stroke-width="2"/><line x1="-16" y1="0.5" x2="16" y2="0.5" stroke="{c}" stroke-width="1.6"/>',
 'bath':'<path d="M-15 -4 L15 -4 L13 14 L-13 14 Z" fill="none" stroke="{c}" stroke-width="2"/><path d="M-15 -4 q15 -10 30 0" stroke="{c}" stroke-width="1.4" fill="none"/><line x1="-8" y1="-12" x2="-8" y2="-4" stroke="{c}" stroke-width="1.6"/><line x1="0" y1="-13" x2="0" y2="-4" stroke="{c}" stroke-width="1.6"/><line x1="8" y1="-12" x2="8" y2="-4" stroke="{c}" stroke-width="1.6"/>',
 'recycle':'<path d="M-10 -4 A11 11 0 0 1 8 -8 L8 -13 L15 -6 L8 1 L8 -3 A7 7 0 0 0 -5 0 Z" fill="{c}" opacity=".8"/><path d="M10 4 A11 11 0 0 1 -8 8 L-8 13 L-15 6 L-8 -1 L-8 3 A7 7 0 0 0 5 0 Z" fill="{c}" opacity=".55"/>',
 'gas':'<path d="M-9 14 q-3 -11 3 -15 q-2 9 4 6 q5 -2 3 -9 q9 7 4 18" fill="none" stroke="{c}" stroke-width="2"/>',
 'anneal':'<rect x="-14" y="-9" width="28" height="18" rx="3" fill="none" stroke="{c}" stroke-width="2"/><path d="M-7 4 q3 -8 0 -12 M0 4 q3 -8 0 -12 M7 4 q3 -8 0 -12" stroke="{c}" stroke-width="1.4" fill="none"/>',
}
ROUTE2G={'coke_oven':'oven','sinter_strand':'strand','pelletizing_grate_kiln':'kiln','blast_furnace':'furnace',
 'shaft_furnace_gas_dr':'shaft','shaft_furnace_h2_dr':'shaft','fluidized_bed_dr':'shaft','rotary_kiln_coal_dr':'kiln',
 'smelting_reduction_hisarna':'furnace','smelting_reduction_corex_finex':'furnace','dri_electric_melter':'eaf',
 'basic_oxygen_furnace':'bof','electric_arc_furnace':'eaf','induction_furnace':'eaf','ladle_furnace':'ladle',
 'rh_vacuum_degasser':'ladle','aod_vod_converter':'bof','continuous_caster':'caster','ingot_mould':'caster',
 'hot_strip_mill':'mill','plate_mill':'mill','long_product_mill':'mill','tandem_cold_mill':'mill',
 'hot_dip_galvanizing':'bath','electro_galvanizing':'bath','tinning_line':'bath','color_coating_line':'bath',
 'asu_air_separation':'gas','slag_granulation':'recycle','bag_filter_dedusting':'recycle'}
VERB2G={'coking':'oven','sintering':'strand','pelletizing':'kiln','ironmaking_smelting':'furnace','direct_reduction':'shaft',
 'smelting_reduction':'furnace','steelmaking':'bof','secondary_refining':'ladle','casting':'caster','hot_rolling':'mill',
 'cold_rolling':'mill','annealing_heat_treatment':'anneal','coating':'bath','finishing_forming':'mill',
 'gas_recovery_utility':'gas','byproduct_treatment':'recycle','power_generation':'furnace','market_mixing':'recycle'}
def _glyph_kind(a):
    f=a.get('facets') or {}
    return ROUTE2G.get(f.get('technology_route')) or VERB2G.get(f.get('transformation_verb'),'furnace')
def act_hue(a, prod_by_name):
    for o in a.get('outputs',[]):
        if o.get('role')=='reference' and o.get('product') in prod_by_name:
            return prod_hue(prod_by_name[o['product']])
    return GRADE_HUE.get((a.get('facets') or {}).get('grade_variant'),'#9fd0c0')

# ---------- identicon ----------
def _identicon(hb,c):
    cells=[]
    for r in range(5):
        for col in range(3):
            if hb[r*3+col] & 0x80:
                for cc in (col,4-col): cells.append(f'<rect x="{cc*20}" y="{r*20}" width="20" height="20" fill="{c}"/>')
    return f'<g opacity="0.12">{"".join(cells)}</g>'

# ---------- 渲染 ----------
def product_sigil(p, size=None):
    c=prod_hue(p); hb=hbytes(p); uid=p['id']
    sz=f' width="{size}" height="{size}"' if size else ''
    dots=''.join(f'<circle cx="{8+hb[i]%84}" cy="{8+hb[i+1]%84}" r="1.8" fill="{c}" opacity=".9"/>' for i in (10,12))
    return (f'<svg viewBox="0 0 100 100"{sz} xmlns="http://www.w3.org/2000/svg">'
      f'<defs><clipPath id="cp{uid}"><rect x="3" y="3" width="94" height="94" rx="16"/></clipPath></defs>'
      f'<rect x="3" y="3" width="94" height="94" rx="16" fill="#0c1d31" stroke="{c}" stroke-width="1.6"/>'
      f'<g clip-path="url(#cp{uid})">{_identicon(hb,c)}</g>{_prod_motif(p,c)}{dots}</svg>')

HEX="25,6 75,6 97,50 75,94 25,94 3,50"
def activity_sigil(a, prod_by_name, size=None):
    c=act_hue(a,prod_by_name); hb=hbytes(a); uid=a['id']
    sz=f' width="{size}" height="{size}"' if size else ''
    gly=GLY[_glyph_kind(a)].replace('{c}',c)
    dots=''.join(f'<circle cx="{10+hb[i]%80}" cy="{10+hb[i+1]%80}" r="1.7" fill="{c}" opacity=".9"/>' for i in (9,13))
    return (f'<svg viewBox="0 0 100 100"{sz} xmlns="http://www.w3.org/2000/svg">'
      f'<defs><clipPath id="h{uid}"><polygon points="{HEX}"/></clipPath>'
      f'<linearGradient id="f{uid}" x1="0" x2="1"><stop offset="0" stop-color="{c}" stop-opacity="0"/><stop offset="1" stop-color="{c}" stop-opacity=".16"/></linearGradient></defs>'
      f'<polygon points="{HEX}" fill="#0c1d31" stroke="{c}" stroke-width="1.8"/>'
      f'<g clip-path="url(#h{uid})">{_identicon(hb,c)}<rect x="0" y="40" width="100" height="20" fill="url(#f{uid})"/></g>'
      f'<g transform="translate(50,50) scale(1.45)">{gly}</g>'
      f'<path d="M16 86 L80 86" stroke="{c}" stroke-width="1.4" opacity=".5"/>{dots}</svg>')

def sigil_for(node, prod_by_name, size=None):
    return activity_sigil(node, prod_by_name, size) if node.get('node_type')=='activity' or 'transformation_verb' in (node.get('facets') or {}) else product_sigil(node, size)

def is_activity(node): return node.get('node_type')=='activity' or 'transformation_verb' in (node.get('facets') or {})

def type_icon_inner(node, c):
    """纯类型 icon（母题/设备 glyph，无 identicon 无框），0..100 视框，供工序图小尺寸用。"""
    if is_activity(node):
        return f'<g transform="translate(50,52) scale(1.7)">{GLY[_glyph_kind(node)].replace("{c}",c)}</g>'
    return _prod_motif(node, c)

def hue_of(node, prod_by_name=None):
    return act_hue(node, prod_by_name or {}) if is_activity(node) else prod_hue(node)

def load_graph(path):
    d=json.load(open(path)); P=d['products']; A=d['activities']
    return d, {p['id']:p for p in P}, {a['id']:a for a in A}, {p['name']:p for p in P}

def main(graph_path, outdir):
    d,P,A,Pby=load_graph(graph_path)
    os.makedirs(outdir, exist_ok=True)
    hashes={}
    for p in d['products']:
        open(os.path.join(outdir,f"{p['id']}.svg"),'w').write(product_sigil(p))
        hashes[p['id']]=sigil_hash(p)
    for a in d['activities']:
        open(os.path.join(outdir,f"{a['id']}.svg"),'w').write(activity_sigil(a,Pby))
        hashes[a['id']]=sigil_hash(a)
    uniq=len(set(hashes.values()))==len(hashes)
    print(f"sigil: {len(hashes)} 枚 -> {outdir}  唯一={uniq}")
    if not uniq: print("  ❌ 存在重复 sigil-hash"); sys.exit(1)

if __name__=="__main__":
    main(sys.argv[1], sys.argv[2])
