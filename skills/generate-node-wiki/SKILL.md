---
name: generate-node-wiki
description: "为 LCA 骨架数据库创建、补全、重建、修复或审计节点 Wiki，并将请求提交为持久化 Wiki 生产 Job。用户提供行业和 P003、A039 等 Product/Activity 节点 ID，要求进行资料研究、正文补充、参数表采集、预览、reviewed 发布或问题修复时使用。不要用于纯 Name Graph、Cross-link、LCA Binding 或 BOM 任务。"
---

# 生成节点 Wiki

将用户意图转换成受控的 Wiki 生产请求，并通过 LCA 项目的控制平面提交持久化 Job。不要直接运行生产脚本、Agent launcher、Gate、Apply 或 Publish。

## 规范请求

- 必须取得 `industry` 和节点 ID。
- 只接受 `P###` 或 `A###` 格式的节点 ID，不猜测、不纠正、不替换用户提供的 ID。
- 一个 Job 只处理一个节点。用户同时提供多个节点时，为每个节点建立独立请求；不得共享 Verify、Gate、预算、重试或发布资格。
- 保留用户提供的 `batch_id`；没有提供时交给控制平面生成。
- 用户没有明确要求正式审核发布时，使用 `publication_mode: preview`；只有明确要求进入正式审核发布时才使用 `reviewed`。
- `preview` 只是可查看的中间产物，不是正式生产成功。自治 Campaign 对 preview 请求使用
  `completion_goal: lca_modeling_ready`；正式发布请求必须同时使用
  `publication_mode: reviewed` 与 `completion_goal: reviewed_publication`。
- `reviewed_publication` 只有在 Job 到达 `published`，且 `publish` Task 的不可变输出 Manifest
  包含与当前 Job、候选哈希、G10/G11 证明绑定的 `release-record-v1` 时才算完成。禁止用
  preview、Candidate、Gate 或 reviewed apply 的阶段成功提前结束 Campaign。

## 执行多语证据检索

- 为每个节点冻结术语档案：规范中文名、中文同义词、规范英文名、英文同义词、相关词和排除词。
- 对正文事实和每个表格字段分别生成中文与英文查询；不得把仅有中文查询或仅有英文来源提名称为多语检索。
- 同义词用于扩大召回；相关词只能发现候选，不得自动等同于目标节点；命中排除词时必须核对对象边界。
- 优先采集政府/监管文件、标准与行业组织文件、制造商技术资料、同行评议研究和可审计项目记录。定量值必须同时冻结单位、basis、地域、代表期、locator、原文和内容 hash。
- 通用行业值只能标为代理值，并写明适用条件与失效条件；不得冒充节点特定实测值。无法取得可靠数值时写入 `explicit_gap`，不得留成未执行采集的空表。
- `preview` 仍必须执行 `table_collect → table_verify → table_population_gate → table_apply`；它只跳过正式发布资格、reviewed apply 和 publish。
- 在 Nomination 前冻结 `wiki-research-plan-v1`；历史 Registry 和 Source Hint 只能作为 `candidate_unverified`，必须在当前 Job 重新 Search/Fetch/Verify。
- 区分 `planned`、`searched`、`fetched` 和 `verified`；查询计划不得冒充检索完成，网络错误不得冒充零结果。
- 中文与英文别名默认只可扩大召回，未经当前 Job 的术语 Verdict 不得用于确认节点等价关系。
- Repair 必须绑定失败 Artifact Hash，并持久化 `wiki-repair-plan-v1` 和 Repair Receipt；禁止无痕修改研究输入。

请求格式：

```json
{
  "industry": "ict_equipment",
  "nodes": ["P003"],
  "publication_mode": "preview"
}
```

需要正式生产完成时，Campaign 必须显式冻结：

```json
{
  "completion_goal": "reviewed_publication",
  "requests": [{
    "industry": "ict_equipment",
    "nodes": ["P003"],
    "publication_mode": "reviewed"
  }]
}
```

## 提交 Job

将请求保存为 JSON，并通过注册的 Skill 入口提交：

```bash
lca-platform start generate-node-wiki --request <request.json>
```

只负责提交 Job。不要在 Skill 内解释 Workflow、选择脚本、选择模型、手动推进阶段、轮询长进程或决定重试。Job 提交后，由 Reconciler、Scheduler、Capability Runtime、Gate Dispatcher、Repair Controller 和 Release Manager 管理后续状态转换。

如果控制平面只返回已受理的 Job，而尚未自动执行后续阶段，如实报告当前状态并停止；不要回退到对话驱动的人工编排方式。

## 遵守权限边界

- 将 Agent 输出仅视为 Proposal、Verdict 或 Attestation。
- 不把对话记忆、模型常识或未冻结网页当作已核验证据。
- 不允许生产 Agent 审核自己的输出。
- 不直接写入正式 Wiki、Registry、Source 或 Release 目标。
- 没有候选绑定的 Gate Receipt 和 hash-locked Apply 时不得发布。
- 遇到 `blocked`、`checkpointed`、`quarantined` 或其他终态时停止，并保留结构化原因和 Artifact Hash。

## 汇报结果

至少报告：

- Job ID 和节点 ID；
- Workflow、Policy 与 Skill Route 版本；
- 当前状态和请求 Artifact Hash；
- 已产生的 Artifact、Checkpoint 或 Release Receipt Hash；
- 阻塞、隔离或需要人工处理的原因。

只有控制平面返回持久化 Release Receipt 时，才能声称节点 Wiki 已发布。
