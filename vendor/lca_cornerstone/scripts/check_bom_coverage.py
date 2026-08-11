#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实产品 BOM 覆盖复核(优化1,《方法论复盘-EPC案例.md》§4)。

把"对外部分类无缺口"补上一条腿:"对一台真实参考产品的 BOM 子系统无缺口"。
内置一份**参考 EV 子系统清单**(由 Tesla Model 3 EPC 19 顶层系统提炼),对照 auto 名称图的
`subsystem` 刻面覆盖,报告:已覆盖 / 折叠可辩护 / 缺失。这是把本轮手工 EPC 对比固化成可重复的复核。

清单条目: (EPC系统, 映射到的 auto subsystem 值, 强度)
  强度 'node' = 应有独立子系统节点;'fold' = 当前分辨率折叠进某总成可辩护(底盘细分等)。
用法: python3 scripts/check_bom_coverage.py [docs/auto-name-graph.json]   (退出码恒 0,复核)
"""
import json, sys

# 参考 BEV 子系统清单(Tesla Model 3 EPC 提炼)
REF=[
 ('10 BODY',            'body',                  'node'),
 ('11 CLOSURE',         'body',                  'fold'),  # 门/盖机构折入车身
 ('12 EXTERIOR',        'exterior',              'node'),
 ('13 SEATS',           'interior',              'fold'),  # 座椅折入内饰(骨架/泡沫/面料已跨链)
 ('14 INSTRUMENT PANEL','interior',              'fold'),
 ('15 INTERIOR TRIM',   'interior',              'node'),
 ('16 HV BATTERY',      'battery',               'node'),
 ('17 ELECTRICAL',      'electrical_electronics','node'),
 ('18 THERMAL',         'thermal_management',    'node'),
 ('20 SAFETY',          'safety_restraint',      'node'),
 ('21 INFOTAINMENT',    'infotainment',          'node'),
 ('30 CHASSIS',         'chassis',               'node'),
 ('31 SUSPENSION',      'chassis',               'fold'),  # 悬架折入底盘
 ('32 STEERING',        'chassis',               'fold'),  # 转向折入底盘
 ('33 BRAKES',          'chassis',               'fold'),  # 制动折入底盘
 ('34 WHEELS&TIRES',    'chassis',               'fold'),  # 轮胎→rubber/轮毂→aluminium 材料级已链
 ('39/40 DRIVE UNITS',  'powertrain',            'node'),
 ('44 HV / 50 CHARGER', 'electrical_electronics','fold'),  # 充电硬件折入电气电子
 ('18b FLUIDS/COOLANT', 'fluids',                'fold'),
]

def main(path='docs/auto-name-graph.json'):
    d=json.load(open(path))
    present={(p.get('facets') or {}).get('subsystem') for p in d['products'] if p.get('boundary')!='background'}
    present.discard(None)
    covered=[]; folded=[]; missing=[]
    for epc,sub,strength in REF:
        if sub in present:
            (covered if strength=='node' else folded).append((epc,sub))
        else:
            missing.append((epc,sub,strength))
    n=len(REF)
    print(f"\n  真实 BOM 覆盖复核 · 参考=Tesla Model 3 EPC({n} 子系统) · 图={path.split('/')[-1]}")
    print(f"  {'='*58}")
    print(f"  ✅ 独立节点覆盖 {len(covered)}: "+', '.join(e for e,_ in covered))
    print(f"  ◐ 折叠(当前分辨率可辩护) {len(folded)}: "+', '.join(e for e,_ in folded))
    if missing:
        print(f"  ❌ 缺失 {len(missing)}(真实产品有、图中无对应 subsystem):")
        for e,s,st in missing: print(f"       {e} → 应建 subsystem={s} [{st}]")
    else:
        print(f"  ❌ 缺失 0 —— 参考 BOM 全部子系统都有对应(node 或可辩护 fold)")
    cov=(len(covered)+len(folded))/n
    print(f"  → 覆盖率 {cov:.0%}（独立 {len(covered)} + 折叠 {len(folded)} / {n}）· 缺失 {len(missing)}")
    return 0

if __name__=='__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv)>1 else 'docs/auto-name-graph.json'))
