# 项目文档中心

这里是 `lca-project` 的统一文档入口。优先使用 [HTML 文档中心](index.html) 浏览；
需要审阅、引用或修改正文时，使用对应 Markdown 源文件。

> 维护规则：Markdown 是正文的权威来源，同名 HTML 是阅读镜像。修改 Markdown 后运行
> `python scripts/build_docs_site.py`；手工设计的技术设计、测试设计与 P003 全流程 HTML 不会被覆盖。

## 01 · 架构与治理

平台为什么这样设计，以及如何验证设计没有被工作流局部规则架空。

| 文档 | 阅读入口 | 源文件 | 说明 |
|---|---|---|---|
| 骨架数据库自治生产平台 · 技术设计 | [HTML](技术设计-骨架数据库自治生产平台.html) | [HTML](技术设计-骨架数据库自治生产平台.html) | 平台控制面、能力边界、状态机、发布事务和工程重构的权威技术设计。 |
| 系统自我修复与目标对齐架构 | [HTML](系统自我修复与目标对齐架构.html) | [Markdown](系统自我修复与目标对齐架构.md) | Goal Contract、三层闭环、Meta Supervisor、复合动作图与效果证明合同。 |
| Goal Contract Governance v2 | [HTML](goal-contract-governance-v2.html) | [Markdown](goal-contract-governance-v2.md) | 三合同、Capability Envelope、Goal 修正案、自治资格与受治理发布的实现边界。 |
| 研究约束治理重设计 v1 | [HTML](research-constraint-governance-redesign-v1.html) | [HTML](research-constraint-governance-redesign-v1.html) | 区分阻断契约与质量目标，并以稳定问题契约和逐问题证据闭合替代关键词启发式。 |
| Job 跨阶段状态一致性与自主收敛设计 v1 | [HTML](job-execution-consistency-autonomous-convergence-v1.html) | [Markdown](job-execution-consistency-autonomous-convergence-v1.md) | 定义 Job/Run/Task/Item/Campaign 原子恢复、Artifact 代际、唯一 Repair Graph、差分 Canary 与最终发布收尾。 |
| 自治修复的受控 SCM 发布 | [HTML](system-repair-scm.html) | [Markdown](system-repair-scm.md) | 偏差 Issue、隔离分支、可追踪 commit、Draft PR、失败降级与基线一致性保护。 |
| 自治生产平台 · 测试策略与测试用例 | [HTML](测试设计-骨架数据库自治生产平台.html) | [HTML](测试设计-骨架数据库自治生产平台.html) | 测试矩阵、优先级、Mutation、Golden、Shadow、Canary 与验收证据要求。 |

## 02 · 实施与运行

当前实现边界、长任务治理、上线状态和剩余准入条件。

| 文档 | 阅读入口 | 源文件 | 说明 |
|---|---|---|---|
| 重构实施与验收状态 | [HTML](重构实施与验收状态.html) | [Markdown](重构实施与验收状态.md) | 已完成能力、可复现测试证据、尚未通过的验收范围和下一里程碑。 |
| A001 Wiki 生产长任务优化与修复方案 | [HTML](A001-Wiki生产长任务优化与修复方案.html) | [Markdown](A001-Wiki生产长任务优化与修复方案.md) | 长任务基准、状态失真、Provider 性能、重试预算和 Supervisor 治理方案。 |

## 03 · Wiki 生产与验收

从垂直切片到 Golden Case 的可追溯生产证据与复盘。

| 文档 | 阅读入口 | 源文件 | 说明 |
|---|---|---|---|
| P003 节点 Wiki 全流程复盘与 Golden Case 工作流 | [HTML](P003节点Wiki生产全流程复盘与Golden-Case工作流.html) | [Markdown](P003节点Wiki生产全流程复盘与Golden-Case工作流.md) | 从早期 Draft Gate 到高质量 Golden 的完整时间线、失效模式和目标工作流。 |
| P003 Golden 内容优先重跑验收报告 | [HTML](P003-Golden内容优先重跑验收报告.html) | [Markdown](P003-Golden内容优先重跑验收报告.md) | 内容优先策略的 v26/v27 对照、质量指标、修复记录与正式产物。 |
| Phase 2 · Wiki 垂直切片验收报告 | [HTML](Phase2-Wiki垂直切片验收报告.html) | [Markdown](Phase2-Wiki垂直切片验收报告.md) | A017、P031、P003 三节点的隔离演练、Gate 结果和未发布原因。 |
| Phase 3 · Wiki 真实证据正式发布验收报告 | [HTML](Phase3-Wiki真实证据正式发布验收报告.html) | [Markdown](Phase3-Wiki真实证据正式发布验收报告.md) | 真实证据、独立核验、发布门禁、工程资产与源仓库保护的验收记录。 |

## 04 · 机器可读清单

迁移来源、文件哈希和 Phase 2 资产映射。

| 文档 | 阅读入口 | 源文件 | 说明 |
|---|---|---|---|
| 迁移资产清单 | — | [JSON](migration-manifest.json) | 从只读来源迁移到当前项目的资产、来源路径和内容哈希。 |
| Wiki Phase 2 迁移清单 | — | [JSON](wiki-phase2-migration-manifest.json) | Wiki Phase 2 专项资产、来源映射和迁移完整性记录。 |

## 生成与校验

```bash
pip install -e '.[docs]'
python scripts/build_docs_site.py --check
python scripts/build_docs_site.py
```

`--check` 会验证所有人读 Markdown/HTML 是否已进入目录，并检查已生成 HTML 是否与源文件同步。
