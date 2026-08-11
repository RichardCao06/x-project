---
id: P053
node_type: product
display_name: "PSU电源模组, 外购成品"
boundary: background
facets: {equipment_class: "psu_module", integration_level: "component", compute_type: "power_conditioning", form_factor: "na"}
produced_by: []
consumed_by: []
external: {cpc: "4641", hs: "8504.40"}
confidence: longtail
provenance_refs: [ecoinvent]
spine_hash: "b278fba97534"
schema_version: wiki-v1
sigil: "sigil/P053.svg"
body_status: draft
claim_verification_status: partial
---
<!-- BODY:START -->
## 定义与产品身份

PSU 电源模组是为 ICT 设备提供电能变换、调节和分配功能的外购成品部件。 〔未核实·模型回忆〕

## 性质与形态

该模组通常作为组件交付，包含电能变换与调节所需的电力电子、电路板、连接器和外壳或安装结构。 〔未核实·模型回忆〕

## 参考流与交接边界

模型参考流宜定义为一件满足声明输入、输出和额定功率规格的外购 PSU 电源模组，交接边界为供应商成品模组交付给设备装配方时。 〔未核实·模型回忆〕 [^internal-review]

## 规格与相邻节点区分

本节点按“psu_module、component、power_conditioning、na”刻面识别，应与整机电源系统、裸露电源电子元件和电池模组区分。 〔未核实·模型回忆〕 [^internal-review]

## 在系统中的角色

该节点是 ICT 设备装配所采购的背景组件，并已链接至电子行业的电力电子装配模组节点。 〔未核实·模型回忆〕 [^internal-review]

## 分类与适用范围

该节点适用于作为 ICT 设备子组件采购的电源调节模组，不覆盖面向最终用户独立销售的完整电子设备。 〔未核实·模型回忆〕 [^internal-review]

## 节点特定采集字段

采集时应记录额定功率、输入输出电压范围、转换效率、质量、主要材料与部件、制造商规格以及装配地点。 〔未核实·模型回忆〕 [^internal-review]

## 区域化补充要求

区域化应记录模组制造地、主要电力电子部件供应地及供应商至设备装配地的运输链。 〔未核实·模型回忆〕 [^internal-review]

## 数据适用状态与缺口

档案列有 ecoinvent 作为候选来源且无已核验来源；额定规格、供应商工艺、材料组成和区域供应链仍为关键数据缺口。 〔未核实·模型回忆〕 [^internal-review]

## 出处

[^internal-review]: 内部评审与建模约定——仅支持显式标注的系统边界、参考流、采集字段与数据缺口判断，不作为外部事实证据。
<!-- BODY:END -->

## 邻域工序图
> 由 graph edges 确定性派生（图==边，可被 lint 比对）；模型不得增删连线。

```mermaid
flowchart LR
  P053["PSU电源模组, 外购成品"]
```

## 🔒 数量（待挂 · NOT POPULATED）
> 本节为占位。输入流 / 输出流 / 参考单位的结构在此预留，数值由实测/权威库挂入，**LLM 不得填写**（数量防火墙）。

| 流 | 方向 | 单位 | 值 | 源 |
|---|---|---|---|---|
| (待挂) | — | — | — | — |

<!-- LCA_ASSOCIATION:START -->
## 🔗 可引用 LCA 数据集与关联

> 已核验关联 3 条：C0=0、C1=3、C2=0、C3=0、C4=0。这些记录用于发现与代理筛选；进入计算前仍须在具体项目中核对边界、许可和版本。

| 强度 | 数据库记录 | 数据库/版本 | 关联对象 | 潜在用途 | 主要限制 | 模型状态 |
|---|---|---|---|---|---|---|
| `C1` · 弱关联 | [スイッチング電源, GLO](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `reference_product_and_producer_process` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：product_family、application、boundary、geography。IDEA 开关电源为宽产品族记录，公开样本没有服务器功率等级、冗余和效率边界。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C1` · 弱关联 | [スイッチング電源, JPN](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `reference_product_and_producer_process` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：product_family、application、boundary、geography。IDEA 开关电源为宽产品族记录，公开样本没有服务器功率等级、冗余和效率边界。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C1` · 弱关联 | [power supply unit production, for desktop computer](https://ecoquery.ecoinvent.org/3.12/cutoff/dataset/6055/documentation) | ecoinvent 3.12 | `reference_product_and_producer_process` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：reference_product、application。中国台式机电源单元与服务器外购 PSU 同属成品电源，但功率密度、冗余、效率等级和结构不同。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |

> 关联不等于计算绑定；项目采用分别进入 `model_dataset_bindings.json` 或 `model_proxy_bindings.json`。
<!-- LCA_ASSOCIATION:END -->

<!-- CHANGELOG:START -->
## 修改日志

### 2026-08-03 · 断言级溯源受控合并

- **发现的问题：** 本次送审断言中有 0 条取得独立支持、9 条证据不足；另有 0 条因冲突、共享引用或锚点不唯一未自动修改。
- **采取的修改：** 已核实断言改挂专属 `ku-*` 引用；证据不足断言移除装饰性旧引用并明确降级。
- **修改原则：** 只修改哈希一致且唯一命中的原文行；不整页重写；不机械处理矛盾；来源注册、正文引用与修改日志同步更新。
- **数据影响：** 本次处理 9 条可安全合并断言；未新增或推断任何 LCI 数值。

### 2026-07-30 · 从“预先强绑定”改为“可引用数据集关联”

- **发现的问题：** 节点 Wiki 的目的，是让后续 LCA 建模快速发现可直接引用或可作代理的数据集；旧栏目只展示正式绑定和 C2，隐藏了具有代理筛选价值的 C1，也把部分真实相邻记录直接归入否决，容易把“关联强弱”误读成“能否立即计算”。
- **采取的修改：** 按冻结的官方/公开证据重新裁决本节点的数据集引用关系，当前展示 3 条关联（C0=0、C1=3、C2=0、C3=0、C4=0），另保留 1 条真正否决；每条同时标记关联对象、潜在用途、主要限制和项目裁决要求。
- **修改原则：** C0–C4 只表示节点—数据集关联强度，不授予计算权限；C1/C2 可进入项目级 P0–P3 代理裁决，C3/C4 也必须在具体模型中核对边界、许可和版本后才形成真正绑定。
<!-- CHANGELOG:END -->
