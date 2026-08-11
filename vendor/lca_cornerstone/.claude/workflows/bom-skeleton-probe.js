export const meta = {
  name: 'bom-skeleton-probe',
  description: '真实BOM跨母行业图的"LLM提名+代码裁决"骨架完整性探针(自动化P3外部BOM探针)。每个BOM聚合桶由Sonnet提名牌号级身份+候选节点id(只喂该族精简索引切片,不喂全图);Opus只复裁多材料/边界争议。裁决(Exact/Coarse/Gap)不在workflow内——由后置确定性脚本grade_bom_matches.py对真图查facet定级,LLM的claim永远被查不被信。等价于name-graph-sop的"LLM fan-out + gate.py后置"。',
  phases: [
    { title: 'Match',     detail: '每个BOM桶→牌号级身份+候选节点id [sonnet,索引切片]' },
    { title: 'Reconcile', detail: '多材料件/跨行业边界争议复裁 [opus]' },
    { title: 'Grade',     detail: '后置确定性gate(grade_bom_matches.py,零LLM):只log调用命令' },
  ],
}

// <<<DATA-BINDING-START>>>
// 默认从 args 读;prep_bom_buckets.py 生成 run-script 时会把本段替换为内联数据(沙箱无磁盘,故经 run-script 注入)。
const CFG = (typeof args === 'object' && args) ? args : {}
const VEHICLE = CFG.vehicle || 'tesla-model-x'
const BUCKETS = CFG.buckets || []   // [{bucket_id, system, material, matgroup, mass_kg, n_parts, sample_names, candidate_inds}]
const INDEX   = CFG.index   || {}   // { industry: "<该族精简索引文本>" }
const M = { reason: (CFG.models && CFG.models.reason) || 'claude-opus-4-7', recall: (CFG.models && CFG.models.recall) || 'sonnet' }
// <<<DATA-BINDING-END>>>

// 提名器输出形状:身份 + 候选id + claimed_facets(后置脚本据此查图);不含任何"已覆盖/Exact"判断
const MATCH_SCHEMA = {
  type: 'object',
  required: ['bucket_id', 'inferred_identity', 'industry', 'candidate_node_id', 'claimed_facets', 'verdict_hint'],
  properties: {
    bucket_id:         { type: 'string' },
    inferred_identity: { type: 'string' },
    industry:          { type: 'string' },
    candidate_node_id: { type: 'string' },
    claimed_facets:    { type: 'object' },
    alt_node_ids:      { type: 'array', items: { type: 'string' } },
    verdict_hint:      { type: 'string', enum: ['exact', 'proxy', 'no_node'] },
    rationale:         { type: 'string' },
  },
}

const sliceFor = b => (b.candidate_inds || []).map(i => `### 母图 ${i}\n${INDEX[i] || '(无索引)'}`).join('\n\n')

// ── phase Match:每桶一个 Sonnet 提名器,只喂它候选母图的索引切片 ──
phase('Match')
const proposals = (await parallel(BUCKETS.map(b => () => agent(
`你是 BOM→骨架的【提名器】(只提名,不裁决)。给定一个 BOM 聚合桶,推断它的【牌号级身份】,在候选母图索引里提名最吻合的节点 id。
规则:
- A2MAC1 的 material 列只到族级(Alloy/Steel/Electronic components…),据【部件名+整车常识】补到牌号级:车身外板=6xxx冷轧铝板、Tesla动力电池=NCA圆柱整包、制动盘=灰铸铁、电机叠片=硅钢、轮毂=A356铸铝…
- 在下方索引里找最吻合节点填 candidate_node_id;claimed_facets 必须用该母图【受控词表】里的合法值(后置脚本靠它查真图定级)。
- 同族有、但牌号/形态/化学不符 → 仍提名该同族节点并 verdict_hint="proxy";同族都没有 → candidate_node_id="" 且 verdict_hint="no_node";牌号完全吻合 → verdict_hint="exact"。
- **verdict_hint 仅供审计,不要替后置脚本判 Exact/Coarse/Gap。** industry 必须是候选母图之一。
【BOM 桶】${JSON.stringify(b)}
【候选母图索引(行格式: ID  名称  {身份刻面值})】
${sliceFor(b)}`,
  { schema: MATCH_SCHEMA, label: `match:${b.bucket_id}`, phase: 'Match', model: M.recall },
)))).filter(Boolean)

// ── phase Reconcile:只把争议项(无节点/多材料/边界)交给 Opus 复裁,省钱 ──
phase('Reconcile')
const contested = proposals.filter(p =>
  p.verdict_hint === 'no_node' ||
  /Metal \+|Several components|Steel \+ Alloy/.test((p.inferred_identity || '') + JSON.stringify(p.claimed_facets || {})))
let reconciled = { items: [] }
if (contested.length) {
  reconciled = await agent(
`你是跨行业【边界裁决者】。下面是提名器标为"无节点/多材料/边界争议"的桶。逐个:确认归属母图、给最终 candidate_node_id(或确认确实无节点=结构缺口,candidate_node_id="")、多材料件按主导材料归并。只改这些项,返回修正后的数组 items(每项同 MATCH_SCHEMA 形状)。
争议项: ${JSON.stringify(contested)}`,
    { schema: { type: 'object', required: ['items'], properties: { items: { type: 'array', items: MATCH_SCHEMA } } },
      label: 'reconcile', phase: 'Reconcile', model: M.reason })
}

// 合并:reconciled 覆盖同 bucket_id 的初版提名
const byId = Object.fromEntries(proposals.map(p => [p.bucket_id, p]))
for (const r of (reconciled.items || [])) byId[r.bucket_id] = r
const matches = Object.values(byId)
log(`提名完成: ${matches.length} 桶(其中 ${contested.length} 项经 Opus 复裁)`)

// ── phase Grade:裁决移出 workflow(同 gate.py 的理由),只 log 后置命令 ──
phase('Grade')
const gradeCmd = `python3 scripts/grade_bom_matches.py docs/${VEHICLE}-bom-matches.json ${VEHICLE}`
log('⏭ 裁决走后置确定性脚本(零LLM)。workflow 结束后:')
log(`   1) 把返回的 matches 落盘 docs/${VEHICLE}-bom-matches.json(冻结artifact,保可复现)`)
log(`   2) ${gradeCmd}  → 对真图查facet定 Exact/Coarse/Gap、质量加权、写 graded.json`)

return { vehicle: VEHICLE, n_buckets: BUCKETS.length, n_contested: contested.length, matches, grade_cmd: gradeCmd }
