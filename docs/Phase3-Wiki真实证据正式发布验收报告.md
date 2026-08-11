# Phase 3：Wiki 真实证据到正式发布验收报告

日期：2026-08-10

## 验收结论

`ict_equipment::P003` 已在目标项目的隔离生产工作区完成真实证据闭环，并正式发布。
批次 `production-p003-v26` 的 journal 最终状态为 `published`，完整经过：

```text
planned → prepared → research_ready → verified → frozen
→ content draft apply → apply_ready → gated → reviewed apply → published
```

原项目不是运行写入目标。最终批次位于：

```text
var/workspaces/wiki-production-20260810-v26/
```

## 真实证据与独立核验

- Nomination：30 条声明，30 条唯一，逐 requirement 基数全部符合合同。
- 外部事实：10 条；受控建模判断、内部图事实和显式缺口：20 条。
- 确定性 Search/Fetch：8 次冻结查询、2 次页面抓取、0 次预算越界。
- 权威域：`publications.europa.eu` 与 `www.ibm.com`，满足至少两个独立 authority。
- Verify-only：`gpt-5.6-sol / medium`，禁用 Web、浏览器、插件、App 和多 Agent。
- Verify 结果：10/10 `CONFIRMED`，10/10 `node_alignment=EXACT`。
- Runtime 证据完整冻结：invocation、events、stderr、verdicts、usage 均有 SHA-256。

## 内容与发布门禁

Draft Content Gate 的结构、丰富度、来源多样性、核心章节证据、引用解析、证据表、
非退化等检查全部通过；草稿事务提交 1 个文件、30 个操作、0 个 manual review。

Claim coverage：

```text
total=30  eligible=30  confirmed=10  controlled_internal=20
missing=0  unresolved=0  contradicted=0  manual_review=0  hash_drift=0
coverage_rate=1.0  quote_compliance_rate=1.0
```

最终 Gate：

| Gate | 结果 |
|---|---|
| Wiki v2 quality | PASS（15 项检查全通过） |
| Name graph | PASS（11/11） |
| Wiki lint + coverage | PASS（31/31） |
| LCA node search matrix | PASS（6/6） |
| LCA dataset association | PASS（11/11） |
| Go/No-Go | GO |

Reviewed Apply 采用 coverage hash lock，事务提交后才执行 publish。正式 bundle 包含
132/132 个 ICT 节点；P003 状态为：

```yaml
schema_version: wiki-v2
body_status: reviewed
claim_verification_status: complete
provenance_status: claim_verified
content_maturity: research_ready
```

## 为完整发布补齐的工程资产

早期隔离 fixture 只有 16 节点图、2 个页面和 2 个 sigil，导致行业级 preview
无法代表真实生产范围。本阶段把原项目中明确属于 ICT 发布闭环的只读资产按需复制到
目标 vendor snapshot：完整 132 节点图、132 个 Wiki 页面、132 个 sigil、来源注册表、
LCA 注册表、viewer 模板和由当前图重新生成的 production name-graph HTML。

补齐后，隔离工作区 manifest 从 61 个文件扩展到 346 个文件；preview 的 graph、
wiki lint、bundle、viewer、preview name-graph、production overlay 六步全部通过。

## 自主发现与修复记录

生产尝试没有用人工改 verdict 绕过错误：

1. Verify 发现 IBM 的“模块化服务器系统”事实把整机与刀片模块混为相邻对象，判为
   `ADJACENT/INSUFFICIENT`；系统停止在 `research_ready`。
2. 一条共享资源断言因证据窗口截断被判为 `INSUFFICIENT`；系统未写 Wiki。
3. 修复 source routing 与 claim constraint：IBM 仅支持目标本体的“亦称高密度服务器”，
   物理形态、机箱使用和共享资源边界改用欧盟法规的可直接定位原文。
4. 新建冻结批次重新 Nomination、Search/Fetch、Verify，得到 10/10 EXACT。
5. Preview 再发现 fixture 页面/sigil/registry/template 不完整；补齐生产快照并先在独立
   workspace 验证六步 PASS，再启动最终 v26，而不是在冻结批次内篡改输入。

## 项目回归与源仓库保护

```text
pytest: 87 passed
make validate: PASS
capabilities: 7
workflows: 5（Wiki workflow 11 steps）
```

源仓库 HEAD 仍为 `1a59503d3b6a86a0e58ca773de065266caf144bb`；tracked binary diff
指纹与本阶段开始前一致：
`92081cade4cdcc4e93ad42230dc7793047eab135354799aed3177a625ef2c467`。
源仓库原有 dirty worktree 被保留，未被目标项目的生产 Apply 修改。

## 关键产物

- `runs/wiki-batches/ict_equipment/production-p003-v26/journal.json`
- `runs/wiki-batches/ict_equipment/production-p003-v26/verify-runtime/`
- `runs/wiki-batches/ict_equipment/production-p003-v26/coverage.json`
- `runs/wiki-batches/ict_equipment/production-p003-v26/gate-report.json`
- `runs/wiki-batches/ict_equipment/production-p003-v26/publish-report.json`
- `wiki/ict_equipment/products/P003--服务器-通用计算-刀片式.md`
- `docs/ict_equipment-wiki-data.js`
- `docs/ict_equipment-wiki.html`

发布后检查发现旧 `publish` 只构建数据包、遗漏 HTML 查看器。现已补生成查看器，
并把发布器修复为 bundle 与 viewer 必须同时 PASS；该缺陷已固化为隔离工作区回归测试。
