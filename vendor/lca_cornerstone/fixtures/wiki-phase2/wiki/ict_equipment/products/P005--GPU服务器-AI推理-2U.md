---
id: P005
node_type: product
display_name: "GPU服务器, AI推理, 2U"
boundary: foreground
facets: {equipment_class: "server", integration_level: "system", compute_type: "gpu_ai_inference", form_factor: "2u", product_subtype: "GPU推理服务器, 2u, gpu_ai_inference"}
produced_by: [A012]
consumed_by: []
external: {cpc: "4521", hs: "8471.50"}
confidence: core
provenance_refs: [ecoinvent, hs, isic]
spine_hash: "76a7efa516b2"
schema_version: wiki-v1
sigil: "sigil/P005.svg"
body_status: draft
claim_verification_status: partial
---
<!-- BODY:START -->
## 定义与产品身份

该节点表示采用2U机架式外形、以GPU为主要加速器并面向AI推理工作负载的完整服务器系统。 〔未核实·模型回忆〕

## 性质与形态

该类系统采用2U机架式机箱，并集成CPU、GPU加速器、内存、存储、电源和散热部件。 〔未核实·模型回忆〕

## 参考流与交接边界

参考流应为一台已完成装配并可交付的2U GPU AI推理服务器，交接边界设在服务器制造商的成品出厂点。 〔未核实·模型回忆〕 [^internal-review]

## 规格与相邻节点区分

该节点以2U外形和GPU AI推理计算身份区别于CPU通用服务器，也区别于主要面向AI训练的GPU服务器。 〔未核实·模型回忆〕 [^internal-review]

## 在系统中的角色

该产品由GPU AI推理服务器制造活动产出，作为前景服务器产品在该图中不再指定下游消费活动。 〔未核实·模型回忆〕 [^internal-review]

## 分类与适用范围

作为自动数据处理设备的服务器系统，该产品可参照协调制度品目84.71；其制造活动可与ISIC Rev.4类别2620作交叉参照。 〔未核实·模型回忆〕

## 节点特定采集字段

采集应至少记录GPU型号和数量、CPU配置、内存、存储、主板、机箱、电源、散热部件及装配层级，并区分推理用途相关配置。 〔未核实·模型回忆〕 [^internal-review]

## 区域化补充要求

应补充服务器及GPU等关键部件的制造和装配地区，以及面向目标市场的运输与供应链区域信息。 〔未核实·模型回忆〕 [^internal-review]

## 数据适用状态与缺口

当前节点未附带可直接使用的生命周期清单数值；GPU配置、部件质量、制造过程和供应链来源需要以具体产品数据补足。 〔未核实·模型回忆〕 [^internal-review]

## 出处

[^internal-review]: 内部评审与建模约定——仅支持显式标注的系统边界、参考流、采集字段与数据缺口判断，不作为外部事实证据。
<!-- BODY:END -->

## 邻域工序图
> 由 graph edges 确定性派生（图==边，可被 lint 比对）；模型不得增删连线。

```mermaid
flowchart LR
  A012(("系统集成, 整机总装 | GPU服务器, AI推理, 2U")) ==> P005
  P005["GPU服务器, AI推理, 2U"]
```

## 🔒 数量（待挂 · NOT POPULATED）
> 本节为占位。输入流 / 输出流 / 参考单位的结构在此预留，数值由实测/权威库挂入，**LLM 不得填写**（数量防火墙）。

| 流 | 方向 | 单位 | 值 | 源 |
|---|---|---|---|---|
| (待挂) | — | — | — | — |

<!-- LCA_ASSOCIATION:START -->
## 🔗 可引用 LCA 数据集与关联

> 已核验关联 4 条：C0=4、C1=0、C2=0、C3=0、C4=0。这些记录用于发现与代理筛选；进入计算前仍须在具体项目中核对边界、许可和版本。

| 强度 | 数据库记录 | 数据库/版本 | 关联对象 | 潜在用途 | 主要限制 | 模型状态 |
|---|---|---|---|---|---|---|
| `C0` · 外部对照 | [はん用コンピュータ, GLO](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `external_product_result` | 外部对照；不可替代 | 数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C0` · 外部对照 | [はん用コンピュータ, JPN](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `external_product_result` | 外部对照；不可替代 | 数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C0` · 外部对照 | [ミッドレンジコンピュータ, GLO](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `external_product_result` | 外部对照；不可替代 | 数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |
| `C0` · 外部对照 | [ミッドレンジコンピュータ, JPN](https://sumpo.or.jp/consulting/lca/idea/din2eh00000001km-att/AIST-IDEAv34_Ja_sample.xlsx) | AIST-IDEA 3.4 public sample | `external_product_result` | 外部对照；不可替代 | 数据集边界覆盖完整产品/行业聚合，直接挂接会与已展开前景链重复计入。 | 仅知识关联 · 仍需项目裁决 · 不可直接计算 |

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
- **采取的修改：** 按冻结的官方/公开证据重新裁决本节点的数据集引用关系，当前展示 4 条关联（C0=4、C1=0、C2=0、C3=0、C4=0），另保留 0 条真正否决；每条同时标记关联对象、潜在用途、主要限制和项目裁决要求。
- **修改原则：** C0–C4 只表示节点—数据集关联强度，不授予计算权限；C1/C2 可进入项目级 P0–P3 代理裁决，C3/C4 也必须在具体模型中核对边界、许可和版本后才形成真正绑定。
<!-- CHANGELOG:END -->
