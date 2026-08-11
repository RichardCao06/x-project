---
name: product-system-model
description: 把具名车型或一张真实 BOM 清单建模成"无数值产品系统树"——映射到骨架节点、沿骨架拓扑递归上溯到地基,产出不挂 LCA 数据集、不算 GWP 的产品系统模型。当用户想:对某车型(如小米 su7)做结构化 LCA 建模、把 BOM 表展开成产品系统树、看一个产品的跨行业上游链/系统边界、或在量化前先建非数值 LCA 模型时触发。关键词:产品系统模型、系统树、无数值 LCA 建模、整车建模、BOM 展开、上游链、su7 建模。
---

# 产品系统模型(无数值)

现实产品 / BOM → 骨架节点 → 一棵产品系统树(产品 → … → 地基,全是骨架身份)。
**本版只做结构:不挂 LCA 数据集、不算 GWP**(那是后续 bom-lca-model 层)。设计全文见 `docs/product-system-model-design.html`。

**核心:BOM 给"叶"(成品侧零件),骨架拓扑给"上游深度"。两者拼起来 = 完整产品系统模型。**
没骨架你只有平铺零件表;有骨架,它把每片叶沿 `act_inputs`/`resolves_to` 上溯到地基。

## 两入口

一行命令:
```
Mode A:  /product-system-model 小米su7
Mode B:  /product-system-model --bom "/path/BOM.xlsx" --name su7
```

**Mode A · 具名车型**(无 BOM,结构原型;LLM 只分类不编质量)
1. **[分类]** 出规格(vehicle_class/powertrain/body_material/battery_chemistry/root_hint)。两种调法:
   - 最简:主会话直接派 1 个 classify subagent(单 agent 无需编排);**或**
   - 走 `product-system-model` workflow —— ⚠ **本 harness 实测 Workflow 的 `args` 形参不注入 workflow 全局 `args`**(name / scriptPath 都不行,CFG 收到空),故须把车型**内联**进 run-script 再 `scriptPath` 调(同 bom-skeleton-probe 的 embed 模式)。
2. **[Bash]** `python3 scripts/build_product_model.py <slug> --mode-a [specs.root_hint]` → 从骨架 auto 原型根递归展开(整车→总成→部件→材料→地基,**剥 LCA**)→ 无数值系统树。
   (实测 SU7 → root_hint = auto P002『整车,纯电(BEV)』,默认即此根。**统一引擎:Mode A/B 同一脚本、同形、同渲染器**——Mode B 子系统来自真 BOM 的 System,Mode A 来自骨架总成。变体驱动展开为后续增强。)

**Mode B · 真 BOM**(精确,质量加权;复用已验证探针)
1. **[Bash]** `python3 scripts/prep_bom_buckets.py "<BOM>" <slug>` → 组件桶 + 自包含 run-script。
2. **[Workflow]** `bom-skeleton-probe`(scriptPath = `/tmp/<slug>-bom-probe.run.js`)→ 组件→节点提名。
3. **[Bash]** 从 task 输出取 `.result.matches` → `docs/<slug>-bom-matches.json`(冻结锚)。
4. **[Bash]** `python3 scripts/grade_bom_matches.py docs/<slug>-bom-matches.json <slug>` → `docs/<slug>-probe-graded.json`。
5. **[Bash]** `python3 scripts/build_product_model.py <slug>` → 系统树 HTML + JSON。

## 产物
```
docs/<slug>-system-model.html   交互式系统树(组件→骨架→地基;徽章=Exact/Coarse/Gap;⛰=地基/背景叶;⇒=跨行业;↺=去重折叠)
docs/<slug>-system-model.json   模型(tree + grade_mass + stats)
```

## 铁律
- **LLM 只在两处**:入口分类(Mode A)、映射(Mode B,= 探针);**展开/组装/报告全是脚本**。
- 展开**停在身份层** —— 不挂数据集、不算数值(本版范围)。
- **Gap = 骨架无对应节点 = 模型断点**;补节点(过 `validate_graph` 11/11)后**只重跑 `build_product_model.py`(零 LLM)**即补全。
- **沙箱无磁盘** → prep/grade/build 都是主会话 Bash,夹在 workflow 两头(同 bom-skeleton-probe)。

## 实测
**Mode B · Tesla Model X**(零 LLM 用冻结 matches):139 组件 → **545 个去重骨架节点 · 跨 13 行业图 · 树深 19 · 3 个模型断点**(小型电机/EPS/空悬压缩机)。NCA 整包→电芯+BMS+热管理+壳体;6xxx 板→熔体→原铝→…;`battery⇒aluminium/copper/chemicals` 真跨图上溯。
**Mode A · 小米SU7**(分类):D-seg BEV · NMC(Max)/LFP(Std) · 101kWh · 2205kg · 钢铝混合(CTB+gigacasting) · root=auto P002;agent 守住"只分类不编质量"铁律。
