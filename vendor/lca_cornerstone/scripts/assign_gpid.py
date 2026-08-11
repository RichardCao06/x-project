#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨行业身份层（《跨行业关联规约》§1-4）。通用多行业版。
- 给各已建行业前景产品打 GPID = <母行业>::<规范键 slug>。
- 生成 registry/products.json。
- 解析背景输入:curated 优先 → 节点自带 home_industry + CPC/HS 候选匹配 → 占位兜底。
用法: python3 scripts/assign_gpid.py
"""
import json, os, sys, glob

# 自动扫描 docs/*-name-graph.json(新行业自动可用;不再硬编码遗漏 shipping/aviation/...)
INDUSTRIES = {os.path.basename(p).replace('-name-graph.json', ''): p
              for p in sorted(glob.glob('docs/*-name-graph.json'))}

# --gpid-only:只给前景节点写 gpid 字段,跳过 background cross-link(避免覆盖 apply_cross_link.py 的手工绑定)
GPID_ONLY = '--gpid-only' in sys.argv

# curated 精确解析:(消费行业,本地id) -> (母行业,母行业本地id)。优先级最高。
CURATED={
 ('steel','P099'):('power','P005'), ('steel','P098'):('steel','P001'), ('power','P009'):('power','P005'),
 ('auto','P077'):('power','P005'),   # 汽车 外购电力 → 电力 市场(高压)
 ('auto','P060'):('steel','P067'),   # 汽车 硅钢/电工钢 → 钢铁 电工钢冷轧卷(HS前缀误配的专家修正)
 ('auto','P063'):('aluminium','P006'),  # 汽车 外购铝锭 → 铝 原铝锭(HS粗配精修)
 ('auto','P062'):('steel','P113'),   # 汽车 钢紧固件/标准件 → 钢铁 紧固件/标准件(Tier0补漏,HS7318/CPC42944)
 ('auto','P070'):('battery','P023'),  # 汽车 锂电电芯/模组 → 电池 电芯,NMC811,方形(主流BEV电芯;HS8507.60)
 ('auto','P071'):('battery','P034'),  # 汽车 外购动力电池包 → 电池 电池包,NMC811,整包(HS8507.60)
 ('auto','P072'):('battery','P036'),  # 汽车 铅酸12V → 电池 铅酸电池,12V起动(HS8507.10)
 ('auto','P073'):('electronics','P048'),  # 汽车 控制器/逆变器 → 电子 功率电子总成,逆变器(HS8504,×8最重)
 ('auto','P047'):('electronics','P072'),  # 汽车 车灯 → 电子 汽车车灯电气总成(HS8512)
 ('battery','P059'):('aluminium','P047'), # 电池 铝箔集流体 → 铝 锻造8系箔(HS粗配精修:箔非billet)
 ('battery','P079'):('steel','P060'),  # 电池 钢壳 → 钢 冷轧卷(HS粗配精修:壳体用薄板,非solid_residue)
 ('battery','P058'):('power','P005'),  # 电池 外购电力 → 电力 市场(高压)(与汽车电力同口径)
 ('auto','P065'):('copper','P014'),  # 汽车 铜线/导体 → 铜 铜线丝,纯铜(线束绕组用,单字'铜'被长度过滤名匹配漏)
 ('battery','P061'):('plastics','P124'),  # 电池 PE/PP隔膜 → 塑料 PE薄膜(电池隔膜)(名'隔膜'⊄'电池隔膜'精确,定向锚正确形态)
 ('chemicals','P233'):('plastics','P077'),  # 化学 外购聚酯树脂(粉末涂料) → 塑料 不饱和聚酯,涂层树脂(避免误配 epoxy)
 ('plastics','P022'):('chemicals','P270'),  # 塑料 顺酐/苯酐 → 化学 顺酐(马来酸酐)(防'酸酐'子串误配固化剂)
 ('plastics','P044'):('chemicals','P221'),  # 塑料 甲基氯硅烷 → 化学 二甲基二氯硅烷(名差异致名匹配漏)
 ('auto','P067'):('glass','P021'),       # 汽车 汽车玻璃 → 玻璃 平板钠钙,安全加工(夹层)(挡风;'玻璃'停用后名匹配空)
 ('electronics','P112'):('glass','P018'),# 电子 显示玻璃基板 → 玻璃 显示铝硅,显示基板
 ('plastics','P036'):('glass','P014'),   # 塑料 玻璃纤维 → 玻璃 E增强玻纤,连续纤维纱(GF增强)
 ('steel','P107'):('aluminium','P006'), # 钢铁 外购铝 → 铝 原铝锭
 ('glass','P070'):('chemicals','P119'),  # 玻璃 硫酸钠/芒硝 → 化学 副产硫酸钠(防误配 electrolyte)
 ('auto','P068'):('rubber','P028'),      # 汽车 轮胎(外购) → 橡胶 天然橡胶,轮胎(NR为轮胎主体胶,代表节点)
 ('auto','P069'):('rubber','P048'),      # 汽车 密封/减振件(外购) → 橡胶 丁腈,密封减振件(NBR油封为汽车密封主体)
 ('copper','P077'):('nonferrous_metals','P001'),  # 铜 锌(合金元素) → 有色 锌精金属锭(单字'锌'被长度过滤)
 ('copper','P078'):('nonferrous_metals','P003'),  # 铜 锡 → 有色 锡精金属锭
 ('copper','P079'):('nonferrous_metals','P004'),  # 铜 镍 → 有色 镍精金属锭
 ('copper','P080'):('nonferrous_metals','P002'),  # 铜 铅 → 有色 铅精金属锭
 ('copper','P081'):('aluminium','P006'),          # 铜 铝(误标nonferrous) → 铝 原铝锭
 ('steel','P106'):('nonferrous_metals','P004'),   # 钢 镍 → 有色 镍精金属锭(修alloy→refined)
 ('steel','P111'):('nonferrous_metals','P001'),   # 钢 锌(镀锌) → 有色 锌精金属锭
 ('battery','P085'):('nonferrous_metals','P002'), # 电池 铅锭/铅合金锭 → 有色 铅精金属锭
 ('battery','P057'):('battery','P036'),  # 电池 退役铅酸电池再生进料 → 铅酸电池12V(闭环;防名匹配误配到 NMC811 锂电芯)
 ('textiles','P076'):('plastics','P155'),  # 纺织 涤纶短纤 → 塑料 PET纤维(中文'涤纶'≠latin'PET'名匹配空→落HS全配pa.fiber)
 ('textiles','P077'):('plastics','P155'),  # 涤纶长丝 → PET纤维
 ('textiles','P078'):('plastics','P156'),  # 涤纶工业丝 → PET工业丝
 ('textiles','P079'):('plastics','P157'),  # 锦纶工业丝 → PA工业丝
 ('textiles','P080'):('plastics','P158'),  # 锦纶短纤 → PA纤维
 ('textiles','P081'):('plastics','P158'),  # 锦纶长丝 → PA纤维
 ('textiles','P082'):('plastics','P159'),  # 丙纶短纤 → PP纤维
 ('textiles','P083'):('plastics','P159'),  # 丙纶长丝 → PP纤维
 ('textiles','P084'):('plastics','P162'),  # 腈纶短纤 → PAN纤维
 ('auto','P076'):('textiles','P074'),      # 汽车 座椅面料 → 纺织 涤纶汽车内饰纺织品
 ('rubber','P122'):('textiles','P054'),    # 橡胶 帘子布 → 纺织 涤纶产业用(帘子布,polyester帘子布主流)
 ('textiles','P086'):('agriculture','P002'), # 纺织 原棉 → 农林 皮棉(轧花初加工纤维,纺纱原料)
 ('plastics','P051'):('agriculture','P013'), # 塑料 天然纤维(麻) → 农林 麻纤维(天然纤维增强)
 # EPC 补的3总成新背景(信息娱乐/被动安全)→ 现成行业;烟火药剂/R1234yf冷媒 chemicals确无→诚实占位
 ('auto','P083'):('electronics','P073'),   # 中控显示屏总成 → 电子 车载显示总成
 ('auto','P084'):('electronics','P055'),   # 车载计算单元 → 电子 控制模组,自控装置模组
 ('auto','P085'):('textiles','P059'),      # 安全约束织物 → 纺织 涤纶产业用(安全带/安全气囊)
 ('auto','P087'):('electronics','P052'),   # 碰撞传感/气囊ECU → 电子 控制模组,ECU
}
# 专家裁定强制占位:母行业确无对应前景节点,子串名匹配会错配,故钉死占位
BLOCK_PLACEHOLDER={
 ('glass','P058'),  # 硝酸钾(化学强化用):chemicals 无硝酸钾节点 → 占位(防误配 electronic_grade_epoxy)
 ('glass','P059'),  # 粘结剂/上浆剂(玻纤上浆):chemicals 无玻纤上浆剂 → 占位(防误配 PVDF粘结剂)
 ('rubber','P089'), # 异丁烯:chemicals 无 → 占位(防子串误配 PIBSI分散剂)
 ('rubber','P091'), # 氯丁二烯:chemicals 无 → 占位(防'丁二烯'子串误配 butadiene)
 ('rubber','P094'), # 二烯单体ENB/DCPD:chemicals 无 → 占位(防误配 crosslinker)
 ('rubber','P096'), # 含氟单体VF2/HFP/TFE:chemicals 无 → 占位(防'烯'子串误配 ethylene)
 ('rubber','P099'), # 催化剂/引发剂(泛指):chemicals 无对应 → 占位(防误配 antioxidant)
 ('rubber','P101'), # 凝聚剂(氯化钠/硫酸,泛指):→ 占位(防误配 na_byproduct)
 ('copper','P082'), # 锰:nonferrous_metals 无锰(属ferroalloy) → 占位
 ('textiles','P085'), # 芳纶纤维:plastics 无芳纶(high_perf未含aramid纤维) → 占位
}
# 后备 home(节点无 home_industry 字段时);钢/电 P0 已写入字段,此处主要给历史兼容
HOME_FALLBACK={
 ('steel','P095'):'mining',('steel','P096'):'scrap_recycling',('steel','P097'):'coal_mining',
 ('steel','P100'):'natural_gas',('steel','P101'):'hydrogen',('steel','P102'):'lime_minerals',
 ('steel','P103'):'minerals',('steel','P104'):'ferroalloy',('steel','P105'):'ferroalloy',
 ('steel','P106'):'nonferrous_metals',('steel','P107'):'aluminium',('steel','P108'):'carbon_materials',
 ('steel','P109'):'minerals',('steel','P110'):'industrial_gases',('steel','P111'):'nonferrous_metals',('steel','P112'):'coal_mining',
}

def slug(facets, order):
    return '.'.join(str(facets[f]) for f in order if f in facets and facets[f] not in (None,''))
def hs4(x): return str(x or '')[:4]

def main():
    graphs={}; order={}
    for ind,path in INDUSTRIES.items():
        if not os.path.exists(path): continue
        graphs[ind]=json.load(open(path))
        order[ind]=[f['name'] for f in graphs[ind]['conventions']['product_facets']]

    # 1) 前景产品 gpid + registry
    gpid_of={}; registry={}
    for ind,g in graphs.items():
        for p in g['products']:
            if p.get('boundary')=='background': continue
            gp=f"{ind}::{slug(p.get('facets') or {}, order[ind])}"
            p['gpid']=gp; gpid_of[(ind,p['id'])]=gp
            registry[gp]={'home_industry':ind,'home_node':p['id'],'display_name':p['name'],
                'canonical_key':p.get('facets') or {},'cpc':p.get('cpc'),'hs':p.get('hs'),
                'status':'built','aliases':[p['name']]}
    producible={e['home_industry'] for e in registry.values()}  # 有前景产品的行业=可解析母行业

    # 解析候选匹配:① 名称特征词重叠(最准,化学品等靠 hs4 粗配会错配:碳酸锂≠对二甲苯) ② 回退 hs精确/4位前缀 或 cpc 相同
    import re
    GENERIC={'材料','外购','固态','液态','气态','溶液','标准','已配制','无机','有机','工业用电','高纯','电池级','电子级',
             'na','背景','部件','总成','中间体','单体','电池化学品','电子化学品','涂料胶粘剂','催化剂添加剂','功能油液',
             '无机酸碱','无机盐','工业气体','无机气体','醇与含氧物','芳烃','烯烃','byproduct','市场组合','精制',
             # 类别词:不能单独驱动匹配(否则 NCM前驱体 误配 LFP前驱体 / 环氧树脂 误配氟塑料涂层因共含'塑料')
             '前驱体','树脂','原料','物料','溶剂','添加剂','助剂','基料','体系','长尾','组件','器件','化学品',
             '塑料','聚合物','涂层树脂','封装料','玻璃','背景','被动元件','元件','器件'}
    def toks(s):
        out=set()
        for part in re.split(r'[,，()（）/、|:：]+', str(s or '')):
            part=part.strip()
            if part and part not in GENERIC and len(part)>=2: out.add(part)
        return out
    def name_match(home, node):
        nt=toks(node.get('name'))
        if not nt: return None
        best=None; bestscore=(0,0)  # (有精确词命中, 最长匹配词长度)——精确恒优于子串
        for gp,e in registry.items():
            if e['home_industry']!=home: continue
            names=[e['display_name']]+list(e.get('aliases',[]))
            et=set()
            for nm in names: et|=toks(nm)
            # 精确词相同(乙烯==乙烯) 恒优于 互为子串(乙烯⊂碳酸乙烯酯——这类跨族子串是错配源)
            exact=set(); sub=set()
            for t in nt:
                if t in et: exact.add(t)
                elif any(t in m and len(t)>=2 for m in names) or any(m2 in t and len(m2)>=2 for m2 in et): sub.add(t)
            if exact:   score=(1, max(len(t) for t in exact))
            elif sub:   score=(0, max(len(t) for t in sub))
            else:       continue
            if score>bestscore or (score==bestscore and best and gp<best): bestscore=score; best=gp
        return best if bestscore[1]>=2 else None
    def cpchs_match(home, node):
        cands=[gp for gp,e in registry.items() if e['home_industry']==home and (
            (node.get('hs') and e.get('hs') and (e['hs']==node['hs'] or hs4(e['hs'])==hs4(node['hs']))) or
            (node.get('cpc') and e.get('cpc') and e['cpc']==node['cpc']))]
        return sorted(cands)[0] if cands else None
    def match(home, node):
        nm=name_match(home, node)
        if nm: return nm
        # 有特征词却名匹配不上 ⇒ 母行业确实没这个产品 ⇒ 占位(不拿粗hs4造错链:碳酸锂≠对二甲苯)
        if toks(node.get('name')): return None
        return cpchs_match(home, node)

    # 2) 背景解析(--gpid-only 时跳过,只想给新行业写 gpid 字段、不动已有 cross-link)
    stats={ind:{'linked':0,'internal':0,'placeholder':0} for ind in graphs}; pending=set()
    if GPID_ONLY:
        print('[--gpid-only] 跳过背景 cross-link 解析(保留所有手工绑定)')
    for ind,g in (graphs.items() if not GPID_ONLY else []):
        for p in g['products']:
            if p.get('boundary')!='background': continue
            key=(ind,p['id'])
            if key in BLOCK_PLACEHOLDER:   # 专家裁定:母行业确无对应节点,强制占位(防子串误配)
                p['home_industry']=p.get('home_industry') or HOME_FALLBACK.get(key,'unknown')
                p['home_status']='placeholder'; p.pop('resolves_to',None)
                stats[ind]['placeholder']+=1; pending.add(p['home_industry']); continue
            if key in CURATED:
                tind,tid=CURATED[key]; gp=gpid_of.get((tind,tid))
                p['home_industry']=tind; p['resolves_to']=gp
                p['home_status']='internal' if tind==ind else 'linked'
                stats[ind]['internal' if tind==ind else 'linked']+=1
                if gp: registry[gp]['aliases']=sorted(set(registry[gp]['aliases']+[p['name']]))
                continue
            home=p.get('home_industry') or HOME_FALLBACK.get(key,'unknown')
            p['home_industry']=home
            gp=match(home,p) if home in producible else None
            if gp:
                p['resolves_to']=gp; p['home_status']='internal' if home==ind else 'linked'
                stats[ind]['internal' if home==ind else 'linked']+=1
                registry[gp]['aliases']=sorted(set(registry[gp]['aliases']+[p['name']]))
            else:
                p['home_status']='placeholder'; p.pop('resolves_to',None)
                stats[ind]['placeholder']+=1; pending.add(home)

    # 3) 落盘
    for ind,path in INDUSTRIES.items():
        if ind in graphs: json.dump(graphs[ind],open(path,'w'),ensure_ascii=False,indent=1)
    os.makedirs('registry',exist_ok=True)
    json.dump({'_meta':{'version':'1.1','industries_built':sorted(graphs),'pending_industries':sorted(pending),'count':len(registry)},
               'products':registry}, open('registry/products.json','w'),ensure_ascii=False,indent=1)
    print(f"registry: {len(registry)} GPID · 行业 {sorted(graphs)}")
    for ind in graphs:
        s=stats[ind]; print(f"  {ind}: 背景 {sum(s.values())} → 跨链 {s['linked']} · 内部 {s['internal']} · 占位 {s['placeholder']}")
    print(f"  待建母行业: {sorted(pending)}")

if __name__=='__main__': main()
