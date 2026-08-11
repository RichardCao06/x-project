---
id: P018
node_type: product
display_name: "主板PCBA, 通用服务器用"
boundary: foreground
facets: {equipment_class: "compute_card", integration_level: "subsystem", compute_type: "cpu_compute", form_factor: "na", product_subtype: "mainboard"}
produced_by: [A001]
consumed_by: [A009, A010, A012, A015, A016, A017, A033, A037, A039, A040]
external: {cpc: "4529", hs: "8473.30"}
confidence: core
provenance_refs: [ecoinvent, gabi, hs, isic, ku-hiqlcd-public-ict-2026]
spine_hash: "c3a19e7f64e2"
schema_version: wiki-v1
sigil: "sigil/P018.svg"
body_status: draft
claim_verification_status: partial
---
<!-- BODY:START -->
## 定义与产品身份

本节点表示用于通用服务器的主板印制电路板组件（PCBA）子系统，而非裸印制线路板、独立处理器或完整服务器整机。 〔未核实·模型回忆〕 [^internal-review]

## 性质与形态

该产品以已完成电子装配的主板PCBA实体形态交付，其计算功能定位为CPU计算用途的服务器子系统。 〔未核实·模型回忆〕 [^internal-review]

## 参考流与交接边界

参考流应定义为一块完成制造的通用服务器主板PCBA，交接点位于主板PCBA制造活动A001的产品输出。 〔未核实·模型回忆〕 [^internal-review]

## 规格与相邻节点区分

本节点按通用服务器主板PCBA的子系统身份建模，不与整机P017混同，也不把其他设备类别或未装配PCB纳入同一节点。 〔未核实·模型回忆〕 [^internal-review]

## 在系统中的角色

该节点是多个ICT设备装配活动的共同输入，包括A009、A010、A012、A015、A016、A017、A033、A037、A039和A040，承担核心计算硬件子系统角色。 〔未核实·模型回忆〕 [^internal-review]

## 分类与适用范围

本节点适用于前景ICT设备制造中的通用服务器主板PCBA；其适用范围由服务器用途、CPU计算定位和子系统集成层级共同限定。 〔未核实·模型回忆〕 [^internal-review]

## 节点特定采集字段

采集时应记录板卡尺寸与层数、处理器和内存接口配置、已装配元件类别、表面处理及制造测试状态，以确认主板PCBA的身份和配置。 〔未核实·模型回忆〕 [^internal-review]

## 区域化补充要求

区域化记录应补充PCBA制造和装配地点、关键电子元件供应市场及适用的电子产品合规要求，以支持地域代表性判断。 〔未核实·模型回忆〕 [^internal-review]

## 数据适用状态与缺口

该节点有候选数据来源和5项LCA关联记录，但尚无已验证来源；具体主板配置、元件清单、制造工艺和区域代表性仍需后续核验。 〔未核实·模型回忆〕 [^internal-review]

## 出处

[^internal-review]: 内部评审与建模约定——仅支持显式标注的系统边界、参考流、采集字段与数据缺口判断，不作为外部事实证据。
<!-- BODY:END -->

## 邻域工序图
> 由 graph edges 确定性派生（图==边，可被 lint 比对）；模型不得增删连线。

```mermaid
flowchart LR
  A001(("SMT贴装, 回流焊接 | 主板PCBA, 通用服务器用")) ==> P018
  P018["主板PCBA, 通用服务器用"]
  P018 --> A009(("系统集成, 整机总装 | 服务器, 通用计算, 2U"))
  P018 --> A010(("系统集成, 整机总装 | 服务器, 通用计算, 1U"))
  P018 --> A012(("系统集成, 整机总装 | GPU服务器, AI推理, 2U"))
  P018 --> A015(("系统集成, 整机总装 | 存储阵列, 全闪存, 4U"))
  P018 --> A016(("系统集成, 整机总装 | 存储阵列, 机械硬盘, 4U"))
  P018 --> A017(("系统集成, 整机总装 | UPS不间断电源, 2U"))
  P018 --> A033(("系统集成, 整机总装 | 存储阵列, 机械硬盘, 2U"))
  P018 --> A037(("系统集成, 整机总装 | 机架PDU, 1U"))
  P018 --> A039(("系统集成, 整机总装 | 服务器, 通用计算, 刀片式"))
  P018 --> A040(("系统集成, 整机总装 | 笔记本电脑"))
```

## 🔒 数量（待挂 · NOT POPULATED）
> 本节为占位。输入流 / 输出流 / 参考单位的结构在此预留，数值由实测/权威库挂入，**LLM 不得填写**（数量防火墙）。

| 流 | 方向 | 单位 | 值 | 源 |
|---|---|---|---|---|
| (待挂) | — | — | — | — |

<!-- LCA_ASSOCIATION:START -->
## 🔗 可引用 LCA 数据集与关联

> 已核验关联 5 条：C0=4、C1=1、C2=0、C3=0、C4=0。这些记录用于发现与代理筛选；进入计算前仍须在具体项目中核对边界、许可和版本。

| 强度 | 数据库记录 | 数据库/版本 | 关联对象 | 潜在用途 | 主要限制 | 模型状态 |
|---|---|---|---|---|---|---|
| `C1` · 弱关联 | [印制线路板组件](https://www.hiqlcd.com/dataset/hiqlcd/1.5.0/cut_off/e8b682ed-6133-44d5-8ef8-6603d9323a78) | HiQLCD 1.5.0 | `reference_product` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：product、reference_product、activity、route、boundary、geography、time。HiQLCD 记录是中国通用 PCBA 市场产品，参考单位为 kg；未证… | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C0` · 外部对照 | [プリント配線実装基板, GLO](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `external_product_result` | 外部对照；不可替代 | 数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C0` · 外部对照 | [プリント配線実装基板, JPN](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `external_product_result` | 外部对照；不可替代 | 数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C0` · 外部对照 | [printed wiring board production, mounted mainboard, desktop computer, Pb free](https://ecoquery.ecoinvent.org/3.12/cutoff/dataset/1091/documentation) | ecoinvent 3.12 | `reference_product` | 外部对照；不可替代 | 数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C0` · 外部对照 | [Printed circuit and electronic assembly - United States](https://api.nal.usda.gov/FederalLCACommonsapi/search?query=9a21e1ba-46e9-39b5-a3d3-0004486ec40c&type=PROCESS) | Federal LCA Commons repository current at access date | `external_product_result` | 外部对照；不可替代 | 数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |

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
- **采取的修改：** 按冻结的官方/公开证据重新裁决本节点的数据集引用关系，当前展示 5 条关联（C0=4、C1=1、C2=0、C3=0、C4=0），另保留 0 条真正否决；每条同时标记关联对象、潜在用途、主要限制和项目裁决要求。
- **中国源补检：** 本节点新增 1 条 HiQLCD 官方公共元数据关联；已核验稳定 UUID、版本、系统模型、参考产品/单位、地域、时间和公开边界，完整 I/O/LCIA 仍受许可控制。
- **修改原则：** C0–C4 只表示节点—数据集关联强度，不授予计算权限；C1/C2 可进入项目级 P0–P3 代理裁决，C3/C4 也必须在具体模型中核对边界、许可和版本后才形成真正绑定。
<!-- CHANGELOG:END -->
