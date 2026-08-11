---
id: P082
node_type: product
display_name: "卡槽连接器, PCIe用"
boundary: background
facets: {equipment_class: "compute_card", integration_level: "component", compute_type: "na", form_factor: "na", product_subtype: "connector_pcie_card_edge_socket"}
produced_by: []
consumed_by: [A001, A002]
external: {cpc: "4731", hs: "8536.69"}
confidence: core
provenance_refs: [internal-review, ku-hiqlcd-public-ict-2026]
spine_hash: "29bfc22845a5"
schema_version: wiki-v1
sigil: "sigil/P082.svg"
body_status: draft
claim_verification_status: partial
---
<!-- BODY:START -->
## 定义与产品身份

该节点表示供PCIe扩展卡插入的卡槽连接器，即主板侧PCIe卡边插座，而非扩展卡上的金手指触点。 〔未核实·模型回忆〕 [^internal-review]

## 性质与形态

PCIe卡槽连接器是安装在主板上的卡边插座，通过成排触点与扩展卡卡边触点配合，提供电气连接和机械保持。 〔未核实·模型回忆〕

## 参考流与交接边界

参考流应为一个可交付给主板或相关PCB装配活动的PCIe卡槽连接器；扩展卡、主板PCB、焊料和固定件应作为相邻产品或材料流处理。 〔未核实·模型回忆〕 [^internal-review]

## 规格与相邻节点区分

该节点应按PCIe卡边插座识别，并按实际需要区分通道长度、插槽代际、直立或直角安装；不得与PCIe卡边金手指或显示输出连接器混用。 〔未核实·模型回忆〕 [^internal-review]

## 在系统中的角色

该产品作为相关PCB装配活动的背景电子输入，为独立显卡等PCIe扩展卡提供主机侧互连接口。 〔未核实·模型回忆〕 [^internal-review]

## 分类与适用范围

适用范围限于组件级PCIe卡槽连接器，不包括M.2插座、内存插槽、线缆连接器或扩展卡本体上的配对卡边触点。 〔未核实·模型回忆〕 [^internal-review]

## 节点特定采集字段

采集时应记录PCIe插槽长度或通道数、代际、安装方向、端子数、端子镀层、壳体材料、质量及额定插拔寿命。 〔未核实·模型回忆〕 [^internal-review]

## 区域化补充要求

区域化时应补充连接器制造、端子冲压和电镀、注塑成型及主板装配所在地，并识别关键铜合金和贵金属镀层供应地区。 〔未核实·模型回忆〕 [^internal-review]

## 数据适用状态与缺口

该节点目前为未解析的背景占位项，缺少电子行业解析目标以及与特定PCIe规格、材料构成和制造地域相对应的证据。 〔未核实·模型回忆〕 [^internal-review]

## 出处

[^internal-review]: 内部评审与建模约定——仅支持显式标注的系统边界、参考流、采集字段与数据缺口判断，不作为外部事实证据。
<!-- BODY:END -->

## 邻域工序图
> 由 graph edges 确定性派生（图==边，可被 lint 比对）；模型不得增删连线。

```mermaid
flowchart LR
  P082["卡槽连接器, PCIe用"]
  P082 --> A001(("SMT贴装, 回流焊接 | 主板PCBA, 通用服务器用"))
  P082 --> A002(("SMT贴装, 回流焊接 | 主板PCBA, AI训练GPU服务器用"))
```

## 🔒 数量（待挂 · NOT POPULATED）
> 本节为占位。输入流 / 输出流 / 参考单位的结构在此预留，数值由实测/权威库挂入，**LLM 不得填写**（数量防火墙）。

| 流 | 方向 | 单位 | 值 | 源 |
|---|---|---|---|---|
| (待挂) | — | — | — | — |

<!-- LCA_ASSOCIATION:START -->
## 🔗 可引用 LCA 数据集与关联

> 已核验关联 7 条：C0=0、C1=7、C2=0、C3=0、C4=0。这些记录用于发现与代理筛选；进入计算前仍须在具体项目中核对边界、许可和版本。

| 强度 | 数据库记录 | 数据库/版本 | 关联对象 | 潜在用途 | 主要限制 | 模型状态 |
|---|---|---|---|---|---|---|
| `C1` · 弱关联 | [プリント配線板用コネクタ, GLO](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `reference_product_and_producer_process` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：product_family、connector_subtype、geography。IDEA公开样本仅暴露印制板用或其他连接器产品族，未证明该节点特定的连接器子型、触点、壳体、镀层和交付边界。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C1` · 弱关联 | [プリント配線板用コネクタ, JPN](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `reference_product_and_producer_process` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：product_family、connector_subtype、geography。IDEA公开样本仅暴露印制板用或其他连接器产品族，未证明该节点特定的连接器子型、触点、壳体、镀层和交付边界。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C1` · 弱关联 | [コネクタ (プリント配線板用コネクタを除く), GLO](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `reference_product_and_producer_process` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：product_family、connector_subtype、geography。IDEA公开样本仅暴露印制板用或其他连接器产品族，未证明该节点特定的连接器子型、触点、壳体、镀层和交付边界。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C1` · 弱关联 | [コネクタ (プリント配線板用コネクタを除く), JPN](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `reference_product_and_producer_process` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：product_family、connector_subtype、geography。IDEA公开样本仅暴露印制板用或其他连接器产品族，未证明该节点特定的连接器子型、触点、壳体、镀层和交付边界。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C1` · 弱关联 | [electric connector production, peripheral component interconnect buss](https://ecoquery.ecoinvent.org/3.12/cutoff/dataset/828/documentation) | ecoinvent 3.12 | `reference_product_and_producer_process` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：reference_product、connector_generation、geography。官方记录明确为计算机 PCI 总线连接器，与 PCIe 卡槽同属卡边插槽产品族；但未证明 PCI/PCI… | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C1` · 弱关联 | [电连接器](https://www.hiqlcd.com/dataset/hiqlcd/1.5.0/cut_off/6758aa72-f6d7-41d9-9297-7e1bd9ca9897) | HiQLCD 1.5.0 | `reference_product` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：product、reference_product、activity、route、boundary、geography、time。中国通用电连接器记录覆盖注塑、冲压、电镀、组装和检测，但没有钉死 BTB… | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C1` · 弱关联 | [Connector PCI (2,6 g, 72 pins, gold plated) - based on parametric plan model](https://lcadatabase.sphera.com/2026/xml-data/processes/bbfe14ab-94d2-4aa0-bcca-7d0678fc0a2b.xml) | Sphera Managed LCA Content 2026.1 | `reference_product_and_producer_process` | 弱关联；可进入代理筛选 | 未通过节点特定硬门：product_family、connector_generation、geography。Sphera PCI连接器与PCIe卡槽属于相邻代际产品，但未证明PCIe代际、触点、材料、镀层和中国代表性。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |

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
- **采取的修改：** 按冻结的官方/公开证据重新裁决本节点的数据集引用关系，当前展示 7 条关联（C0=0、C1=7、C2=0、C3=0、C4=0），另保留 1 条真正否决；每条同时标记关联对象、潜在用途、主要限制和项目裁决要求。
- **中国源补检：** 本节点新增 1 条 HiQLCD 官方公共元数据关联；已核验稳定 UUID、版本、系统模型、参考产品/单位、地域、时间和公开边界，完整 I/O/LCIA 仍受许可控制。
- **修改原则：** C0–C4 只表示节点—数据集关联强度，不授予计算权限；C1/C2 可进入项目级 P0–P3 代理裁决，C3/C4 也必须在具体模型中核对边界、许可和版本后才形成真正绑定。
<!-- CHANGELOG:END -->
