# P003 Golden 内容优先重跑验收报告

日期：2026-08-10  
生产工作区：`var/workspaces/wiki-production-20260810-v27`  
批次：`runs/wiki-batches/ict_equipment/production-p003-v27`

## 结论

P003 已按“真实证据锚点 + Content Blueprint + 受控建模判断 + 双向 claim coverage”重新生产并发布。批次 journal 最终状态为 `published`；正式 Wiki bundle 与 viewer 均构建成功。

## 核心变化

- Research/Verify 仍冻结 30 条研究 claim，不以增加正文深度为由放松外部证据裁决。
- 新增 Content Architect 阶段：必须逐字、逐节、恰好一次容纳 30 条研究 claim；新增知识只能是 `modeling_judgment` 或 `evidence_gap`。
- 新增 P003 Content Blueprint：冻结九节主题、正文/断言/段落/建模深度阈值、禁止短语和 30 行节点专属证据表标签。
- coverage 允许带 `P003-Cnnn` 标识的受控内容 claim，但研究 claim 仍必须保持原冻结数量，外部事实不得通过内容阶段追加。
- Content Apply 支持 hash-locked remediation；失败修复使用新计划和新事务，不允许手改页面绕过 journal。

## 自主发现与修复记录

1. 首次 Nomination 生成了会随核验状态失真的缺口句；语义约束将缺口限定为长期缺失的型号级 BOM、净质量、制造、运输、共享资源分配和代表性数据，然后重跑通过。
2. 首次 Content 输出结构合格但纯句子正文仅 5,462 字，低于 6,500 字阈值；Draft Content Gate 在 Apply 前拒绝。加入长度缓冲和“对象—字段—适用条件—失效条件”约束后重跑，纯句子正文达到 7,110 字。
3. 首次 Preview 发现数据质量表占位词 `待采/待核` 不符合词表，wiki lint 仅 28/30；重新蒸馏为 `待核`，生成第二个 hash-locked 合并计划并执行 remediation，Preview 修复为 30/30。

## v26 / v27 对比

| 指标 | v26 | v27 |
|---|---:|---:|
| BODY 字符数（含溯源标记） | 4,453 | 13,476 |
| 正文断言 | 30 | 135 |
| 连贯段落 | 30 个单句段 | 27 个多句段 |
| 建模判断 | 18 | 111 |
| 外部 CONFIRMED | 10 | 10 |
| 错误的笼统“无节点证据” | 1 | 0 |
| 产品性质 / 参数 / 质量表行 | 4 / 6 / 5 | 8 / 12 / 10 |

## 验收结果

- 研究：9 次冻结官方查询、2 次网络抓取、10 条外部事实全部 `CONFIRMED`。
- 内容：135 条 claim 全部双向映射；10 条 external confirmed，125 条 controlled internal。
- coverage：`135/135`，coverage 与 quote compliance 均为 `1.0`；unresolved、contradicted、manual review、hash drift 均为 `0`。
- 图谱：`validate_graph` 11/11。
- Wiki：Preview 30/30；coverage-aware lint 31/31。
- 数据集关联：11/11；节点搜索矩阵：6/6。
- 项目回归：`89 passed`；`make validate` 为 `status: pass`。

## 主要产物

- 正式查看器：`var/workspaces/wiki-production-20260810-v27/docs/ict_equipment-wiki.html`
- 正式页面：`var/workspaces/wiki-production-20260810-v27/wiki/ict_equipment/products/P003--服务器-通用计算-刀片式.md`
- Golden Gate：`var/workspaces/wiki-production-20260810-v27/runs/wiki-batches/ict_equipment/production-p003-v27/draft-content-gate.json`
- Coverage：`var/workspaces/wiki-production-20260810-v27/runs/wiki-batches/ict_equipment/production-p003-v27/coverage.json`
- 正式 Gate：`var/workspaces/wiki-production-20260810-v27/runs/wiki-batches/ict_equipment/production-p003-v27/gate-report.json`
- 发布报告：`var/workspaces/wiki-production-20260810-v27/runs/wiki-batches/ict_equipment/production-p003-v27/publish-report.json`
