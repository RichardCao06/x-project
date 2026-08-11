# P003 节点 Wiki 生产全流程复盘与 Golden Case 工作流

日期：2026-08-11  
复盘对象：`ict_equipment::P003`（服务器，通用计算，刀片式）  
工程范围：`/Users/shujudagongren/Myspace/lca-project`  
主要批次：`wiki-production-20260810-v3` 至 `wiki-production-20260811-v30`

---

## 1. 执行摘要

P003 并不是“同一篇正文写错了三十次”。从可追溯 Artifact 看，v3–v30 实际经历了五类不同的产品化问题：

1. **Agent 运行与证据链能否被信任**：模型输出协议、运行证明、禁网 Verify、节点身份对齐、证据原文和 hash 是否成立。
2. **批次能否安全发布**：Draft Gate、coverage、Go/No-Go、事务 Apply、完整行业快照、bundle 和 viewer 是否形成闭环。
3. **内容是否达到 Golden 深度**：早期 Gate 能证明页面“可靠、结构完整”，却不能证明内容深入、有解释力。
4. **正文是否像一篇文章**：扩写后出现 claim 拼接、标签泛滥、出处重复、链接失效和同源引用未合并，技术上通过但阅读体验不合格。
5. **表格是否真有可用数据**：表结构存在不等于数据已采集；国际参考、中国参考、实测、定义值、代理值和明确缺口需要分别治理。

因此，轮次很多的根本原因不是单一 Agent 能力不足，而是**完成定义被逐层发现**：先定义“证据正确”，再定义“内容足够深”，再定义“可读”，最后才定义“表格有数据且地域语义正确”。每出现一类用户可见问题，系统才补上一层 Gate。换言之，早期流程优化的是“不要错”，用户真正需要的是“先达到内容价值，再在价值不退化的前提下保证不出错”。

截至 v30，P003 已完成以下闭环：

- 外部事实与节点身份逐 claim 核验；
- 深度正文、段落级编辑和受控建模判断；
- 正文隐藏内部审计标签，仅保留轻量引用入口；
- 同源引用合并、来源卡片可跳转；
- 物性、参数和数据质量表由来源绑定的数据填充；
- 国际参考与中国参考分轨，中国参数覆盖 14/16；
- Graph 11/11、Wiki lint 31/31、100 项项目测试和项目 validate 通过。

但仍不能把 v30 等同于“P003 已有完整 LCI”。v30 的中国数据是 H3C B5700 G6 的公开参考配置，其中中国值为定义值或代理值，**实测值仍为 0**；型号级 BOM、供应商矩阵、包装清单和共享机箱分配记录仍是明确缺口。

---

## 2. 复盘范围、证据与可信度

本报告依据以下持久化证据，而非仅依赖对话记忆：

- v3–v30 各工作区的 `journal.json`、`release-journal.json`；
- `nomination-runtime/`、`verify-runtime/` 中的 invocation、events、stderr、result、usage；
- `draft-content-gate.json`、`coverage.json`、`go-no-go.json`、`gate-report.json`、`quality-gate.json`；
- `preview-report.json`、`publish-report.json`、`viewer-repair-report.json`；
- v26–v30 的正式 P003 Markdown、bundle 和 viewer；
- `tests/wiki_phase2/` 中由事故固化的回归测试；
- `workflows/wiki-node-production@5.json` 及当前生产脚本；
- Phase 2、Phase 3 和 v27 Golden 重跑验收报告。

复盘采用两种证据等级：

- **已确认**：journal 或 Gate 明确记录停止原因、指标或失败项。
- **仅能确认停止点**：批次停在某状态，但旧 journal 没有记录调用失败原因。此类回合不推测具体错误；这本身也是“失败原因未结构化持久化”的工程缺陷。

v1–v2 没有找到 P003 的独立生产批次；可追溯的 P003 尝试从 v3 开始。因此本文不会把平台搭建工作虚构成两轮 P003 内容生产。

---

## 3. 完整时间线：每轮发生了什么

### 3.1 v3–v12：从 Agent 可运行到 Draft Gate 能阻止坏页面

| 版本 | 最终状态 | 暴露问题 | 处理与结果 |
|---|---|---|---|
| v3 | `prepared` | Nomination 运行多次超时并回退 HTTP，未形成 `nomination-result.json`。 | 批次未进入研究阶段，没有写页面。 |
| v4 | `prepared` | 与 v3 相同，只有运行事件和 stderr，没有可验收结果。 | 保持在 prepared，继续校准 launcher/runtime。 |
| v5 | `prepared` | 与 v3/v4 相同，模型调用完成性仍不稳定。 | 未把不完整运行伪装成提名成功。 |
| v6 | `prepared` | 首次形成结果，但生成 31 条 claim，而合同目标是 30 个冻结 slot；旧 journal 没有记录后续命令失败详情。 | 结果未晋级；下一版重新生成。 |
| v7 | `blocked` | Verify 得到 27 条 `NOT_FOUND`、2 条 `UNRELATED/INSUFFICIENT`、1 条 `ADJACENT/INSUFFICIENT`，0 条外部 CONFIRMED；正文仅 1,863 字符、20 条断言、0 个外部来源。 | Draft Content Gate 在写入前阻断，正式页面不变。证明了“没有证据时不会硬写”。 |
| v8 | `prepared` | 重新提名并生成搜索证据，但 journal 只证明停在 prepared，未记录明确失败原因。 | 被后续更完整批次取代。 |
| v9 | `research_ready` | Verify runtime 只有 2 条 EXACT/CONFIRMED；6 条因机箱、模块或活动对象混淆被判 ADJACENT，另有 2 条证据不足。 | 不接受 Verify，继续修改 claim 主语和来源路由。 |
| v10 | `blocked` | 一次 finalize 发现重复 extract result；语义上只有 4 条 CONFIRMED，3 条 ADJACENT，且“性质与形态”无合格外部证据。 | 去重后仍被 Draft Gate 以核心章节未落证阻断。 |
| v11 | `research_ready` | 进步到 5 条 CONFIRMED，但仍有 2 条 ADJACENT、3 条 EXACT/INSUFFICIENT。 | 继续收紧“成品刀片服务器”与“服务器刀片模块/机箱”的对象边界。 |
| v12 | `frozen` | 7 条 CONFIRMED、3 条 ADJACENT。Draft 内容本身通过，但 coverage 只有 90%，页面不可晋级；Preview 又发现隔离 fixture 缺行业页面和 16 个 sigil，Wiki lint 仅 28/30。 | 没有 reviewed/publish。由此发现“单节点候选合格”与“行业发布快照完整”是两个独立条件。 |

这一阶段解决的是最基础的安全问题：Agent 只能提名，Search/Fetch 确定性执行，Verify 独立且禁网；只要存在 ADJACENT、证据不足或运行证明不完整，就不能把页面升级为 reviewed。

### 3.2 v13–v26：反复消除对象错位、来源单一和发布快照缺失

| 版本 | 最终状态 | 暴露问题 | 处理与结果 |
|---|---|---|---|
| v13 | `prepared` | 30 条新提名已生成，但未形成持久化研究/核验结果；旧 journal 未记录中止原因。 | 未晋级，被后续来源路由版本取代。 |
| v14 | `prepared` | 同样停在 prepared，只能确认该轮未进入 Verify。 | 不纳入可发布证据链。 |
| v15 | `research_ready` | Verify 为 7 CONFIRMED、3 ADJACENT；模块化计算模块仍被误当系统级成品。 | 继续调整节点主题定位和 claim constraint。 |
| v16 | `research_ready` | 6 CONFIRMED、3 ADJACENT、1 EXACT/INSUFFICIENT；同一来源原文仍被过度外推。 | 缩短断言，使 claim 不超过原文。 |
| v17 | `research_ready` | 提名再次出现 31 条；Verify 只有 5 CONFIRMED，4 条 EXACT 但证据不足、1 条 ADJACENT。 | 放弃该候选，恢复严格 slot 基数。 |
| v18 | `prepared` | 形成 30 条提名，但未进入持久化 Verify。 | 作为路由调整过渡版本，不参与发布。 |
| v19 | `failed` | 10 条外部事实已全部 EXACT/CONFIRMED，coverage 100%，但全部来自 `publications.europa.eu`；质量 Gate 的“两家独立 authority”失败。 | Gate 拒绝单一来源闭环，要求引入独立权威来源。 |
| v20 | `frozen` | 引入 IBM 后有两家 authority，但提名/冻结结果为 31 条；coverage 数字虽为 100%，页面仍不可 reviewed。 | 说明“覆盖率 100%”不能掩盖 claim 集合基数漂移，批次未发布。 |
| v21 | `blocked` | Verify 文件显示 30 条，但 post-apply coverage 变成 34 条，只覆盖 26 条，8 条进入 manual review；Go/No-Go 为 NO_GO。 | 阻断 reviewed apply，暴露生成正文、KUs 和 coverage 集合守恒不足。 |
| v22 | `research_ready` | 达到 9 CONFIRMED，仅 1 条仍把紧凑计算模块当成成品而被判 ADJACENT。 | 继续重写该 slot，限定原文对象必须是目标产品本体。 |
| v23 | `prepared` | 新提名形成但未进入持久化 Verify；无结构化失败原因。 | 被下一版取代。 |
| v24 | `research_ready` | 8 CONFIRMED、1 ADJACENT、1 EXACT/INSUFFICIENT；IBM“模块化服务器系统”仍混合整机与服务器刀片模块。 | IBM 只保留能直接支持目标本体的同义/高密度描述，物理形态和共享资源边界改由欧盟法规原文支撑。 |
| v25 | `frozen` | 首次达到 10/10 EXACT/CONFIRMED、两家 authority，Draft Gate 通过；但 Preview 的完整性 lint 仍为 28/30：缺大量行业页面和 16 个 sigil。 | 不在冻结批次内补文件；先扩充只读 vendor snapshot，再新建最终工作区。 |
| v26 | `published` | 30 条 claim、10 条外部事实全部 EXACT/CONFIRMED、coverage 100%，Graph 11/11、Wiki lint 31/31，正式发布成功。发布后又发现 publish 只构建 data bundle，漏掉 HTML viewer。 | 补生成 viewer，并修改发布器：bundle 与 viewer 必须同时 PASS；加入隔离回归测试。 |

v26 是“真实证据到正式发布”的第一个闭环，但它只证明了**可靠性和发布完整性**，尚未证明达到用户期望的内容深度。

### 3.3 v27：可靠但浅，第一次把“内容价值”升为硬 Gate

用户阅读 v26 后指出与 Golden Case 差距很大。对比结果很清楚：v26 BODY 只有 4,453 字符、30 条断言，基本是一条 claim 对应一个单句段落；它满足证据和结构要求，却没有形成深入的建模说明。

v27 引入 Content Blueprint 和 Content Architect：

- 研究层仍冻结原来的 30 条 claim，不能借“扩写”偷偷增加外部事实；
- 内容层允许增加受控 `modeling_judgment` 和 `evidence_gap`；
- 冻结九个正文主题、正文长度、断言数、段落数、建模深度、必需术语、禁止短语和三张节点专属表；
- coverage 从只检查研究 claim，升级为研究 claim 与内容 claim 双向守恒。

该轮内部又暴露了三类问题：

1. 首次 Nomination 生成了会随着核验状态变化而失真的缺口句；修复为只描述长期存在的型号 BOM、净质量、制造、运输、共享资源分配和代表性缺口。
2. 首次 Content 输出结构合格，但纯句子正文只有 5,462 字，低于 6,500 字阈值；Draft Gate 在 Apply 前拒绝。加入“对象—字段—适用条件—失效条件”和长度缓冲后达到 7,110 字。
3. Preview 发现数据质量表词表不合法，lint 未全过；通过新的 hash-locked remediation 事务修复，而不是手改已冻结页面。

最终 v27 达到 13,476 个 BODY 字符、135 条内容 claim、27 个多句段落、10 条外部 CONFIRMED，Graph 11/11、Wiki lint 31/31 并发布。

但 v27 的新 Gate 也产生了副作用：为了达到“建模判断数量”和“断言数量”，正文出现 111 个可见 `〔建模判断〕` 标签、125 个 `internal-review` 引用。长度和计数变好了，读者体验反而变差。它说明**可计数的深度代理指标可以防止过短，却不能单独代表写作质量**。

### 3.4 v28：从“扩写成功”到“文章可读”

用户指出 v27 像拼凑：例如“刀片服务器至少包含一个处理器和系统内存。来源。刀片服务器是高密度独立服务器设备。”两句虽各自有证据，却没有论述关系。

根因不是翻译，而是组装器的最小单元错误：

- Research 以 claim 为单位；
- Distill 也按 claim 逐句输出；
- 引用在每句后机械注入；
- 章节只规定“必须包含哪些 claim”，没有先规划段落主旨、句间关系和信息顺序；
- 建模判断用于填充深度阈值，却没有作为解释事实、限定边界和说明失效条件的修辞角色；
- 相同来源没有在表达层融合，导致同一原文被拆成多句重复陈述。

v28 为此增加结构化段落和独立编辑审查，但正式批次本身仍经历多次自我阻断：

- Verify runtime 绑定了 v27 旧路径，而不是 v28 当前冻结 evidence；
- 一次 Verify 输出缺 `result.protocol`；
- Content candidate 改写了 Verify 结果，违反“内容层不得改变证据裁决”；
- Draft Gate 连续两次阻断不合格候选；
- Go/No-Go 发现 confirmed 证明失效；
- release gate 一次失败后才通过。

编辑过程中又出现 11 个 P003 专用修复轮次，依次处理：对象名称统一、独立功能与机箱依赖、产品本体与组件边界、BOM/附件/包装边界、制造与使用阶段共享资源分配、分类准入顺序、代表性与数据粒度的区分、可追溯不等于实测等语义问题。最终 BODY 收敛到 10,791 字符、48 个自然段，外部事实被融合成段落论点，建模判断用于解释边界和使用条件。

同一阶段还修复了四个读者端问题：

- 去掉正文中的 `〔建模判断〕`、`internal-review` 和“已核实”可见标签；
- 删除与来源卡片重复的“出处/引用原文与核验记录”展示；
- 来源卡片优先使用 registry 中的正式 URL，恢复原文跳转；
- 按 source URL 合并同源 claim，一张来源卡片显示该来源支持的多条断言。

此外，`file://...html?id=P003` 在本地查看器中不稳定。构建器新增 `--start-node P003`，生成真实文件 `ict_equipment-wiki-P003.html`，避免依赖 query 参数；viewer 还绑定 bundle 内容 hash，减少本地浏览器继续使用旧 JS 的情况。

### 3.5 v29：发现“有表结构”不等于“采集了表数据”

用户继续检查发现物性/常数表、规格/工艺参数表和数据质量与代表性表虽然有行名，但基本没有数据。原因是此前 Content Blueprint 只要求“表头、行数和字段标签完整”，没有要求：

- 值必须来自冻结来源；
- 国际值和中国值要分别达到最低覆盖；
- 值必须标明实测、定义或代理；
- 明确缺口不能被当成已填数据；
- 参考配置必须说明型号、地域、年份和适用边界。

v29 新增独立 Table Population 子流程，并冻结 Dell PowerEdge FC430 2019 PCF 最高销量配置作为国际参考。首次表格候选看似有 3 个中国值，但实际把国际参考或“缺口判断”放进中国值轨，还使用了不受控口径。下游 Wiki lint 抓到三类问题：口径值不受控、数值缺实测/代理/定义标签、国际/中国来源地域错轨。

修复后：

- 口径统一为受控词表，如 `standard_spec`、`proxy`、`reference`；
- 所有数值显式标注 pedigree；
- 中国轨没有中国来源时恢复为明确缺口，而不是拿国际参考冒充；
- `缺口：...`、`未公开` 被统一识别为 null；
- viewer 不再把“缺口证据 10/10”显示成“中国值 10/10”。

v29 最终为：物性 8 行、7 行有值；参数 12 行、国际值 10、真实中国值 0；质量 10 行全部有评估或明确缺口。Graph 11/11、Wiki lint 31/31、98 项测试通过。

### 3.6 v30：不是中国没有服务器，而是检索策略没有进入中国官方产品资料

用户质疑中国没有刀片服务器数据。事实不是“没有企业生产”，而是此前来源策略集中于欧盟法规、IBM 定义和国际厂商 PCF，且 Gate 只要求两个 authority，并未要求中国来源覆盖。

v30 调整检索方向后，找到并冻结 H3C UniServer B5700 G6 的中国官方资料：

- 官方产品碳足迹资料给出中国制造/使用场景、配置、寿命、功耗与产品碳足迹；
- 官方用户指南给出尺寸、最大满配质量、处理器插槽、内存槽和 B16000 共享机箱关系；
- 产品页用于确认产品身份和产品族定位。

工程上新增 `params_cn_population_floor` 硬 Gate，防止未来页面在中国轨全空时仍以“表格完整”通过。最终 v30：

- 物性表 11 行，10 行有值；
- 参数表 16 行，国际值 12，中国值 14；
- 中国值中 11 个定义值、3 个代理值、0 个实测值；
- 数据质量表 10 行全部有评估或明确缺口；
- Graph 11/11、Wiki lint 31/31、100 项测试、项目 validate 和浏览器原文链接检查均通过。

---

## 4. 为什么会跑这么多轮

### 4.1 一开始没有完整的 Golden 完成合同

最初的完成定义主要是：十节齐全、claim 有分类、引用可解析、两家来源、coverage 100%、Gate 全过。这个合同能生产“安全的知识卡片”，却不足以生产“内容深入、连贯、可直接阅读、表格有数据的专业 Wiki”。用户每次评审实际上都在补充缺失的验收标准。

### 4.2 Gate 按已知事故生长，无法拦截尚未定义的质量问题

v26 的 Gate 没有错，它准确验证了当时写进政策的内容；问题是政策没有定义内容深度。v27 定义了深度，却用断言数和标签数做代理，未定义阅读流畅度。v28 定义了段落编辑，却尚未要求表格人口。v29 要求表格人口，却没有中国覆盖下限。每层 Gate 都是后验补齐。

### 4.3 研究、写作、编辑和展示曾被同一个 claim 数据结构绑死

证据核验的最佳粒度是原子 claim；文章写作的最佳粒度是段落和论证；页面展示的最佳粒度是轻量引用和合并来源卡。早期系统让同一种 claim 结构同时服务三者，必然出现“证据正确但文章像数据库 dump”。

### 4.4 节点身份边界没有在检索前充分冻结

P003 最难的并不是找到“blade server”文本，而是区分：成品刀片服务器、服务器刀片模块、刀片机箱、模块化服务器系统、一般服务器、制造活动。早期先搜后判断，导致大量候选在 Verify 才被判 ADJACENT。标准流程应在搜索前生成正反例和排除词，把错误对象挡在候选阶段。

### 4.5 来源策略只追求权威性和独立性，没有覆盖维度矩阵

“两家独立 authority”能避免单源，但不能自动带来规格、PCF、中国地域或型号数据。来源计划必须按用途分槽：法规定义、厂商规格、PCF/EPD、PCR/标准、地域代表、中国官方资料，而不是只统计域名数量。

### 4.6 失败原因没有从一开始结构化持久化

多个版本只停在 prepared 或 research_ready，journal 没有记录触发命令、异常类别和候选摘要。结果是后来只能确认“停在哪里”，不能完全证明“为什么停”。一个自治系统如果不能给失败分类，就无法可靠选择重试、改 query、改 claim、回滚还是升级人工。

### 4.7 节点专用补丁替代了通用能力升级

当前仓库存在 `curate_p003_editorial_repair.py` 及 round2–round11。它们证明语义问题得到了认真修复，但也证明修复仍大量依赖 P003 的硬编码位置和句子。若直接跑新节点，这些经验不会自动迁移。正确做法是把每一类问题提升为：

- 通用 Editorial Contract；
- 可复用检查器或 reviewer rubric；
- `defects/wiki/` 的最小失败样本；
- mutation test；
- 对应的自动返工路由。

### 4.8 工程完成和用户感知完成混在同一个“PASS”里

v26 从工程证据看已发布，但 viewer 漏构建；v27 数据变了，用户却因浏览器缓存、query 文件路径和版面标签感知为“没变化”。正式发布必须同时交付数据正确性、页面可达性、内容差异摘要和浏览器 smoke test，不能只报告 journal 为 published。

---

## 5. 暴露的核心问题与工程化修复

| 核心问题 | 逃逸表现 | 已有修复 | 仍需平台化的修复 |
|---|---|---|---|
| Agent 输出不稳定 | 超时、缺 protocol、31 条 claim、重复 result | 动态 schema、slot 基数、runtime attestation、resume | 所有异常写入统一 failure envelope；区分可重试与不可重试错误 |
| 节点身份错位 | 模块/机箱/活动被当成 P003 | 冻结 `node_identity`，EXACT 才可 CONFIRMED | 搜索前生成正例、反例、排除词和邻接对象测试集 |
| 原文与断言不等价 | 原文只说模块，claim 扩写成整机事实 | supporting quote 必须是 excerpt 子串，独立 Verify | 增加 entailment mutation：删限定词、换主语、换层级必须失败 |
| 内容可靠但浅 | v26 只有 30 个单句段 | Content Blueprint、深度阈值 | 用“问题覆盖”和“决策价值”取代单纯字数/断言数奖励 |
| 正文 claim 拼接 | 句间无关系、同源事实重复 | 段落 schema、rhetorical role、editorial fusion | 通用 paragraph planner；禁止 claim 顺序直接等于正文顺序 |
| 标签污染阅读 | 111 个建模标签、125 个 internal-review | reader 隐藏内部标签，只留外部引用 | 将审计信息彻底留在 bundle 数据层，正文 AST 不再含展示标签 |
| 来源重复/链接失效 | “出处”重复、卡片不跳原文、同源多卡 | URL 优先、来源卡合并、reader 回归测试 | 浏览器自动点击全部外部链接并记录 HTTP/下载结果 |
| 本地 viewer 不可达/旧缓存 | query 文件打不开、页面看似没更新 | `--start-node` 独立 HTML、bundle hash 绑定 | 发布证书写入页面版本和正文 diff 摘要，用户可见地显示 release ID |
| 表结构空壳 | 三张表有行名无数据 | 独立 table collect/verify/population gate | 每种节点类型定义最小字段集、来源类型和地域覆盖政策 |
| 地域语义错轨 | 国际数据被计为中国值、缺口被计为值 | null/gap 语义、region lint、pedigree 标签 | 地域要求从 advisory 升为按任务政策配置的硬 Gate |
| 中国检索方向缺失 | 错误得出“找不到中国数据” | H3C 官方产品/指南/PCF 路由，CN floor | 建立中文厂商词典、产品族词典、官方域名和文档类型路由 |
| 修复只作用于样本 | 11 个 P003 专用修复脚本 | 部分问题已进 pytest | 将 11 轮 delta 归并为通用 rubric 与 defect fixtures，P003 专用脚本退出生产路径 |
| 发布范围不完整 | 单节点过 Gate，行业 bundle 缺页/sigil/viewer | 完整 vendor snapshot、preview 六步、bundle+viewer | 发布 manifest 声明并校验全量资产闭包，禁止运行期临时补资产 |

---

## 6. 把 P003 固化为 Golden Case，Golden 到底应包含什么

P003 Golden 不能只是“当前 Markdown 文件”，否则任何文字变化都会造成脆弱的全文 hash 比较，也无法解释为什么合格。建议把 Golden 拆成七个可独立验收的层：

### G-A：节点身份 Golden

- 固定 P003 的 node dossier、spine hash、产品身份刻面和交接边界；
- 固定正例：完整装配、以 CPU 通用计算为主、刀片式交付的服务器产品；
- 固定反例：刀片机箱、主板 PCBA、服务器刀片未完成模块、机架式服务器、GPU 主导服务器、数据中心设施、制造活动；
- mutation 将主语替换为上述相邻对象时，Verify 必须拒绝。

### G-B：证据 Golden

- 10 个核心研究 slot 每个都有冻结 claim、EXACT verdict、supporting quote、source URL、payload hash 和 locator；
- 同一证据可以支持多个 claim，但 registry 只保留一个 source identity；
- 法规定义、独立厂商说明、规格/PCF、中国官方参考分别占明确来源槽位；
- 原文限定词、对象层级或地域被篡改时，Gate 必须失败。

### G-C：内容 Golden

- 九个正文主题全部覆盖，且每节回答事先定义的问题，而不是只满足字数；
- 每个段落有唯一 focus，句子具有 thesis、evidence、explanation、boundary 或 gap 角色；
- 相邻外部 claim 先融合为一个自然事实句，再写建模解释；
- 外部事实、图谱事实、建模判断和证据缺口在数据层可追溯，但读者正文只显示必要引用；
- 不把“断言更多、标签更多”直接当成质量更高。

### G-D：表格 Golden

- 物性、参数、数据质量三张表都有节点专属字段，不是通用空壳；
- 每个非空值绑定 source、basis、pedigree、region、reference configuration；
- `measured`、`defined`、`proxy` 和 `gap` 四种状态互斥；
- 国际值与中国值分轨，缺口证据不计入已填值；
- P003 当前基线至少保持 v30 的 10/11 物性、12/16 国际参数、14/16 中国参数、10/10 质量评估，除非新政策显式批准降级。

### G-E：阅读器 Golden

- 正文无 `〔建模判断〕`、`已核实` 和 `internal-review` 可见徽标；
- 每个外部事实附近只有轻量引用链接；
- 来源区不重复展示“出处”和“引用原文”；
- 同 URL 来源合并为一个来源卡，卡片列出支持的 claim 数；
- 所有来源卡可点击，P003 独立 HTML 无 query 参数即可打开；
- 页面可见 release ID、生成时间和数据摘要，避免新旧版本混淆。

### G-F：发布与回放 Golden

- 从 frozen inputs 可离线重放 compose、gate、apply、preview 和 publish；
- reviewed apply 绑定 coverage hash 和目标旧 hash；
- bundle、viewer、页面、registry 和 release journal 的 hash 互相闭合；
- 任一 Apply 中断可回滚，resume 不重复副作用；
- 原项目无写入、无 symlink 逃逸。

### G-G：缺陷 Golden

把 P003 历史事故做成最小 defect corpus，而不是保留 11 个节点专用改写脚本：

- 模块冒充整机；
- claim 超出 quote；
- 单一 authority；
- 31/30 claim 基数漂移；
- content 改写 Verify verdict；
- 两个事实句无连接地拼接；
- 可见审计标签泛滥；
- 同源引用未合并；
- registry 有 URL 但 reader 不使用；
- 空表通过；
- 缺口被计为数值；
- 国际数据进入中国轨；
- viewer 只构建 data 不构建 HTML；
- `file://...?id=` 本地不可达；
- vendor snapshot 缺页或 sigil。

每次生产代码变更都必须同时跑正向 Golden replay 和上述 mutation；仅“当前 P003 页面还能打开”不算回归通过。

---

## 7. 以 P003 为 Golden 的标准生产工作流

当前 `wiki-node-production@5` 已有 17 个阶段。建议保留其主干，同时补上 Golden preflight、失败分类、浏览器验收和有限自动返工，形成下面的标准流。

### 阶段 0：任务与 Golden 合同冻结

输入：`industry + node_id + target_policy`。  
动作：冻结 graph release、node dossier、正反例、内容问题清单、表格字段合同、地域策略和发布目标。  
Gate：节点存在、spine hash 匹配、Golden policy 可解析、生产快照资产闭包完整。  
失败路由：配置错误直接停止，不调用 Agent。

### 阶段 1：Plan / Prepare

必须先执行标准 plan，决定 `rebuild` 或 `audit`，然后生成不可变 node dossier。旧 wiki-v1、缺十节或含模型回忆的页面只能 rebuild。  
输出：manifest、journal、dossier、预算、runtime policy。  
Gate：一个批次内 claim slot、模型、effort、工具权限和预算全部冻结。

### 阶段 2：来源策略与 Research Nomination

Research Agent 不上网，只为每个 requirement 提名“要证明什么、应去哪类来源找、locator 是什么”。来源计划按用途分槽：

- 法规/标准：定义、适用范围、排除；
- 厂商规格/指南：形态、配置和接口；
- PCF/EPD/PCR：参考配置、边界、生命周期和质量指标；
- 中国官方厂商资料：CN 参数与地域代表；
- 内部图谱：A039、上下游和系统边界；
- 建模判断：分配、代理、失效条件，不伪装成外部事实。

Gate：claim 数恰好符合 slot 合同；主语为目标节点；不得把 requirement 名称改写成事实；相邻对象只能列为反例或 ADJACENT 候选。

### 阶段 3：确定性 Search / Fetch

脚本根据 frozen source plan 搜索、抓取和缓存，不允许 Agent 自由浏览后直接写正文。  
输出：每个 claim 的候选 URL、payload hash、excerpt、locator、content type、region 和 source identity。  
Gate：SSRF/本地地址阻断、预算、hash、excerpt 可重提取、URL canonicalization、来源类型和地域槽位完整。

### 阶段 4：独立 Verify

只读 reviewer 在禁网环境中比较 `claim + node_identity + frozen excerpt`。  
规则：只有 `CONFIRMED + EXACT + quote substring` 才能支持外部事实；ADJACENT、INSUFFICIENT、CONTRADICTED 不得进入正文事实层。  
Gate：核心 slot 全确认、至少两家独立 authority、要求中国参考时 CN source slot 满足。

自动返工：

- `ADJACENT` → 回到 claim 主语/来源路由，不重写 Verify；
- `INSUFFICIENT` → 缩小 claim 或换 locator；
- `NOT_FOUND` → 切换来源类型或记录显式 gap；
- protocol/runtime/hash 错误 → 同输入重试 launcher；
- 最多两次同类重试，仍失败则进入人工/政策队列，禁止无限循环。

### 阶段 5：Freeze

冻结 nomination、Search/Fetch、Verify、usage 和 invocation。此后内容层只能引用裁决，不能修改 claim 文本、quote、verdict 或 node alignment。  
Gate：claim 集合和 runtime attestation 全部 hash 闭合。

### 阶段 6A：段落级 Content Compose

先做 section plan，再写 paragraph plan，最后生成句子。禁止按 claim 顺序逐条贴正文。每段必须声明：

- 本段回答的问题；
- thesis；
- 使用哪些 evidence claim；
- 要给出的 modeling implication；
- 适用条件、失效条件或证据缺口；
- 与前后段的关系。

外部事实优先通过 editorial fusion 合并。例如 P003-4 与 P003-5 应写成一个连贯事实：“刀片服务器是一种高密度独立服务器设备，其基本构成至少包括处理器和系统内存。”随后再解释这里的“独立”不意味着可脱离刀片机箱运行。

### 阶段 6B：独立 Editorial Review

Reviewer 不检查来源真伪，而检查文章质量：

- 句间是否存在解释、因果、限定或转折关系；
- 是否重复同源事实；
- 是否混淆本体、组件、共享基础设施和活动；
- 是否把可追溯写成实测；
- 是否把代理可用写成数据完整；
- 每节是否真正回答 Golden 问题；
- 是否出现为了凑计数而增加的重复判断。

NO_GO 只返回结构化 paragraph delta；Compose Agent 只能修改指定段落。两轮仍 NO_GO 时停止自动改写并升级，不允许生成 round12、round13 式无限节点专用脚本。

### 阶段 7：Draft Content Gate 与 Draft Apply

Draft Gate 同时检查：研究 claim 守恒、内容 claim 守恒、章节问题覆盖、段落连贯度、重复率、字数下限、标签禁用、引用解析、非退化和节点身份。  
只有 staged candidate 通过后才进行 hash-locked draft Apply；失败必须记录 `blocked_before_content_apply`，正式页不变。

### 阶段 8A：Table Collect / Verify

表格证据与正文证据并行但分流管理。Collector 按字段合同采集值；Verifier 核对单位、配置、年份、地域、定义/代理/实测身份和来源原文。  
同一数值不得因展示在两个地域轨而复制 source identity；公开参考不得写成项目实测。

### 阶段 8B：Table Population Gate / Apply

硬检查：

- 节点专属行数与必填字段；
- 非空值必须有 source、basis、pedigree；
- gap 不计入 populated；
- INT/CN source region 匹配；
- 中国任务启用 `params_cn_population_floor`；
- 低于下限只能发布为明确的 `partial/reference_configuration_only`，不能宣称 dataset ready。

通过后单独 hash-locked Apply，防止表格返工改坏已审正文。

### 阶段 9：Preview 与浏览器验收

Preview 必须在 production 同构的完整行业快照上运行：Graph、Wiki lint、bundle、viewer、name graph、overlay 全部 PASS。浏览器 smoke test 至少检查：

- `ict_equipment-wiki-P003.html` 可直接打开；
- release ID 和正文 hash 与批次一致；
- 正文无内部标签；
- 同源卡片已合并；
- 每个外部来源链接可打开或下载；
- 表格覆盖计数把值与缺口分开；
- 页面截图与 Golden 视觉规则无明显退化。

### 阶段 10：Release Gate / Reviewed Apply / Publish

G7 汇总证据、内容、表格、浏览器和行业完整性；G8 将 reviewed Apply 绑定 coverage hash、table gate hash 和旧目标 hash；最后同时构建 bundle 与 viewer。  
只有 release journal 为 `published`，且所有 Gate PASS，才可对外称“正式发布”。

### 阶段 11：发布后差异与持续学习

发布报告自动生成：正文增删、来源增删、表格覆盖变化、地域覆盖、缺口变化、页面和 bundle hash。任何用户发现的逃逸错误必须：

1. 形成最小 defect fixture；
2. 证明旧 Gate 会放过；
3. 修改通用合同/检查器；
4. 加 mutation；
5. 对同版本、同脚本、同 prompt 产物做追溯扫描；
6. 再修当前 P003 页面。

顺序不能反过来；只修当前页面不算完成。

---

## 8. Wiki 生产任务如何调用 Skills、Agents、脚本与 Workflow

这一节描述一次真实 Wiki 任务在项目中的执行机制。最重要的原则是：**Skill 只负责识别意图和选择流程，Workflow 负责规定顺序，Agent 只提交候选或裁决，脚本负责确定性执行和 Gate，Kernel 负责状态、记忆和发布。**任何一层都不能越权替代另一层。

### 8.1 六层执行架构

| 层 | 项目资产 | 负责什么 | 明确不负责什么 |
|---|---|---|---|
| 意图与方法层 | `skills/generate-node-wiki/SKILL.md` | 识别“生成/补全/修复节点 Wiki”，收集行业和节点 ID，路由到版本化 Workflow | 不直接写 Wiki，不保存运行状态，不宣布 Gate PASS |
| 编排层 | `workflows/wiki-node-production@5.json` | 定义 17 个阶段、依赖关系、Agent/脚本 capability、Gate 和输出协议 | 不生成正文，不直接修改正式文件 |
| Agent 层 | `agents/researcher`、`agents/reviewer`、`agents/repairer` | 生成研究提名、内容候选、语义裁决和最小修复建议 | 无 Web、无正式写权限、不能自审、不能发布 |
| 确定性执行层 | `vendor/lca_cornerstone/scripts/wiki_*.py`、`run_wiki_*.py` | 节点提取、Search/Fetch、schema 校验、coverage、Gate、bundle 和 viewer 构建 | 不凭模型记忆补事实，不改变 Workflow 政策 |
| 状态与记忆层 | SQLite、CAS、Event Ledger、batch journal、registry | 保存 Job/Run/Stage、不可变 Artifact、来源、hash、事件、失败和恢复点 | 不把聊天上下文当生产记忆，不允许静默覆盖历史裁决 |
| 发布层 | `release.apply`、`src/lca_project/kernel/release.py` | staging、hash lock、原子 Apply、回滚、reviewed 升级和 publish receipt | Agent 不可直接调用，Gate 不全时不可写正式目标 |

这六层的调用关系是：

```text
用户意图
  → generate-node-wiki Skill
    → wiki-node-production@5 Workflow
      → Control Plane 创建 Job / Run
        → Agent 生成只读候选
        → 脚本冻结证据并执行 Gate
        → CAS / SQLite / Event Ledger 保存证明
        → Release Manager 执行 hash-locked Apply
        → bundle + viewer + release journal
```

### 8.2 Skill 在生产中的作用

新项目的 `skills/generate-node-wiki/SKILL.md` 是一个薄入口：

- 捕获“生成、补全、重建、修复、Golden 对齐、批量 Wiki”等任务意图；
- 只收集 `industry`、`node_id` 和任务目标；
- 固定路由到 `workflow://wiki-node-production@5`；
- 不携带正文，不直接执行 Apply，也不把对话内容当作已核实证据。

完整的方法学约束来自 vendored `generate-node-wiki` 生产规范，包括：外部事实、内部图谱事实、建模判断和证据缺口分层；Product/Activity 内容合同；EXACT 节点对齐；Golden 非退化；数量防火墙；引用与证据表要求。新项目把这些规范落实为 Workflow、schema、脚本和 Gate，而不是要求主 Agent 靠记忆遵守。

其他 Skill 不嵌入正文生产主链，但形成上下游关系：

- `industry-graph` 先提供已发布的节点身份、刻面和边；Wiki 不得反向修改图谱；
- `cross-link-binding` 在 Wiki 之外处理跨行业 GPID 绑定；
- `bom-skeleton-probe` 用真实 BOM 检验图谱完整性，不为 Wiki 虚构型号数据；
- 当 graph release 变化时，影响分析应把依赖它的 Wiki 标为 stale，再重新触发 `generate-node-wiki`。

因此，Skill 是“任务入口与政策选择器”，不是一个会自己完成全部工作的超级 Prompt。

### 8.3 Agent 如何分工并受到约束

| Agent/逻辑角色 | 配置 | 输入 | 输出 | 权限边界 |
|---|---|---|---|---|
| Researcher | Terra / medium | 冻结 node dossier、claim slots、来源策略 | schema-valid nomination 或 table collection proposal | `artifact:read`，network deny，禁止写权威文件和自审 |
| Content Composer | `agent.propose` 的内容任务包 | frozen Verify、Content Blueprint | `wiki-content-draft-v2`，按 section/paragraph/sentence 组织 | 无 Web；不能修改 Verify verdict、quote 或 node identity |
| Independent Reviewer | Sol / medium | 冻结 dossier、evidence、candidate | `wiki-verdict-v1` 或 `wiki-editorial-review-v1` | 只读、无 Web、无 Apply；必须给出 rule 与 evidence hash |
| Table Verifier | `agent.review` 的表格任务包 | 值、单位、地域、配置、来源摘录 | source/value verdict | 不能把 proxy 改成 measured，不能把 INT 来源批准到 CN 轨 |
| Repairer | Terra / medium | 结构化失败 envelope | 最小 repair proposal、应回退阶段、预计验证 capability | 不直接修改页面；不能重新解释已冻结 Gate |

Agent 输出必须满足三个条件才会被 Control Plane 接受：

1. 绑定当前阶段输入 Artifact hash；
2. 符合阶段输出 schema；
3. 带已注册 Agent 配置、Prompt hash、模型、effort、工具权限和 usage 的 attestation。

Agent 不能改变 Workflow 状态。它只能把候选提交给确定性控制器；真正的 `research_ready → verified → frozen` 迁移由控制器在 schema、hash 和 Gate 通过后完成。这样即使 Agent 自称“已核实”或“可以发布”，系统也不会据此升级状态。

### 8.4 Workflow 如何成为唯一状态机

`workflows/wiki-node-production@5.json` 把一次 Wiki 任务拆为 17 个有依赖的阶段：

| 序号 | Workflow step | Capability | 关键输入/输出 | 放行条件 |
|---:|---|---|---|---|
| 1 | `plan` | `wiki.batch` | node IDs → `wiki-plan-v5` | 节点存在、任务模式和预算冻结 |
| 2 | `prepare` | `wiki.batch` | plan → `node-dossier-v5` | spine、刻面、边界、旧页基线完整 |
| 3 | `research_ready` | `agent.propose` | dossier → `wiki-proposal-v1` | G0 运行证明、slot schema |
| 4 | `verify` | `agent.review` | frozen evidence → `wiki-verdict-v1` | G4：EXACT、quote、来源身份 |
| 5 | `freeze` | `wiki.batch` | proposal + verdict → attestation | claim 集合与 runtime hash 闭合 |
| 6 | `content_compose` | `agent.propose` | Verify + Blueprint → content draft | 不改变研究事实，段落 schema 合格 |
| 7 | `editorial_review` | `agent.review` | content draft → editorial verdict | G5：连贯性、边界、重复与表达 |
| 8 | `draft_content_gate` | `wiki.batch` | staged content → Gate report | G6：结构、深度、coverage、非退化 |
| 9 | `draft_apply` | `release.apply` | passed draft → `wiki-draft-v5` | hash-locked draft transaction |
| 10 | `table_collect` | `agent.propose` | table contract + sources → table evidence | 值、配置、地域和来源字段齐全 |
| 11 | `table_verify` | `agent.review` | table evidence → source verdict | G6T：数值与来源原文相符 |
| 12 | `table_population_gate` | `wiki.batch` | verified table candidate | G6P：人口下限、pedigree、地域分轨 |
| 13 | `table_apply` | `release.apply` | passed tables → table apply receipt | 与正文分离的 hash-locked Apply |
| 14 | `preview` | `wiki.batch` | draft + tables → preview | 完整行业快照六步构建 |
| 15 | `release_gate` | `wiki.batch` | preview + 全部证明 | G7：Graph、Wiki、coverage、browser |
| 16 | `reviewed_apply` | `release.apply` | release certificate | G8：旧目标 hash 与全部 Gate hash 绑定 |
| 17 | `publish` | `release.apply` | reviewed artifact | bundle、viewer、post-verify、release journal |

Workflow 的 `needs` 字段保证阶段不能越级。例如 `reviewed_apply` 依赖 `release_gate`，而 `release_gate` 依赖 `preview`；即使正文已经生成，也不可能跳过表格 Gate 和浏览器预览直接发布。

### 8.5 各类脚本在每一步做什么

#### 控制入口与状态推进

`vendor/lca_cornerstone/scripts/wiki_batch.py` 是唯一批次控制入口，提供：

```text
plan → prepare → validate → research-ready → verify → finalize
     → apply(draft) → preview → go-no-go → gate
     → apply(reviewed) → publish
```

它负责 journal 合法迁移、resume、claim 集合守恒、Gate 绑定和事务边界。`blocked` 或 `failed` 只有显式 `--resume` 才能继续，防止脚本意外把失败批次当成新输入。

#### 研究和证据冻结

- `run_wiki_nomination_capture.py`：用动态 schema 启动 Researcher，冻结 invocation、events、stderr、result 和 usage；
- `wiki_source_discovery.py`：根据来源身份与 locator 确定性 Search/Fetch，执行 URL 安全检查、缓存、payload hash 和 excerpt 提取；
- `run_wiki_verify_capture.py`：启动禁网 Reviewer，并冻结 Verify 的全部运行证据；
- `wiki_verify_compose.py`：把 claim、fetch result 与 verdict 组合成可审计 Verify Artifact；
- `wiki_research_ready.py`：校验核心 external slot、authority 和节点对齐是否达到研究完成条件。

这里 Agent 不直接使用 Web。Researcher 只说“应查什么来源”，Search/Fetch 由脚本执行；Reviewer 只看已冻结原文。这避免搜索 Agent 一边找资料、一边选择性摘录、一边批准自己的结论。

#### 正文生成和编辑

- `run_wiki_content_capture.py`：按 Content Blueprint 生成结构化内容；
- `run_wiki_content_sectioned_capture.py`：章节隔离生成，降低长文串扰；
- `run_wiki_editorial_review_capture.py` / `run_wiki_editorial_sectioned_review.py`：独立检查段落连贯、边界和语义；
- `run_wiki_content_editorial_loop.py`：在限定次数内按结构化问题返工；
- `wiki_content_enrich.py`：确定性地把验证过的段落装配成 BODY、KUs 和证据表骨架；
- `wiki_draft_content_gate.py`：在写入前检查章节、深度、来源、重复、claim coverage 和 Golden 非退化。

内容 Agent 输出的是结构化段落，不直接编辑 Markdown。`wiki_content_enrich.py` 才负责把已通过审查的内容转换为页面候选，从而使“模型写了什么”和“工程最终写入什么”可对账。

#### 表格数据生产

- `wiki_table_population.py`：读取冻结 reference configuration 和来源绑定数据，生成三张表候选；
- Table Verifier 检查数值、单位、年份、地域和 pedigree；
- Table Population Gate 检查 props、INT params、CN params 和 quality 的人口下限；
- 通过后使用独立事务写表，不重新生成正文。

正文流与数据表流分离很关键：法规和规格事实可以支撑正文身份说明，但不能自动变成定量表值；反过来，某型号 PCF 的数值也不能无条件代表整个 P003 产品族。

#### Coverage、质量和发布

- `wiki_claim_coverage.py`：检查研究 claim、内容 claim、引用和页面状态的双向集合守恒；
- `wiki_quality_contract.py`：定义 Product/Activity 固定内容合同；
- `wiki_lint.py`：执行引用、表 schema、地域、数值 pedigree、章节和 reviewed 状态等确定性检查；
- `build_wiki_bundle.py`：把已发布 Wiki 与 registry 构建为数据包；
- `build_wiki_viewer.py`：构建行业 viewer 和无需 query 的节点直达 HTML；
- `src/lca_project/kernel/release.py`：执行 hash-locked staging、apply 和 rollback。

### 8.6 项目的“Memory”具体是什么

自治生产不能依赖 Agent 在几十轮对话中“记得上次发生了什么”。项目把记忆拆成可验证的持久层：

| 记忆类型 | 位置 | 用途 |
|---|---|---|
| 控制状态 | `var/state.db` | Job、Run、Stage、Gate、exception、release 的 SQLite 投影 |
| 不可变内容 | `var/artifacts/` | 以 SHA-256 寻址的 dossier、proposal、evidence、verdict、Gate 和 receipt |
| 事件历史 | Event Ledger | 追加式记录每次状态变化，可重建状态投影 |
| 批次记忆 | `var/workspaces/.../journal.json` | Wiki 专用 planned → published 状态和 resume 点 |
| 来源记忆 | Wiki source registry、冻结 payload | source identity、URL、locator、excerpt 和内容 hash |
| 方法记忆 | Skills、Workflow、schema、policy、tests | “应该怎么做”和“过去哪些错误不能再发生” |
| Golden/缺陷记忆 | Golden profiles、`tests/wiki_phase2/`、`defects/wiki/` | 正向基线、历史事故和 mutation |

聊天记录和模型上下文只能帮助理解任务，不能成为 production evidence。任何要跨轮复用的信息都必须进入上述某类 Artifact，并带来源、schema、版本和 hash。

### 8.7 一次新节点任务的实际调用顺序

当用户提出“为 `ict_equipment::Pxxx` 生成 Wiki”时，平台应按以下方式运行：

1. **Skill 路由**：`generate-node-wiki` 识别任务，选择 `wiki-node-production@5`，提交行业、节点和 policy。
2. **Control Plane 建 Job**：生成 idempotency key，冻结 dossier 输入；同输入重试返回同一 Job，不重复生产。
3. **批次规划**：调用 `wiki_batch.py plan <industry> --nodes <ID>`，判断 rebuild/audit，创建 manifest 和 journal。
4. **准备节点档案**：`prepare` 从本地完整快照提取单节点身份、刻面、边、旧页和基线指标，禁止读取/写入原项目运行态。
5. **Researcher 提名**：launcher 生成严格 slot 化的 claim proposal；模型只读、无 Web、无 Apply。
6. **Search/Fetch**：脚本按来源策略获取候选，冻结 URL、payload、excerpt、locator 和 hash。
7. **Reviewer 核验**：禁网 Reviewer 对每条 claim 判 EXACT/ADJACENT 和 CONFIRMED/INSUFFICIENT 等；失败由 Repairer 映射到 claim、locator 或来源路由返工。
8. **Freeze**：控制器冻结研究证据链，内容阶段不再允许改变事实裁决。
9. **Content Compose**：Content Agent 先做章节与段落计划，再生成句子；外部事实、建模解释、边界和缺口分层。
10. **Editorial Review**：独立 Reviewer 按段落检查连贯性和语义；最多两轮定点返工。
11. **Draft Gate / Apply**：脚本验证 Golden、coverage、引用和非退化；通过后只写 draft。
12. **Table Collect / Verify / Gate**：独立采集定量证据，核对地域和 pedigree，达到人口下限后单独 Apply。
13. **Preview**：在完整行业快照构建 Graph、bundle、viewer 和 overlay，运行 lint 与浏览器 smoke test。
14. **Release**：G7/G8 汇总全部 signed receipts，Release Manager 原子写 reviewed 页面并同时构建 bundle 与 viewer。
15. **学习**：发布差异、失败 envelope 和用户发现的问题进入 defect/mutation；下一节点从新的 policy 版本开始受益。

### 8.8 一个可审计批次会留下哪些产物

一次成功生产至少留下：

- `manifest.json`、`journal.json`、`prepared.json`；
- nomination invocation/events/stderr/result/usage；
- `source-queue.json`、`source-evidence.json`、冻结 payload hash；
- verify invocation/events/stderr/verdict/usage；
- `frozen.json`、Content Blueprint、content result、editorial review；
- `draft-content-gate.json`、`coverage.json`；
- table collection、table source verdict、table population gate；
- draft/table/reviewed apply transaction receipts；
- `preview-report.json`、`quality-gate.json`、`gate-report.json`；
- `publish-report.json`、`release-journal.json`；
- 正式 Markdown、source registry、bundle、viewer 和各自 SHA-256。

如果缺少其中任一阶段所要求的 Artifact，系统只能报告停在哪一层，不能用一句“Agent 已完成”代替生产证明。

### 8.9 各组件之间的禁止性边界

- Skill 不直接写页面；
- Agent 不执行 Search/Fetch，不改变状态，不 Apply，不 publish；
- Reviewer 不修改候选，也不审核自己的输出；
- 脚本不把模型记忆写成外部事实；
- Content Compose 不修改 Verify verdict；
- Wiki 不反向修改 graph spine；
- preview 不升级 reviewed；
- table gap 不计作 populated value；
- Release Manager 不接受未绑定当前 candidate hash 的旧 Gate；
- 对话中的人工修改不能绕过 journal 和 hash-locked transaction。

这些边界共同实现“Agent 可以犯错，但错误不能无证明地进入正式 Wiki”。

---

## 9. 自动发现与自主修复的控制表

| 失败类别 | 判定者 | 自动动作 | 最大自动次数 | 不允许的动作 |
|---|---|---|---:|---|
| Runtime 超时/缺 protocol | launcher | 原输入幂等重试，切 HTTP fallback | 2 | 修改输出 JSON 冒充成功 |
| Claim 基数/字段漂移 | schema Gate | 退回 Nomination，动态 schema 重生 | 2 | 在 finalize 手工删 claim |
| ADJACENT | Verify | 退回来源路由或缩小 claim 主语 | 2 | 把 alignment 手填为 EXACT |
| INSUFFICIENT | Verify | 更换 locator、缩小断言 | 2 | 用建模判断包装成外部事实 |
| 单一 authority | Quality Gate | 补另一来源类型 | 1 | 把同域不同 URL 计作独立来源 |
| Content 改写 verdict | Freeze Gate | 丢弃 content candidate，重新 compose | 1 | 更新 frozen Verify 配合正文 |
| 段落拼接/重复 | Editorial Gate | 只返工问题段落并重新融合引用 | 2 | P003 专用逐句硬编码无限追加 |
| 深度退化 | Golden Gate | 回到 section/paragraph plan | 2 | 仅重复句子或增加标签凑数 |
| 表格空壳 | Population Gate | 回到字段来源采集 | 1 个来源路由周期 | 把缺口文本计作 populated |
| 地域错轨 | Region lint | 清空错轨值并重新找本地来源 | 1 | 用国际代理冒充中国值 |
| Preview 资产缺失 | Snapshot Gate | 重建完整只读 workspace | 1 | 在冻结批次内临时复制未声明资产 |
| Viewer/链接失败 | Browser Gate | 重建 viewer、刷新 bundle hash、重测链接 | 2 | 只报告 Markdown 正确 |
| Apply/Publish 中断 | transaction manager | 回滚或幂等 resume | 2 | 绕过 journal 直接覆盖正式文件 |

自治不是“永远重试直到 PASS”。自治的正确含义是：系统能识别错误类别、选择正确回退阶段、限制重试预算、保存失败证据，并在超出能力边界时诚实停止。

---

## 10. 新节点的一次标准运行应达到的准入/退出条件

### 运行前准入

- 图谱节点已发布且 spine hash 冻结；
- node dossier 有正反例和邻接对象；
- Product/Activity 对应内容合同与表格合同存在；
- 来源策略包含法规、规格、环境资料和目标地域槽位；
- 完整行业 vendor snapshot 已校验；
- Golden profile 与预算已冻结。

### 正文退出

- 核心 external slots 全部 EXACT/CONFIRMED；
- 两家以上真正独立 authority；
- 研究 claim 和内容 claim 双向守恒；
- 九节问题覆盖、段落 reviewer GO；
- 无可见内部标签、无重复来源展示；
- 内容与上一个 Golden 相比无未经批准的语义退化。

### 表格退出

- 字段集节点专属；
- 值、source、basis、pedigree、region 成对完整；
- INT/CN 分轨和下限满足任务政策；
- measured/defined/proxy/gap 不混淆；
- 数据集 readiness 与真实覆盖一致。

### 发布退出

- Draft Gate、Table Gate、Preview、G7、G8 全部 PASS；
- Graph 11/11、Wiki lint 31/31；
- 浏览器可打开直达文件并完成来源链接 smoke test；
- bundle、viewer、registry、page、journal hash 闭合；
- release journal=`published`；
- 自动生成版本差异和剩余缺口说明。

---

## 11. 人如何逐步退出

P003 应先作为人工确认过的 Golden 和 mutation 基线，而不是长期要求人逐段重写。建议采用三级自治：

1. **Shadow**：新节点全自动跑到 preview，与人工结果比较；人只判最终内容和错误类型，不直接改文件。
2. **Supervised publish**：连续批次达到证据准确率、编辑通过率、链接成功率和缺陷逃逸率门槛后，人只批准 release certificate。
3. **Policy-bounded autonomous publish**：成熟节点类型在预算、来源类型和风险范围内自动发布；只有身份冲突、证据矛盾、地域下限无法满足或两轮编辑不收敛时才升级人工。

人退出的前提不是 Agent 不再犯错，而是错误大部分能在发布前被独立 Gate 发现，且系统知道应退回哪个阶段修复。最终人工角色从“对话驱动每一步”变为“制定政策、维护 Golden、处理少量新型异常和抽样审计”。

---

## 12. 后续工程行动优先级

### P0：在跑下一个节点前完成

1. 把 v30 P003 拆成 G-A 至 G-G 的正式 Golden fixture 和 release certificate。
2. 将 11 个 `curate_p003_editorial_repair_round*.py` 提炼成通用 Editorial Contract 与 defect fixtures；从标准生产路径移除节点专用修补。
3. 统一 failure envelope：所有 prepared/research_ready 中止都必须记录 `stage/error_class/retryable/input_hash/output_hash/message`。
4. 把浏览器直达、同源合并、外链点击、内部标签隐藏、值/缺口计数加入 G7。
5. 给 Table Population policy 增加按节点类型和地域目标配置的字段/覆盖阈值。

### P1：扩展到下一批节点时完成

1. 建立中文厂商、产品族、官方域名、文档类型和搜索术语路由表。
2. 选择 A017、P031 和一个新 Product 节点做 shadow，验证 P003 经验是否真正可迁移。
3. 采集每阶段一次通过率、返工次数、失败类别、token/时间/网络预算和人工升级率。
4. 设定同类错误最多两轮的自动修复上限，防止“几十轮但没有新增控制能力”。

### P2：自治晋级前完成

1. Golden replay + defect mutation 全链离线运行；
2. 对历史同 prompt/同脚本页面执行追溯扫描；
3. 定义抽样审计、自动回滚、来源过期和 graph spine 变化后的 stale 重建策略；
4. 连续多个批次达到自治发布 SLO 后，再逐步撤出人工发布批准。

---

## 13. 最终结论

P003 的几十轮尝试有一部分是建设生产基础设施的必要成本：没有这些回合，就不会有节点身份 EXACT、运行证明、事务发布、完整快照和可回放 Gate。但也有明显可以避免的返工：Golden 完成合同定义过晚、研究 claim 直接驱动正文、用数量指标替代编辑质量、表格人口与地域策略后置，以及 P003 专用硬编码修补没有及时上升为通用规则。

如果 P003 真正成为 Golden Case，下一节点的标准不应是“照着 P003 的句子再写一篇”，而应是：

> 用 P003 固化的身份测试、证据槽位、段落合同、表格语义、阅读器验收、缺陷 mutation 和自动返工路由，证明新节点能够在有限轮次内完成同等级生产闭环。

理想目标不是一次模型调用就正确，而是一次标准工作流内完成：正常路径一轮；同类可恢复错误最多两轮；超限后准确停止并给出机器可执行的缺口任务。只有这样，“几十轮对话把一个节点磨出来”才会真正转化为“平台自主生产整个骨架数据库”。

---

## 附录 A：关键产物

- Phase 2 报告：`docs/Phase2-Wiki垂直切片验收报告.md`
- Phase 3 报告：`docs/Phase3-Wiki真实证据正式发布验收报告.md`
- v27 Golden 重跑报告：`docs/P003-Golden内容优先重跑验收报告.md`
- 当前工作流：`workflows/wiki-node-production@5.json`
- P003 Content Blueprint：`vendor/lca_cornerstone/fixtures/wiki-phase2/content-blueprints/P003.json`
- Reader 回归：`tests/wiki_phase2/test_wiki_reader_presentation.py`
- Content/连贯性回归：`tests/wiki_phase2/test_content_golden_pipeline.py`
- Table Population 回归：`tests/wiki_phase2/test_wiki_table_population.py`
- 历史缺陷回归：`tests/wiki_phase2/test_wiki_defect_corpus.py`
- v26 首次正式发布：`var/workspaces/wiki-production-20260810-v26/runs/wiki-batches/ict_equipment/production-p003-v26/`
- v28 编辑后发布：`var/workspaces/wiki-production-20260811-v28/runs/wiki-batches/ict_equipment/production-p003-v28-batch/`
- v29 表格发布：`var/workspaces/wiki-production-20260811-v29/runs/wiki-batches/ict_equipment/production-p003-v29-table-data/release-journal.json`
- v30 中国参考发布：`var/workspaces/wiki-production-20260811-v30/runs/wiki-batches/ict_equipment/production-p003-v30-cn-reference/release-journal.json`
- v30 P003 查看器：`var/workspaces/wiki-production-20260811-v30/docs/ict_equipment-wiki-P003.html`
