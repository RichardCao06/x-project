---
name: cross-link-binding
description: 给某个行业图的 background 节点做"LLM 提名 + 代码裁决"的跨行业 GPID 绑定(填 home_status='linked' + resolves_to=对方行业 GPID)。新建行业图(SOP 跑完 gate 11/11 后)的右半截收口工序——前景产品自身已闭合,但 background(钢板/铝合金/电缆/化学品/燃油...)默认全是裸 placeholder,无法跨链上溯到 steel/aluminium/oil_refining 等图。当用户想:让某行业的 product-system-model 能跨行业展开 / 修补 home_status=null 的 placeholder / 把新行业接入骨架跨链体系 / 看某行业的跨行业关联缺口 / 标准化新行业右半截收口流程时触发。关键词:跨行业关联、GPID 绑定、cross-link、resolves_to、home_status、骨架右半截、行业接入、跨链覆盖率、placeholder 收口。
---

# 跨行业 GPID 绑定(left leg 内部 cross-link)

把某行业图的 background 节点(钢板/铜电缆/燃油/化学品...)绑到对应 home_industry 的真实 GPID。等价于
`bom-skeleton-probe` 的"LLM 提名 + 代码裁决"模式 → workflow 沙箱无磁盘,所有 I/O 用 main-loop Bash 包夹。

**这是左腿(骨架)内部的事,不碰 LCA 库(右腿 link_lca.py)。**

## 为什么是这个形状(必读,否则会设计错)

- 新建行业图 SOP 跑完 gate.py(11/11)只完成**行业内部闭合**(A/B/C/D/E);
  跨行业关联(`resolves_to` / `home_status`)是一道**独立工序**,目前是 `assign_gpid.py` 的硬编码 CURATED 字典——
  老行业(auto 82%/plastics 81%)是人工补的,**所有新建行业都 0%**(2026-06-30 实测 25/41 行业 0%)。
- 后果:这些行业的 product-system-model 只能在自己行业内展开(深度 ~3),无法像 Tesla 那样 cradle-to-gate 跨 13 行业。
- 解法:把"LLM 提名 + 代码裁决"做成可批量管线,LLM 一次性给某行业全部 bg 节点提名候选,
  脚本对真图查 `gpid` 字段裁决并写回。LLM 的提名永远被查不被信(同 gate.py 哲学)。

## 配方(main-loop 按序驱动)

约定 `<slug>` = 要做 cross-link 的源行业(如 `shipping` / `aviation` / `ict_equipment`)。

### ① 入口物料化(Bash · 零 LLM)

```bash
python3 scripts/prep_cross_link.py <slug>
```

输出 `/tmp/<slug>-cross-link-input.json`:
- `pending`:剔除已 `home_status=linked` 的 bg 节点 + 每条带 prov_hint(从 provenance 解析的候选 GPID)
- `index`:各 target_industry 的精简前景索引(id+name+facets,LLM 在此范围内提名)
- `summary`:统计 by target / by prov_hit

### ② LLM 提名(Agent · sonnet)

派 1 个 `general-purpose` agent (model='sonnet')。Prompt 主干:

> 你是【跨行业 GPID 绑定 · 提名器】(只提名,不裁决)。读 `/tmp/<slug>-cross-link-input.json`,
> 给每个 pending 节点在其 target_industry 的 index 里提名最佳前景节点 id。
> 规则:prov_hint 非空优先用;否则按 name/cpc/hs 匹配同族节点;同族不符 → proxy;同族都没 → no_node。
> 输出写 `/tmp/<slug>-cross-link-nominations.json`,schema:
> ```
> {"slug": "<slug>", "nominations": [{src_id, target_id, verdict_hint(exact/proxy/no_node), matched_name, rationale}, ...]}
> ```

### ③ 代码裁决 + 写回(Bash · 零 LLM = GATE)

```bash
python3 scripts/apply_cross_link.py <slug> --dry-run    # 先看裁决统计
python3 scripts/apply_cross_link.py <slug>              # 写回 docs/<slug>-name-graph.json(自带 backup)
```

裁决规则:
- LLM `verdict_hint=no_node` → `Gap_no_node`(留 null,记缺口)
- LLM 提名 target_id 不在 target 图 → `Gap_bad_target_id`(算法/索引漂移)
- target 节点是 background → `Gap_target_is_background`(不能绑到 background)
- target 节点 `gpid` 字段为空 → `pending_target_gpid`(target 行业未跑 assign_gpid)
- gpid 有 → `home_status='linked'` + `resolves_to=gpid` + `cross_link_verdict=Exact/Coarse`

### ④ 验证 + 重建(Bash)

```bash
python3 scripts/cross_link_lint.py        # 跨链 gate(GPID 覆盖 / 单母行业 / 跨行业闭合)
python3 scripts/build_product_model.py <product_slug> --mode-a <slug>::<root_node_id>   # 看效果
```

## 前置依赖

**target 行业必须先有 gpid 字段**(否则 apply 会标 `pending_target_gpid`)。一次性给所有行业生成:

```bash
python3 scripts/assign_gpid.py --gpid-only    # 只写前景 gpid,不动 background cross-link
```

`--gpid-only` 是关键:它跳过 background 解析,完整保留 apply_cross_link.py 的手工绑定。

## 产物

```
docs/<slug>-name-graph.json                    源图就地更新 home_status + resolves_to(自带 .bak)
registry/<slug>_cross_link_gaps.json           缺口台账:LLM no_node 的节点 = target 行业真缺口,供后续建图收口
/tmp/<slug>-cross-link-input.json              prep 产出(可缓存重跑)
/tmp/<slug>-cross-link-nominations.json        LLM 提名(可冻结/审计)
```

## 铁律

- **LLM 只在阶段 ② 提名**;阶段 ① 与 ③ 是确定性脚本,LLM 的 claim **永远被查不被信**(同 gate.py)
- **不动 LCA 库**:这是左腿内部 cross-link,不是右腿绑定;`apply_cross_link.py` 不读任何 LCA dataset
- **apply 默认带 backup**(`docs/<slug>-name-graph.json.bak`),首次 dry-run 看裁决合理才写回
- **Gap 不强凑**:LLM 标 no_node 的留 null,写入缺口台账;target 行业建图层补节点后,重跑 prep+LLM+apply 即补全(零额外 LLM cost,因 prep_hint 会自动命中新加节点)

## 实测(shipping · 2026-06-30 试跑首例)

- **输入**:shipping 57 bg / 15 target 行业 / 1261 候选前景节点
- **LLM 提名**(sonnet,82K tokens,5 分钟):10 exact / 30 proxy / 17 no_node
- **代码裁决**:8 Exact + 21 Coarse = 29 linked(50%) / 11 pending / 17 Gap
- **跑 `assign_gpid.py --gpid-only` 补 5 个新 target 行业 gpid → 重跑 apply**:11 pending → linked,**累计 40 linked(70%)**
- **手工修 1 个 home_industry 错(P136 LNG 胶合板:chemicals → wood_materials)**:**41 linked(72%)**
- **效果**:远洋货轮模型从 **16 节点 / 跨 1 行业 / 深 3** → **104 节点 / 跨 6 行业 / 深 17**(6× 节点 / 6× 行业 / 5.7× 深)
- **缺口**:16 个 no_node 全是 target 行业图真缺口,主要在 machinery(13:船用主机/辅机/螺旋桨/储罐) + electrical_equipment(2:电缆/BESS) + nonferrous_metals(1:Invar) → 写入 `registry/shipping_cross_link_gaps.json` 等下一轮建图收口

## 何时触发

- 新建一个行业图、跑完 gate 11/11 后(SOP 的第七阶段)
- 跑 product-system-model 发现 placeholder 满地、跨行业图数 ≤ 2 时
- 全图体检发现某行业 home_status linked 覆盖率 < 50% 时
- 用户问"这个行业的 background 节点怎么挂不上钢/铝/化学品"时
