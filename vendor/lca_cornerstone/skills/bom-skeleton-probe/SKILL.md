---
name: bom-skeleton-probe
description: 拿真实整车/产品 BOM(A2MAC1 拆解表等 .xlsx)对 lca-cornerstone 骨架做"LLM 提名 + 代码裁决"的完整性探针(自动化 P3 外部 BOM 探针),质量加权出 Exact/Coarse/Gap 账并暴露真缺口。当用户给一份真实 BOM/物料清单/拆解表,想:按骨架建模某车型/产品、排查骨架节点遗漏、算某车的 LCA 牌号级覆盖率、或验证某行业图对真实产品的完整性时触发。关键词:BOM、物料清单、拆解表、teardown、A2MAC1、整车建模、骨架缺口、完整性探针、覆盖率。
---

# BOM 骨架完整性探针

把真实 BOM 跨母行业图做 **LLM 提名 + 代码裁决** 的完整性探针。等价于 `name-graph-sop` 的 "LLM fan-out + gate.py 后置":
LLM 只在 workflow 内**提名**(脏 BOM 串 → 牌号级身份 + 候选节点 id),裁决(Exact/Coarse/Gap)由后置确定性脚本**对真图查 facet** 决定,LLM 的 claim 永远被查不被信。

设计全文见 `docs/bom-skeleton-probe-design.html`。

## 为什么是这个形状(必读,否则会设计错)

**Workflow 脚本沙箱无文件系统访问。** 所以 BOM 解析 / 查图 / 写报告**都不能进 workflow**——workflow 里只装纯 LLM 提名;
所有 I/O 用 main-loop 的 Bash 脚本**夹在 workflow 前后**。这和 `gate.py` 不在 workflow 内派 LLM 同理。

```
[Bash] prep_bom_buckets.py  →  [Workflow] LLM 提名  →  [Bash] 冻结 + grade_bom_matches.py(=GATE)
 解析+分桶+切索引+生成run.js    Sonnet×N 提名 / Opus 复裁    对真图查facet定级 + 质量加权
```

## 配方(main-loop 按序驱动)

约定 `<BOM>` = 用户的 .xlsx 路径,`<slug>` = 车型/产品短名(如 `tesla-model-x`)。

**① 入口物料化(Bash,确定性)**
```bash
python3 scripts/prep_bom_buckets.py "<BOM>" <slug>
node --check /tmp/<slug>-bom-probe.run.js    # 自检 splice 出的 run.js 合法
```
产出 `/tmp/<slug>-bom-buckets.json`(桶 + 质量)和 `/tmp/<slug>-bom-probe.run.js`(自包含 run-script,数据内联,不进主会话上下文)。

**② LLM 提名(Workflow 工具,后台)**
```
Workflow({scriptPath: "/tmp/<slug>-bom-probe.run.js"})
```
后台跑 N 个 Sonnet 提名器 + 1 个 Opus 复裁(~12–15 分钟,~$2–3)。完成会有 `<task-notification>`。
**iterate 用 `resumeFromRunId`**:改 prompt/加桶后重跑只付增量。

**③ 冻结提名表(Bash)** —— 从 task 输出文件取 `.result.matches`(注意是 `.result.matches`,文件顶层是 `{summary,agentCount,logs,result}`):
```bash
OUT="<task-notification 给的 output-file 路径>"
jq '{vehicle: .result.vehicle, matches: .result.matches}' "$OUT" > docs/<slug>-bom-matches.json
```
**这份 `docs/<slug>-bom-matches.json` 是可复现性的锚** —— 裁决可在它上面无限重放。

**④ 裁决 = GATE(Bash,零 LLM)**
```bash
python3 scripts/grade_bom_matches.py docs/<slug>-bom-matches.json <slug>
```
对真图逐条查 `facets ⊇ claimed?` 定 Exact/Coarse/Gap、质量加权,写 `docs/<slug>-probe-graded.json`。
打印诚实覆盖(仅 Exact)与含代理(Exact+Coarse)两个数。

**⑤(可选)抽样复核** —— 派 `checker`(`subagent_type:checker`)对抗复核 LLM 的身份推断并重跑真 grade。

## 修缺口 → 只重跑 grade(零 LLM)

grade 暴露的 **Coarse** 就是骨架真缺口(同族代理,GWP 会偏)。补节点后**只重跑 ④**(零 LLM、零 token),
相应桶确定性从 Coarse 翻成 Exact:
1. 在对应母图 `docs/<ind>-name-graph.json` 镜像同族模板节点加 产品+产出活动+边(facet 用受控值)。
2. `python3 scripts/validate_graph.py docs/<ind>-name-graph.json` 必须 **11/11**(改图硬约束,不得 LLM 自报)。
   - 活动键碰撞(`前景活动键唯一` 不过)→ 用系列专属 `reference_product_anchor`(镜像 2xxx/7xxx 先例)避碰。
3. 重跑 ④ → 看翻档。

## 三条铁律(实测教训)

1. **细分桶是诚实数的前提,不只靠裁决严。** `(系统,材料)` 聚合会把电池揉进电控桶、被迫整桶提名到一个"存在"的电控节点 → 假 Exact(Tesla 实测 v1 粗分桶给 83.3%,几乎等于旧口径 84.7%)。prep 已对 `Electronic components/Other` 按路径关键词(battery/motor/inverter/harness/thermal/ecu)细拆;新 BOM 若出现新的跨域大杂烩材料,补 `refine_sub`。
2. **grade 完全不读 LLM 的 `verdict_hint`** —— 只入审计列。代码独立查真图说了算(可复现、可证伪、可当 gate)。
3. **诚实覆盖 = Exact;Coarse 是"同族代理、GWP 会偏",不是覆盖。** 别把 Exact+Coarse 当头条对外报。

## 产物清单
```
scripts/prep_bom_buckets.py              入口物料化 + 生成 run-script
.Codex/workflows/bom-skeleton-probe.js  workflow(LLM 提名;DATA-BINDING 段被 prep 注入)
scripts/grade_bom_matches.py             确定性裁决 GATE
docs/<slug>-bom-matches.json             冻结的 LLM 提名表(可复现锚)
docs/<slug>-probe-graded.json            裁决结果
```
