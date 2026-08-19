# LCA Autonomy OS / LCA 自治演进系统

> **Goal-Governed Self-Evolving Production System for LCA Knowledge & Data**  
> **面向 LCA 知识与数据的目标契约治理自进化生产系统**

[![Acceptance](https://github.com/RichardCao06/x-project/actions/workflows/acceptance.yml/badge.svg)](https://github.com/RichardCao06/x-project/actions/workflows/acceptance.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Governance](https://img.shields.io/badge/Governance-Goal%20Contract%20v2-006f67)
![Runtime](https://img.shields.io/badge/Release%20Mode-shadow-e2913b)

**中文：** LCA Autonomy OS 不是一个可以任意修改自身的“超级 Agent”，而是一个由 Goal Contract 治理的自治生产与自我演进系统。它把 Agent 当作可替换的认知节点，把 Prompt、工具、Memory、Workflow、重试、Sandbox 与 Reviewer 组织成可演进 Harness，再由持久化控制平面负责目标、状态、证明、权限、发布和回滚。

**English:** LCA Autonomy OS is not an unconstrained “super-agent.” It is a Goal-governed autonomous production and self-evolution system. Agents remain replaceable cognitive nodes; prompts, tools, memory, workflows, retries, sandboxes, and reviewers form evolvable harnesses; the persistent control plane owns goals, state, proof, authority, release, and rollback.

---

## 项目定位 / Project Positioning

| 层级 / Layer | 会改变什么 / What evolves | 在本项目中的职责 / Responsibility here |
|---|---|---|
| **Agent Core / Agent 核心** | 模型、策略或内化技能 / model, policy, internalized skill | 生成候选、语义判断、问题诊断；不直接写入或自我签署 / propose, judge, diagnose; never directly apply or self-certify |
| **Harness / 运行脚手架** | Prompt、Context、工具、Memory、路由、重试、Sandbox、Reviewer / prompts, context, tools, memory, routing, retries, sandbox, reviewers | 放大并约束 Agent，形成可恢复、可验证的执行循环 / amplify and constrain agents into recoverable, verifiable execution loops |
| **System / 系统** | Goal、Workflow、角色、数据、Gate、预算、权限、发布和治理 / goals, workflows, roles, data, gates, budget, authority, release, governance | 选择和组织 Agent–Harness 组合，并判断结果是否真正服务目标 / select and govern Agent–Harness combinations and determine whether outcomes serve the Goal |

**中文：** 当前项目的核心是**受治理的自进化系统**，其中已经包含 Harness 的自我优化能力；基础模型权重更新并不是当前主要机制。  
**English:** The project is primarily a **governed self-evolving system** with harness-level optimization. Base-model weight updates are not the current primary mechanism.

## 核心目标 / Core Objective

**中文：** 将 LCA 骨架数据库、节点 Wiki、证据、跨行业连接、LCA 数据集绑定、BOM 与产品系统的生产，从“人通过对话维护下一步”升级为“Desired State、Job、Event、Artifact 与 Goal Contract 驱动”的长期自治生产系统。

**English:** Transform production of LCA skeleton databases, node Wikis, evidence, cross-industry links, dataset bindings, BOMs, and product systems from conversation-maintained execution into a long-running system driven by Desired State, Jobs, Events, Artifacts, and Goal Contracts.

系统追求的不是“所有步骤运行成功”，而是：

The system does not optimize for “all steps succeeded.” It optimizes for:

```text
真实 Goal / True Goal
  → 可计算任务 / Executable Jobs
  → 受限 Agent + 确定性能力 / Bounded Agents + Deterministic Capabilities
  → 不可变 Artifact / Immutable Artifacts
  → 独立证明 / Independent Assurance
  → 自治资格判断 / Autonomy Eligibility
  → 受控发布 / Governed Release
  → 线上监测、修复与演进 / Online Monitoring, Repair, and Evolution
```

## 架构概览 / Architecture Overview

```text
Interface / 交互层
  CLI · Skills · Dashboard · Automation API · Future MCP/Site UI
                         │
Control & Governance / 控制与治理
  Goal Registry · Reconciler · Planner · Scheduler · Repair Controller
  Goal / Autonomy / Assurance Contracts · Capability Envelope
                         │
Execution / 执行层
  Deterministic Runtime · Agent Runtime · Harness · Sandbox · Workers
                         │
Domain / 领域层
  Portfolio · Name Graph · Evidence/Wiki · Cross-link/LCA · BOM/Product System
                         │
Assurance & Release / 保障与发布
  Gate Dispatcher · Independent Reviewer · Mutation/Cohort Lab
  Alignment Assessment · Autonomy Check · Governed Release · Rollback
                         │
Foundation / 基础设施
  SQLite State DB · Event Log · Artifact CAS · Policies · Defect Memory
```

正常生产路径由系统自主推进：

The normal production path is autonomous:

```text
Desired State → Reconcile → Plan → Execute → Gate
→ Alignment → Autonomy Check → Release → Monitor
```

人不再是每一步的操作员，但始终位于治理回路之上。  
Humans leave the normal execution loop but remain above the governance loop.

## 四个不可变治理对象 / Four Immutable Governance Objects

| 对象 / Object | 回答的问题 / Governing question |
|---|---|
| `goal-contract-v2` | 什么结果有价值、什么禁止出现、哪些终态是诚实的？ / What outcome is valuable, what is forbidden, and which terminal states are honest? |
| `autonomy-contract-v1` | 在不同风险下，系统可以自动执行哪些动作？ / Which actions may the system perform automatically at each risk level? |
| `assurance-contract-v1` | 哪些独立证据足以证明 Goal Clause 或发布成立？ / What independent proof is sufficient for a Goal clause or release? |
| `capability-envelope-v1` | 当前 Agent–Harness 组合在什么模型、工具、Workflow、输入范围和误差边界内被认证？ / Under which runtime, scope, cohort, and error boundary is the Agent–Harness combination certified? |

每个生产 Job 都冻结四类合同及其内容哈希。运行中的 Job 不能静默采用更宽松的 Goal、不同的 Capability 或新的发布权限。

Every production Job freezes exact versions and hashes of all four contracts. A running Job cannot silently adopt a looser Goal, a different Capability, or broader publication authority.

## Goal 演进 / Goal Evolution

Goal Contract 是一个**版本化、可证伪、受治理的目标假设**，不是一次写死的 Prompt。

A Goal Contract is a **versioned, falsifiable, governed goal hypothesis**, not a one-time prompt.

```text
人的初始意图 / Initial human intent
  → Goal hypothesis / 目标假设
  → Agent operationalization / Agent 操作化
  → Execution & observation / 实践与观测
  → Deviation / 偏差
  → GoalChangeProposal / 目标修正案
  → Acceptance-set diff / 接受集合差异
  → Independent evaluation / 独立评价
  → Human authorization when semantics or risk change / 语义或风险变化时由人授权
  → New immutable Goal version / 新的不可变 Goal 版本
```

Agent 可以主动发现歧义、冲突、不可观测目标和代理指标偏离，并提出拆分或修订方案；但只要修改改变“什么算成功”、风险由谁承担或系统为何服务，就不能由提出修改的 Agent 自行生效。

Agents may discover ambiguity, conflict, unobservable goals, or proxy-metric drift and propose decomposition or amendments. Any change to success semantics, risk ownership, or system purpose requires authority outside the proposing Agent.

## 自我修复与自我演进 / Self-Repair and Self-Evolution

| 层级 / Level | 范围 / Scope | 典型动作 / Typical actions |
|---|---|---|
| **L0 · 执行自愈 / Execution healing** | 不改变业务语义 / no semantic change | Lease、Provider、缓存、I/O、Worker、Checkpoint 恢复 / lease, provider, cache, I/O, worker, checkpoint recovery |
| **L1 · Job 目标回归 / Job goal recovery** | 有界修改当前任务输入或 Harness / bounded task or harness change | 查询改写、文档路由、局部内容修复、最小节点重放 / query rewrite, document routing, local content repair, minimal replay |
| **L2 · 系统迭代 / System evolution** | 跨 Job 的 Prompt、工具、Gate、Policy、Workflow 或代码变化 / cross-Job prompt, tool, gate, policy, workflow, or code change | Sandbox → Golden/Mutation → Shadow → Canary → Promotion/Rollback |

所有系统变更都必须满足：候选生成者不等于最终验证者；不能通过删除困难目标、缩小分母或放宽 Gate 来制造成功；失败时可以回滚到上一冻结版本。

Every system change separates proposer from final evaluator, forbids manufacturing success by deleting hard goals, shrinking denominators, or weakening gates, and remains rollbackable to the previous frozen version.

## 人与 Agent 的关系 / Human–Agent Relationship

**人的保留权力 / Human-reserved authority**

- 项目目的、利益相关者价值和 Goal Contract 正式生效；  
  Project purpose, stakeholder value, and formal Goal activation.
- 会改变成功语义、风险预算、身份边界或发布权限的修订；  
  Amendments that change success semantics, risk budgets, identity boundaries, or publication authority.
- 高影响定量数据、代理政策、不可逆操作和专家签署；  
  High-impact quantitative data, proxy policy, irreversible operations, and expert sign-off.
- 自治等级升级与系统级效用审计。  
  Autonomy-level promotion and system-level utility audit.

**Agent 与系统自主负责 / Autonomous Agent and system responsibilities**

- 缺口发现、任务分解、调度、搜索、提取、候选生成和独立审查；  
  Gap discovery, decomposition, scheduling, retrieval, extraction, candidate generation, and independent review.
- L0/L1 修复、低风险 L2 候选、Sandbox/Shadow/Canary、自动回滚；  
  L0/L1 repair, low-risk L2 candidates, sandbox/shadow/canary, and automatic rollback.
- 偏差聚类、Goal 修订提案、Capability 重新认证任务和最小异常包。  
  Deviation clustering, Goal amendment proposals, Capability recertification work, and minimal exception packages.

原则是：**正常路径无人化，治理边界有人化，人工介入事件化。**  
Principle: **autonomous normal path, human-governed boundaries, event-driven escalation.**

## v2 状态 / v2 Status

| 能力 / Capability | 状态 / Status |
|---|---|
| Goal、Autonomy、Assurance、Capability 四合同 / Four-contract governance | ✅ Implemented |
| Job 不可变合同绑定 / Immutable Job contract binding | ✅ Implemented |
| Goal 修正案、接受集合差异与影响传播 / Goal amendments, acceptance-set diff, impact propagation | ✅ Implemented |
| 非补偿式 Alignment Assessment / Non-compensatory alignment assessment | ✅ Implemented |
| Cohort Capability 认证与线上漂移撤权 / Cohort certification and online drift revocation | ✅ Implemented |
| Governed Release `disabled / shadow / enforced` | ✅ Implemented |
| 生产配置 / Checked-in production configuration | 🟡 `shadow` by default |
| 真实低风险自动发布授权 / Real low-risk autonomous publication authority | ⏳ Requires an independently certified Capability, zero blocking reassessments, and explicit governance approval |

**中文：** `v2 code_complete` 表示代码、治理闭环、迁移、CLI、Schema 和测试已具备；它不等于系统已经获得真实生产环境中的无人发布授权。  
**English:** `v2 code_complete` means the code, governance loop, migrations, CLI, schemas, and tests exist. It does not mean the system already has unattended publication authority in a real production environment.

当前配置仅精确映射 `wiki-node-production@9`，并从 `shadow` 开始；切换到 `enforced` 前必须完成真实 Cohort 认证与治理审批。

The checked-in configuration maps `wiki-node-production@9` exactly and starts in `shadow`. Real Cohort certification and governance approval are required before switching to `enforced`.

## 快速开始 / Quick Start

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

lca-platform init
lca-platform validate
lca-platform reconcile --once
lca-platform status
lca-platform dashboard

pytest
```

默认运行数据 / Default runtime data:

```text
var/state.db       # control-plane state / 控制平面状态
var/artifacts/     # content-addressed artifacts / 内容寻址产物
var/workspaces/    # isolated execution workspaces / 隔离执行空间
```

Dashboard 默认监听 `http://127.0.0.1:8765`。绑定非 loopback 地址必须显式使用 `--allow-remote`。

The Dashboard listens on `http://127.0.0.1:8765` by default. Binding to a non-loopback address requires `--allow-remote`.

## 自治 Campaign / Autonomous Campaign

```bash
PYTHONPATH=src python -m lca_project.cli --root . autonomy-create \
  config/autonomous-wiki-campaign.example.json --run
```

Campaign 请求不可变且幂等，并发和单 Job 修复次数是硬预算。系统可以自动创建 Job、物化 Workflow、执行、审计和进行有界修复，但不能自行修改 Goal Contract 或扩大发布权限。

Campaign requests are immutable and idempotent, with hard concurrency and per-Job repair budgets. The system may create Jobs, materialize Workflows, execute, audit, and perform bounded repair, but it cannot modify Goal Contracts or expand publication authority by itself.

## 治理操作 / Governance Operations

```bash
# 查看治理状态和准入条件 / Inspect governance status and readiness
lca-governance --root . status
lca-governance --root . readiness

# 评估一个 Job 是否与 Goal 对齐 / Assess Job-to-Goal alignment
lca-governance --root . assess-alignment job_A039 \
  --clause-results clause-results.json \
  --prohibited-outcomes prohibited-outcomes.json \
  --terminal-state needs_research \
  --capability-match

# 判断是否具备低风险发布资格 / Check low-risk publication eligibility
lca-governance --root . check-autonomy job_A039 publish --risk low \
  --runtime runtime-fingerprint.json \
  --input-scope input-scope.json \
  --requirement-evidence release-requirement-evidence.json
```

完整 Goal 修正案、Capability 认证、在线观测和合同暂停示例见 [`docs/goal-contract-governance-v2.md`](docs/goal-contract-governance-v2.md)。

See [`docs/goal-contract-governance-v2.md`](docs/goal-contract-governance-v2.md) for complete Goal amendment, Capability certification, online observation, and contract suspension examples.

## 长任务监督 / Long-Running Stage Supervision

```bash
lca-platform supervise var/workspaces/<job>/stage-plan.json
```

`supervise` 将冻结命令批次作为一个有界阶段运行。子进程在 Supervisor 内阻塞等待，不向 Agent 暴露轮询 API；重试、模型调用、子进程数和墙钟时间都有硬预算。达到第 101 个模型调用单位或收到首次 context compaction 后，系统写入阶段专属 checkpoint，而不是继续失控运行。

`supervise` executes a frozen command batch as one bounded stage. Child processes are waited inside the Supervisor without exposing polling APIs to the Agent. Retries, model calls, subprocesses, and elapsed time are hard-budgeted. Before the 101st model-call unit or after the first reported context compaction, the system writes a stage-specific checkpoint instead of continuing uncontrolled execution.

## 仓库结构 / Repository Layout

| 路径 / Path | 职责 / Responsibility |
|---|---|
| `src/lca_project/kernel` | State、Event、CAS、编排、治理、修复、认证与发布 / state, events, CAS, orchestration, governance, repair, certification, release |
| `src/lca_project/contracts` | 版本化控制平面与治理协议 / versioned control-plane and governance protocols |
| `src/lca_project/domains` | Graph、Wiki、Cross-link、LCA、BOM 领域适配器 / graph, Wiki, cross-link, LCA, and BOM adapters |
| `src/lca_project/dashboard` | 本地 read model、HTTP API 与控制界面 / local read models, HTTP API, and control UI |
| `capabilities` | 确定性执行能力、权限和副作用合同 / deterministic capabilities, permissions, and side-effect contracts |
| `workflows` | 声明式、版本化 DAG / declarative versioned DAGs |
| `agents` | 冻结 Agent 定义、Prompt 与输出合同 / frozen agent definitions, prompts, and output contracts |
| `policies` | Goal、自治、保障、Capability、Gate、预算与修复政策 / Goal, autonomy, assurance, Capability, gate, budget, and repair policies |
| `skills` | 薄意图路由，不承载生产状态 / thin intent routing; never production state |
| `contracts` | JSON Schema 与跨模块协议 / JSON Schemas and cross-module protocols |
| `tests` | Golden、Mutation、Cohort、回放和验收测试 / Golden, Mutation, Cohort, replay, and acceptance tests |
| `docs` | 技术设计、治理、复盘、状态和验收文档 / design, governance, retrospective, status, and acceptance documentation |

## 搜索与外部证据 / Search and External Evidence

搜索 Provider 路由定义在 `config/search-providers.json`。将 `config/search-providers.env.example` 复制为 `.env.search.local` 并填写本地密钥；该文件已被 Git 忽略。Provider Policy 只声明路由和 Secret 映射，实际 Worker 仍需要对应 Adapter。

Search-provider routing is declared in `config/search-providers.json`. Copy `config/search-providers.env.example` to `.env.search.local` and provide local keys; the secret file is ignored by Git. The provider policy declares routing and secret mapping, while Workers still require the corresponding adapters.

## 安全边界 / Safety Boundaries

- Agent 只产生 Proposal、Verdict、Patch 或 Attestation，不直接修改权威库。  
  Agents produce proposals, verdicts, patches, or attestations; they do not directly modify authoritative stores.
- Artifact 不可变且内容寻址，跨模块通信使用版本化 Event。  
  Artifacts are immutable and content-addressed; cross-module communication uses versioned Events.
- 生成候选、独立评价和发布授权相互分离。  
  Candidate generation, independent evaluation, and publication authorization are separated.
- Retry 有界；重复、未知或政策敏感失败进入 Quarantine 或最小异常包。  
  Retries are bounded; repeated, unknown, or policy-sensitive failures become quarantined or minimal exception packages.
- 系统不能证明开放领域 LCA 知识“绝对无错”，也不替代专家对身份、边界、代表性和关键定量值的责任。  
  The system cannot prove absolute correctness of open-domain LCA knowledge and does not replace expert responsibility for identity, boundaries, representativeness, or critical quantitative values.

## 完成定义 / Definition of Done

```bash
python -m lca_project.cli validate
pytest
python -m compileall -q src tests
```

代码级完成还需要机器可读的治理 Readiness 通过。生产级自动发布进一步要求真实 Cohort Certificate、无阻塞 Reassessment/Drift，以及治理者明确将低风险发布切换为 `enforced`。

Code-level completion also requires a green machine-readable governance readiness check. Production-level autonomous publication additionally requires real Cohort certificates, no blocking reassessment or drift, and explicit governance activation of low-risk `enforced` release.

## 文档 / Documentation

- [HTML 文档中心 / HTML Documentation Center](docs/index.html)
- [文档源目录 / Documentation Sources](docs/README.md)
- [Goal Contract Governance v2](docs/goal-contract-governance-v2.md)
- [系统自我修复与目标对齐架构 / Self-Repair and Goal Alignment Architecture](docs/系统自我修复与目标对齐架构.md)
- [技术设计 / Technical Design](docs/技术设计-骨架数据库自治生产平台.html)
- [实施与验收状态 / Implementation and Acceptance Status](docs/重构实施与验收状态.md)

Markdown 是叙事文档的权威来源；对应 HTML 由 `python scripts/build_docs_site.py` 生成。

Markdown is authoritative for narrative documents; matching HTML pages are generated by `python scripts/build_docs_site.py`.

## 名称与兼容性 / Naming and Compatibility

本项目的发布名称由 `lca-project` 更新为 **`lca-autonomy-system`**，显示名称为 **LCA Autonomy OS / LCA 自治演进系统**。为避免破坏现有自动化，Python import package 和 CLI 暂时保持不变：

The distribution name changes from `lca-project` to **`lca-autonomy-system`**, with the display name **LCA Autonomy OS / LCA 自治演进系统**. To preserve existing automation, the Python import package and CLI names remain unchanged:

```text
Python package: lca_project
CLI:            lca-platform
Governance CLI: lca-governance
```

旧名称仍可能出现在历史迁移记录、冻结 Artifact 和旧版技术文档中；这些历史对象不会为了品牌统一而重写。

The legacy name may remain in historical migration records, frozen Artifacts, and older design documents. Historical objects are not rewritten solely for rebranding.
