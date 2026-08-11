export const meta = {
  name: 'wiki-ku-nominate',
  description: '生产空页只做断言与来源提名；禁止联网。Search/Fetch 由确定性 Python 层接管。',
  phases: [
    { title: 'Extract', detail: '每节点一个 agent：外部事实槽提供少量证据锚点，建模判断槽生成丰富解释与 LCA 推理；无 WebSearch/WebFetch。' },
  ],
}

const NOMINATION_SCHEMA = {
  type: 'object',
  required: ['node_id', 'claims'],
  properties: {
    node_id: { type: 'string' },
    claims: {
      type: 'array',
      items: {
        type: 'object',
        required: [
          'requirement_id', 'section', 'claim_text', 'claim_kind', 'believed_source',
          'believed_locator', 'attribution_confidence',
        ],
        properties: {
          requirement_id: { type: 'string' },
          section: { type: 'string' },
          claim_text: { type: 'string' },
          claim_kind: { type: 'string', enum: ['external_fact', 'internal_graph_fact', 'modeling_judgment', 'evidence_gap'] },
          believed_source: { type: 'string' },
          believed_locator: { type: 'string' },
          attribution_confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
      },
    },
  },
}

/* DATA-BINDING:START — 由 scripts/wiki_batch.py 覆盖；这里只是协议校验样例。 */
const NODES = [
  {
    "node_id": "P001",
    "node_type": "product",
    "industry": "steel",
    "industry_cn": "钢铁",
    "name": "焦炭, 块状",
    "facets": {"base_material": "coke_carbon"},
    "boundary": "foreground",
    "dossier": {
      "required_sections": [
        "定义与产品身份", "性质与形态", "参考流与交接边界", "规格与相邻节点区分",
        "在系统中的角色", "分类与适用范围", "节点特定采集字段", "区域化补充要求",
        "数据适用状态与缺口", "出处"
      ]
    }
  }
]
/* DATA-BINDING:END */

phase('Extract')
const nominated = await parallel(NODES.map(n => () => agent(
  `你是${n.industry_cn || n.industry}行业 LCA 节点研究提名员。你不得联网搜索，也不得声称已经核验来源。\n` +
  `节点:${n.node_id}「${n.name}」 boundary=${n.boundary}\n刻面:${JSON.stringify(n.facets)}\n` +
  `冻结档案:${JSON.stringify(n.dossier || {})}\n\n` +
  `严格按 dossier.claim_requirements 的顺序：external_fact 的每个 requirement_id 连续返回 1–2 条事实锚点，` +
  `modeling_judgment 返回 2–4 条互不重复、节点特异的解释或 LCA 判断，其他 requirement 返回 1–4 条；` +
  `requirement_id、section、claim_kind 必须逐条原样复制，不得遗漏、增加或改名。` +
  `external_fact 必须给出直接支持该节点身份或过程边界的具体一手来源全名与大致 locator；` +
  `internal_graph_fact 的 believed_source 固定写 LCA-CORNERSTONE_GRAPH；` +
  `modeling_judgment/evidence_gap 的 believed_source 固定写 INTERNAL_MODELING_JUDGMENT。` +
  `modeling_judgment 是正式正文内容，可使用通用领域知识解释机理、边界、系统角色、采集、分配、` +
  `区域化与不确定性；不得冒充法规、机构立场、具名装置事实、精确工艺参数或定量值。` +
  `不得用机箱、上游组件、宽泛行业分类或相邻工艺的来源冒充目标节点身份来源。` +
  `禁止给 LCI 数值，禁止把建模判断说成已核实，禁止编造精确页码。` +
  `输出 node_id 必须严格等于 "${n.node_id}"。`,
  {
    label: `nominate:${n.node_id}`, phase: 'Extract', schema: NOMINATION_SCHEMA,
    model: 'gpt-5.6-terra', effort: 'medium',
  }
).then(r => ({
  node_id: n.node_id, industry: n.industry,
  node_identity: {
    display_name: n.name, node_type: n.node_type,
    facets: n.facets || {}, boundary: n.boundary,
  },
  claims: (r && r.claims) || [],
}))))

const claims = nominated.flatMap(group => group.claims.map((claim, index) => ({
  ...claim,
  node_id: group.node_id,
  industry: group.industry,
  node_identity: group.node_identity,
  claim_id: `${group.node_id}-${index}`,
})))

return {
  protocol: { version: 'wiki-ku-nomination-v2', mode: 'extract' },
  claims,
}
