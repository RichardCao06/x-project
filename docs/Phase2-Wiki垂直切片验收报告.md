# Phase 2：Wiki 垂直切片验收报告

日期：2026-08-10

## 验收结论

Phase 2 的工程执行边界和离线演练已通过；正式 reviewed/publish 尚未授权。
平台能在完全不访问原仓库的隔离工作区中，对三种页面状态执行真实
`plan → prepare → validate`，把每个节点的输入、输出、状态和证明写入
SQLite、CAS 与 Event Ledger，并在缺少新一轮外部证据和 Verify attestation 时
诚实停止在 `prepared`。这不是失败，而是“证明不足不得发布”原则的预期结果。

## 演练节点

| 节点 | 路由 | 实际结果 |
|---|---|---|
| `oil_refining::A017` | current → audit | 39 repair claims；validate PASS |
| `ict_equipment::P031` | current → audit | 31 repair claims；validate PASS |
| `ict_equipment::P003` | legacy → rebuild | nomination workflow 生成；validate PASS |

最新 rehearsal Artifact：

```text
sha256:7a5cb819c1959a2ca63e43ff4ffaa6f0dddd79858235be8d7b76265b0058fb12
stopped_at: prepared
publish_authorized: false
source_checkout_access: false
```

## 本阶段实现

- 11 阶段持久化状态机：plan、prepared、research-ready、verified、frozen、
  draft-gated、draft-applied、previewed、release-gated、reviewed-applied、published。
- 每阶段 CAS 输入/输出哈希、Event、SQLite Job/Run/Stage 投影。
- 同一证据幂等重放；不同证据冲突并隔离；resume 不重复副作用。
- Nomination/Repair 同批隔离；Agent 只能提交 frozen proposal/verdict/attestation。
- Agent 模型、effort、Prompt、工具、usage 与禁网证明 G0 校验。
- Proof Authority 对 Agent attestation 与 Gate 回执签名；证明同时绑定 CAS、SQLite
  和 Event Ledger。原始 JSON、自报 attestation、签名篡改及跨候选复用均不能放行。
- G1 节点身份、G4 EXACT/原文、Draft Content Gate、G0–G7 release hard-bind。
- Product/Activity 内容合同、claim 集合守恒、共享脚注原子化、Golden 非退化、
  preview 章节顺序、SSRF/本地来源阻断。
- Release 中途故障回滚、旧 Gate 复用阻断、reviewed apply 旧目标哈希绑定。
- 60 文件的隔离兼容工作区；全部来自 frozen vendor snapshot，无源仓库路径或 symlink。

## 测试证据

```text
pytest: 78 passed, 0 xfail, 0 skipped
wiki runtime selftest: PASS
project validate: PASS
capabilities: 7
wiki workflow steps: 11
Phase 2 anchor hashes: 6/6
```

历史缺陷语料位于 `defects/wiki/`，覆盖 source≠claim verified、共享脚注、
本地/私网 URL、重复证据缺口壳、Product/Activity 混淆、coverage 丢项、
Golden 退化、preview 重排、节点身份交换、ADJACENT 升格、旧 Gate 复用和
Apply 崩溃。

干净运行态连续执行两次 rehearsal，三个 run ID 与报告 Artifact hash 完全相同；
运行投影为 3 jobs / 3 runs / 每节点 2 个已完成阶段，没有重复副作用。

## 为什么没有发布 Wiki

本轮没有新运行 Nomination、确定性 Search/Fetch 和独立 Verify，因此没有新的
claim-level EXACT 证明、真实 invocation manifest 和 coverage 证书。复用旧运行或
合成 PASS 会违反 Wiki Skill 和技术设计。平台因此保持三节点为 draft/待研究状态，
没有修改任何正式 Wiki，也没有伪造 reviewed/published。

## 下一生产动作

为 P003 运行新的 nomination-only；为 A017/P031 对全部冻结 repair claims 执行确定性
Search/Fetch；通过固定 launcher 分节点运行 Verify-only。只有 coverage、Draft Gate、
G0–G7 和专家签署满足后，才进入 reviewed Apply 与 production publish。
