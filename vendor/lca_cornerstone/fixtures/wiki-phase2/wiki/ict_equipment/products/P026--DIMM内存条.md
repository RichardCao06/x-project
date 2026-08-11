---
id: P026
node_type: product
display_name: "DIMM内存条"
boundary: foreground
facets: {equipment_class: "memory_module", integration_level: "subsystem", compute_type: "na", form_factor: "na", product_subtype: "内存条DIMM"}
produced_by: [A006, A035]
consumed_by: [A009, A010, A011, A012, A015, A016, A032, A033, A035, A039, A040]
external: {cpc: "4529", hs: "8473.30"}
confidence: core
provenance_refs: [ecoinvent, hs, isic, ku-hiqlcd-public-ict-2026]
spine_hash: "0322ac250544"
schema_version: wiki-v1
sigil: "sigil/P026.svg"
body_status: draft
claim_verification_status: partial
---
<!-- BODY:START -->
## 定义与产品身份

DIMM是双列直插式内存模块，由存储器件和模块PCB组成，用于通过标准化模块接口向主机提供易失性工作存储。 〔未核实·模型回忆〕

## 性质与形态

DIMM通常是装配DRAM封装、SPD器件和边缘连接器的矩形PCB模块，其引脚通常分列在连接器两侧并独立电气连接。 〔未核实·模型回忆〕

## 参考流与交接边界

本节点的参考流宜定义为一条完成制造、测试并在出厂交付点可安装于兼容主板的DIMM内存条；主板或整机装配不属于该产品的交接边界。 〔未核实·模型回忆〕 [^internal-review]

## 规格与相邻节点区分

DIMM以模块级易失性存储身份区别于SSD模组等非易失性存储模块，也区别于未封装的DRAM芯片。 〔未核实·模型回忆〕

## 在系统中的角色

该内存模块由A006和A035生产，并被A009、A010、A011、A012、A015、A016、A032、A033、A035、A039和A040下游活动消耗。 〔未核实·模型回忆〕 [^internal-review]

## 分类与适用范围

该节点覆盖integration_level=subsystem的DIMM内存模块，不以容量、DDR代际或是否带纠错功能另设产品身份。 〔未核实·模型回忆〕 [^internal-review]

## 节点特定采集字段

采集时应记录DIMM类型、DRAM封装数量与容量、模块代际、是否ECC、PCB尺寸和层数、连接器材料及测试良率。 〔未核实·模型回忆〕 [^internal-review]

## 区域化补充要求

区域化应补充DRAM制造与封装、模块PCB和组装测试的生产区域，以及最终交付区域。 〔未核实·模型回忆〕 [^internal-review]

## 数据适用状态与缺口

现有档案未附经核验的来源，且缺少DIMM代际、容量、DRAM封装数量、制造地点和供应链配置；不能据此推导型号级清单。 〔未核实·模型回忆〕 [^internal-review]

## 出处

[^internal-review]: 内部评审与建模约定——仅支持显式标注的系统边界、参考流、采集字段与数据缺口判断，不作为外部事实证据。
<!-- BODY:END -->

## 邻域工序图
> 由 graph edges 确定性派生（图==边，可被 lint 比对）；模型不得增删连线。

```mermaid
flowchart LR
  A006(("SMT贴装, 回流焊接 | DIMM内存条")) ==> P026
  A035(("烧录测试, 压力老化 | DIMM内存条")) ==> P026
  P026["DIMM内存条"]
  P026 --> A009(("系统集成, 整机总装 | 服务器, 通用计算, 2U"))
  P026 --> A010(("系统集成, 整机总装 | 服务器, 通用计算, 1U"))
  P026 --> A011(("系统集成, 整机总装 | GPU服务器, AI训练, 4U"))
  P026 --> A012(("系统集成, 整机总装 | GPU服务器, AI推理, 2U"))
  P026 --> A015(("系统集成, 整机总装 | 存储阵列, 全闪存, 4U"))
  P026 --> A016(("系统集成, 整机总装 | 存储阵列, 机械硬盘, 4U"))
  P026 --> A032(("系统集成, 整机总装 | 存储阵列, 全闪存, 2U"))
  P026 --> A033(("系统集成, 整机总装 | 存储阵列, 机械硬盘, 2U"))
  P026 --> A035(("烧录测试, 压力老化 | DIMM内存条"))
  P026 --> A039(("系统集成, 整机总装 | 服务器, 通用计算, 刀片式"))
  P026 --> A040(("系统集成, 整机总装 | 笔记本电脑"))
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
| `C0` · 外部对照 | [printed wiring board production, surface mounted, unspecified, Pb free](https://ecoquery.ecoinvent.org/3.12/cutoff/dataset/1415/documentation) | ecoinvent 3.12 | `reference_product` | 外部对照；不可替代 | 数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
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
