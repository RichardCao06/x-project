---
name: industry-graph
description: "为 LCA 骨架数据库生产行业 Product/Activity 名称图；按原 name-graph-sop 完成约定、多源播种、闭合、外部对表、独立评审、整合、11/11 代码 Gate 和 hash-bound 发布。"
---

# 行业名称图生产

将一个行业提交为持久化名称图生产 Job。Agent 只生成 Proposal 或 Review，不能直接写正式图、宣布 Gate 通过或自行发布。

生产顺序固定为：计划 → 建模约定 → 多源播种 → 合并建图 → A/B/C 闭合 → 外部分类对表 → 独立评审 → 整合 → 完整性记分卡 → 确定性物化/对账 → 11/11 Gate → hash-bound 发布。

请求至少包含稳定的行业 slug；`display_name` 省略时使用 slug：

```json
{"industry":"steel","display_name":"钢铁"}
```

只负责通过 Skill 入口创建 Job。只有得到绑定候选 SHA-256 的 `graph-gate-report-v2` PASS 和 `release-record-v1`，才可声称发布成功。
