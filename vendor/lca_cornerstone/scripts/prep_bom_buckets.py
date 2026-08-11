#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prep_bom_buckets.py — 通用 BOM 入口物料化(无 LLM,产品无关)。

任何产品(整车/显卡/…)的 BOM 都走这一条:解析 → 聚合成桶 → 推 candidate_inds(通用关键词材料→行业,
只为选索引切片,真匹配是 LLM 的事)→ 切母图精简索引 → 注入 canonical workflow 生成自包含 run-script。

两种输入:
  · A2MAC1 整车拆解 xlsx(固定列口径)
  · 规范化 BOM JSON: {"product":..,"host":"electronics","components":[{"name","material","mass_kg","subsystem"}]}

用法: python3 scripts/prep_bom_buckets.py <BOM.xlsx|BOM.json> [slug] [--host auto] [--min-kg 0.5]
"""
import json, os, sys, re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from graph_index import build_index

BOM_DEFAULT = "/Users/shujudagongren/Downloads/BillOfMaterialExcelReport (1).xlsx"
CANONICAL_WF = os.path.join(os.path.dirname(HERE), ".claude/workflows/bom-skeleton-probe.js")

# 通用关键词【材料/部件名 → 候选母行业】(产品无关;只为选索引切片,真匹配由 LLM 做)。
# 顺序扫,命中即收(可多命中);全 miss 落 DEFAULT。
KW2IND = [
    (r'\bsteel\b|钢|hsla|铸铁|cast[_ ]?iron|碳钢|不锈钢|stainless', ['steel']),
    (r'\balloy\b|铝\b|alumin|6xxx|7xxx|a356|adc12|gigacast', ['aluminium']),
    (r'magnesium|\b镁\b|az91|am60', ['magnesium']),
    (r'copper|铜|漆包|brass|bronze|黄铜', ['copper']),
    (r'titanium|钛|ti-6al|nitinol|niobium|铌', ['nonferrous_metals']),
    (r'solder|\bsac\b|焊膏|焊球|braze|锡膏|bga', ['nonferrous_metals']),
    (r'magnet|ndfeb|钕铁硼|永磁|稀土|samarium', ['nonferrous_metals']),
    (r'glass|玻璃|windshield|windscreen|窗\b', ['glass']),
    (r'rubber|epdm|elastomer|橡胶|轮胎|\btyre\b|\btire\b|\btpv\b|\btpe\b|\bnbr\b|\bsbr\b|seal\b|密封', ['rubber']),
    (r'\bpp\b|\bpc\b|\babs\b|\bpa\d|\bpet\b|\bpbt\b|\bpur\b|\bpu\b|\bpom\b|\bpvc\b|plastic|塑料|树脂|resin|polymer|nylon|聚', ['plastics']),
    (r'fabric|carpet|textile|fiber|fibre|织物|地毯|面料|纤维|皮革|leather|\bfoam\b|泡沫|insulation|隔音|气囊', ['textiles']),
    (r'fluid|\boil\b|coolant|refriger|lubric|grease|\b油\b|冷却液|制冷剂|brake fluid|刹车|润滑', ['chemicals']),
    (r'coat|paint|adhesive|glue|\bink\b|\btim\b|underfill|flux|sealant|epoxy|涂|胶\b|油墨|助焊|底填|硅脂', ['chemicals']),
    # 电子 / 半导体
    (r'\bdie\b|\bic\b|semiconductor|wafer|晶粒|芯片|\bchip\b|\bgpu\b|\bcpu\b|asic|logic|analog|mosfet|drmos|transistor|bios|gb2\d\d', ['electronics']),
    (r'dram|gddr|sram|nand|\bflash\b|memory|存储|显存|闪存|\bddr\b', ['electronics']),
    (r'\bpcb\b|fr4|印制|基板|载板|substrate|\bpcba\b|fcbga', ['electronics']),
    (r'capacitor|mlcc|inductor|resistor|电容|电感|电阻|passive|被动元件|ferrite|磁芯', ['electronics']),
    (r'connector|连接器|金手指|socket|端子|pcie|hdmi|displayport|\bdp\b', ['electronics', 'copper']),
    (r'electronic|电子|\becu\b|sensor|传感|module|控制|display|\bvrm\b|wiring|harness|线束|cable|电缆|pwm', ['electronics', 'copper']),
    (r'battery|\bcell\b|电池|电芯|模组|\bpack\b|\bbms\b', ['battery']),
]
DEFAULT_CANDS = ['steel', 'aluminium', 'plastics', 'electronics']


def inds_for(material, name=''):
    s = (str(material) + ' ' + str(name)).lower()
    out = []
    for pat, inds in KW2IND:
        if re.search(pat, s):
            for i in inds:
                if i not in out:
                    out.append(i)
    return out or list(DEFAULT_CANDS)


BATTERY_CLASSES = {'cell', 'module', 'pack', 'electrode', 'cathode_active_material'}  # battery 只切装配相关类

# "Electronic components"/"Other"(A2MAC1 整车)是跨域大杂烩 → 按路径关键词细拆,免得电池揉进电控桶。
ELEC_COMPOSITE = {'Electronic components', 'Other'}
def refine_sub(mat, path):
    if mat not in ELEC_COMPOSITE:
        return ''
    pl = path.lower()
    if 'battery' in pl:
        return 'battery'
    if any(k in pl for k in ('rotor', 'stator', 'motor', 'alternator')):
        return 'emotor'
    if any(k in pl for k in ('inverter', 'converter', 'charger', 'dc-dc', 'dc dc', 'obc', 'rectifier')):
        return 'powerelec'
    if any(k in pl for k in ('harness', 'cable', 'wire', 'wiring', 'busbar', 'bus bar')):
        return 'harness'
    if any(k in pl for k in ('pump', 'valve', 'compressor', 'radiator', 'cooler', 'thermal', 'climat', 'hvac', 'heater')):
        return 'thermal'
    if any(k in pl for k in ('ecu', 'sensor', 'control', 'module', 'display', 'screen', 'computer',
                             'camera', 'radar', 'antenna', 'speaker', 'horn', 'light', 'lamp')):
        return 'ecu'
    return 'misc_elec'

REFINE2INDS = {
    'battery': ['battery'], 'emotor': ['electronics', 'nonferrous_metals', 'steel', 'copper'],
    'powerelec': ['electronics', 'copper'], 'harness': ['copper', 'electronics'],
    'thermal': ['steel', 'aluminium', 'chemicals', 'rubber', 'plastics'], 'ecu': ['electronics', 'copper'],
    'misc_elec': ['electronics', 'copper', 'nonferrous_metals'],
}


def read_parts(path):
    """A2MAC1 整车拆解 xlsx → 零件行。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb['Bill of material']
    out = []
    for r in ws.iter_rows(min_row=8, max_row=ws.max_row, values_only=True):
        if all(v is None for v in r):
            continue
        g = lambda i: r[i] if i < len(r) else None
        try:
            w = float(g(25) or 0)
        except (TypeError, ValueError):
            w = 0.0
        out.append(dict(sys=(g(15) or '').strip(),
                        path=' / '.join(str(g(c)) for c in range(16, 24) if g(c)),
                        mat=(g(35) or '').strip(), mg=(g(34) or '').strip(), w=w))
    wb.close()
    return out, {}


def read_bom_json(path):
    """规范化 BOM JSON → 零件行 + meta(product/host)。任何产品都用这个口径。"""
    d = json.load(open(path))
    out = []
    for c in (d.get('components') or d.get('parts') or []):
        out.append(dict(sys=(c.get('subsystem') or c.get('system') or '(none)'),
                        path=str(c.get('name') or c.get('part') or ''),
                        mat=str(c.get('material') or c.get('mat') or '').strip(),
                        mg=str(c.get('material_group') or c.get('material') or '').strip(),
                        w=float(c.get('mass_kg') or c.get('mass') or 0)))
    return out, {'product': d.get('product'), 'host': d.get('host')}


def slice_index(ind):
    p = f"docs/{ind}-name-graph.json"
    if not os.path.exists(p):
        return ''
    g = json.load(open(p))
    idx, pf, af = build_index(g)
    cv = idx['identity']['controlled_values']
    lines = [f"## {ind} · 产品身份刻面键: {', '.join(pf)}"]
    for k in pf[:3]:
        vs = cv.get(k)
        if vs:
            short = [str(x).split('(')[0].split('（')[0].strip() for x in vs]
            lines.append(f"   {k} ∈ {{{', '.join(short[:28])}}}")
    rows = idx['products']
    if ind == 'battery':
        rows = [p for p in rows if (p['facets'].get('component_class') in BATTERY_CLASSES)]
    for p in rows:
        fv = '|'.join(str(p['facets'].get(k, '') or '') for k in pf)
        b = p.get('boundary', '')
        tag = f"  [{b}]" if b and b != 'foreground' else ''
        lines.append(f"{p['id']}  {p['name']}  {{{fv}}}{tag}")
    return '\n'.join(lines)


def main():
    raw = sys.argv[1:]
    host_cli, min_kg, pos = 'auto', 0.5, []
    i = 0
    while i < len(raw):
        if raw[i] == '--host' and i + 1 < len(raw):
            host_cli = raw[i + 1]; i += 2
        elif raw[i] == '--min-kg' and i + 1 < len(raw):
            min_kg = float(raw[i + 1]); i += 2
        else:
            pos.append(raw[i]); i += 1
    bom = pos[0] if pos else BOM_DEFAULT
    slug = pos[1] if len(pos) > 1 else 'tesla-model-x'

    parts, meta = read_bom_json(bom) if bom.endswith('.json') else read_parts(bom)
    host = meta.get('host') or host_cli
    TOTAL = sum(p['w'] for p in parts)

    agg = defaultdict(lambda: {'w': 0.0, 'n': 0, 'names': set()})
    for p in parts:
        sub = refine_sub(p['mat'], p['path'])
        a = agg[(p['sys'], p['mat'], p['mg'], sub)]
        a['w'] += p['w']
        a['n'] += 1
        if p['path']:
            segs = [x for x in p['path'].split(' / ') if x and x != 'None']
            a['names'].add(' · '.join(segs[-2:]) if len(segs) >= 2 else (segs[-1] if segs else p['path']))

    buckets = []
    bid = 0
    for (s, mat, mg, sub), a in sorted(agg.items(), key=lambda x: -x[1]['w']):
        if TOTAL > 0 and a['w'] < min_kg:  # 有质量才用阈值并长尾;无质量(结构件)全留
            continue
        bid += 1
        names = sorted(a['names'])
        cands = REFINE2INDS.get(sub) or inds_for(mat, ' '.join(names[:3]))
        label = f"{mat}/{sub}" if sub else mat
        buckets.append(dict(bucket_id=f"B{bid:03d}", system=s, material=label, matgroup=mg,
                            mass_kg=round(a['w'], 2), n_parts=a['n'],
                            sample_names=names[:6], part_names=names[:60], candidate_inds=cands))
    kept = sum(b['mass_kg'] for b in buckets)
    tail = round(TOTAL - kept, 2)

    used = sorted(set(i for b in buckets for i in b['candidate_inds']))
    INDEX = {i: slice_index(i) for i in used}

    os.makedirs('/tmp', exist_ok=True)
    json.dump(dict(vehicle=slug, product=meta.get('product') or slug, host=host,
                   total_kg=round(TOTAL, 2), kept_kg=round(kept, 2), tail_kg=tail, buckets=buckets),
              open(f"/tmp/{slug}-bom-buckets.json", "w"), ensure_ascii=False, indent=1)

    wf = open(CANONICAL_WF).read()
    A, B = '// <<<DATA-BINDING-START>>>', '// <<<DATA-BINDING-END>>>'
    pre, rest = wf.split(A)
    _, post = rest.split(B)
    embed = (f"// (prep 注入内联数据 —— 沙箱无磁盘,数据经本段进 workflow)\n"
             f"const VEHICLE = {json.dumps(slug)}\n"
             f"const BUCKETS = {json.dumps(buckets, ensure_ascii=False)}\n"
             f"const INDEX = {json.dumps(INDEX, ensure_ascii=False)}\n"
             f"const M = {{ reason: 'opus', recall: 'sonnet' }}\n")
    runjs = pre + embed + post
    runpath = f"/tmp/{slug}-bom-probe.run.js"
    open(runpath, "w").write(runjs)

    print(f"{meta.get('product') or slug}: {TOTAL:.2f} kg · {len(parts)} 件 → {len(buckets)} 桶(覆盖 {kept:.2f}, 长尾 {tail:.2f}) · host={host}")
    print(f"用到母图: {', '.join(used)}")
    print(f"→ /tmp/{slug}-bom-buckets.json + {runpath}")


if __name__ == '__main__':
    main()
