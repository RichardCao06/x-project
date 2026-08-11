export const meta = {
  name: 'wiki-ku-provenance',
  description: '模型自报知识+来源 → WebSearch独立核验 → KU+Provenance(仅workflow内的LLM三阶段;蒸馏/组装是main-loop的确定性脚本)',
  phases: [
    { title: 'Extract', detail: '每节点一个agent:产候选断言 + 自报来源/位置/归因置信度(结构化输出)' },
    { title: 'SearchFetch', detail: '每节点一个批量agent:逐断言按自报来源构造查询并抓取候选原文' },
    { title: 'Verify', detail: '每节点一个独立批量agent(与Extract无关联上下文)逐项裁决,默认偏向不确认' },
  ],
}

// ---- 结构化输出 schema ----
const CLAIMS_SCHEMA = {
  type: 'object',
  required: ['node_id', 'claims'],
  properties: {
    node_id: { type: 'string' },
    claims: {
      type: 'array',
      items: {
        type: 'object',
        required: ['claim_text', 'section', 'believed_source', 'attribution_confidence', 'search_query_hint'],
        properties: {
          claim_text: { type: 'string' },
          section: {
            type: 'string',
            enum: [
              '定义与产品身份', '性质与形态', '参考流与交接边界', '规格与相邻节点区分',
              '在系统中的角色', '分类与适用范围', '节点特定采集字段', '区域化补充要求',
              '数据适用状态与缺口', '出处',
              '定义与参考活动', '参考产品与参考单位', '单元过程边界', '技术路线与相邻活动区分',
              '投入产出与脊边对账', '直接排放、废物与监测指标边界',
            ],
          },
          believed_source: { type: 'string' },
          believed_locator: { type: 'string' },
          attribution_confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          search_query_hint: { type: 'string' },
        },
      },
    },
  },
}

const BATCH_FETCH_SCHEMA = {
  type: 'object',
  required: ['node_id', 'items'],
  properties: {
    node_id: { type: 'string' },
    items: {
      type: 'array',
      items: {
        type: 'object',
        required: ['claim_id', 'status'],
        properties: {
          claim_id: { type: 'string' },
          status: { type: 'string', enum: ['found', 'not_found', 'paywalled'] },
          url: { type: 'string' },
          excerpt: { type: 'string' },
        },
      },
    },
  },
}

const BATCH_VERDICT_SCHEMA = {
  type: 'object',
  required: ['node_id', 'items'],
  properties: {
    node_id: { type: 'string' },
    items: {
      type: 'array',
      items: {
        type: 'object',
        required: ['claim_id', 'verdict', 'reasoning'],
        properties: {
          claim_id: { type: 'string' },
          verdict: { type: 'string', enum: ['CONFIRMED', 'CONTRADICTED', 'INSUFFICIENT'] },
          supporting_quote: { type: 'string' },
          reasoning: { type: 'string' },
        },
      },
    },
  },
}

/* DATA-BINDING:START — 由 scripts/prep_node_wiki.py 覆盖本段;下方占位样例供 validate_wiki_workflow.py 校验。
   实测教训:Workflow 的 args 形参不注入全局(name/scriptPath 都不行),故数据必须 embed 进 run-script,见 [[product-system-model]] 备注。 */
const NODES = [
  { "node_id": "P001", "industry": "steel", "industry_cn": "钢铁", "name": "焦炭, 块状",
    "facets": { "base_material": "coke_carbon", "composition_grade": "na_not_applicable", "form_state": "lump_pellet_sinter", "process_form": "standard" },
    "boundary": "foreground" }
]
/* DATA-BINDING:END */

phase('Extract')
const extracted = await parallel(NODES.map(n => () => agent(
  `你是${n.industry_cn || n.industry}行业 LCA 知识撰写者。节点:${n.node_id}「${n.name}」boundary=${n.boundary}\n刻面:${JSON.stringify(n.facets)}\n\n` +
  `节点研究档案:${JSON.stringify(n.dossier || {})}\n\n` +
  `写 ${n.claim_budget ? `恰好 ${n.claim_budget}` : '3-5'} 条这个节点的定性事实断言。` +
  `section 必须从节点研究档案 required_sections 中选择并原样输出；优先覆盖定义、边界、相邻节点区分和区域化采集要求，` +
  `不要自造章节名。` +
  `每条断言必须同时给出:你认为它出自哪个具体来源(标准/数据库/百科全书的名字)、大致章节或位置、` +
  `以及你对这个溯源判断本身的把握(high/medium/low)。**如果你并不确定具体是哪个来源或章节,老实标 low,` +
  `不要为了显得权威而编造精确页码或章节号**。不写任何带计量单位的数字。再给一个供后续检索用的搜索关键词——` +
  `优先包含你认为的具体源名(如"Iron and Steel Production BREF chapter 5"),而不是断言本身的关键词。` +
  `输出 node_id 必须严格等于 "${n.node_id}"，不得加行业前缀。`,
  { label: `extract:${n.node_id}`, phase: 'Extract', schema: CLAIMS_SCHEMA, model: 'sonnet' }
).then(r => (r ? { ...r, node_id: n.node_id, industry: n.industry } : null))))

// 展平成 claim 级列表(纯代码,非 agent 调用——workflow 沙箱无文件系统,这里只是内存里的数组变换)
const allClaims = extracted.filter(Boolean).flatMap(e =>
  (e.claims || []).map((c, i) => ({ ...c, node_id: e.node_id, industry: e.industry, claim_id: `${e.node_id}-${i}` }))
)
log(`Extract 完成:${NODES.length} 节点 → ${allClaims.length} 条候选断言`)

// 批量场景按节点分组：每节点一次 SearchFetch + 一次独立 Verify。
// 这保留逐断言结果协议，同时把 agent 数从 O(claims) 降到 O(nodes)。
const claimGroups = Object.values(allClaims.reduce((acc, claim) => {
  if (!acc[claim.node_id]) acc[claim.node_id] = { node_id: claim.node_id, claims: [] }
  acc[claim.node_id].claims.push(claim)
  return acc
}, {}))

phase('SearchFetch')
const searchedGroups = await parallel(claimGroups.map(group => () => agent(
  `请核验同一节点的以下候选断言:${JSON.stringify(group.claims)}\n\n` +
  `每条断言最多执行 1 次 WebSearch 和 1 次 WebFetch。优先搜索 believed_source 本身；` +
  `只接受 http:// 或 https:// 的真实外部网页/PDF，严禁 file://、localhost、本地仓库文件、搜索摘要或模型自己的说明。` +
  `excerpt 必须是来源中的逐字原文，不得夹带你的判断、纠错、总结或“该断言正确/错误”等元话语。` +
  `必须为每个 claim_id 返回且只返回一项；无法取得逐字原文就报 not_found/paywalled。` +
  `输出 node_id 必须严格等于 "${group.node_id}"。`,
  { label: `search:${group.node_id}`, phase: 'SearchFetch', schema: BATCH_FETCH_SCHEMA, model: 'sonnet' }
).then(r => ({ node_id: group.node_id, items: (r && r.items) || [] }))))

const fetchById = {}
searchedGroups.forEach(group => (group.items || []).forEach(item => { fetchById[item.claim_id] = item }))
const foundGroups = claimGroups.map(group => ({
  node_id: group.node_id,
  items: group.claims
    .map(claim => ({ claim, fetchResult: fetchById[claim.claim_id] || { status: 'not_found' } }))
    .filter(item => item.fetchResult.status === 'found' && item.fetchResult.excerpt &&
      /^https?:\/\//.test(item.fetchResult.url || '')),
})).filter(group => group.items.length)

phase('Verify')
const verifiedGroups = await parallel(foundGroups.map(group => () => agent(
  `你是独立核验员。不得联网搜索，也不知道断言由谁撰写。请逐项判断以下“断言+外部原文”:` +
  `${JSON.stringify(group.items)}\n\n` +
  `原文明确、具体支持断言才给 CONFIRMED；明确相反给 CONTRADICTED；仅话题相关、只支持复合断言的一部分、` +
  `或摘录带有搜索者自己的说明而非原文，给 INSUFFICIENT。CONFIRMED 必须逐字复制 supporting_quote。` +
  `拿不准默认 INSUFFICIENT。必须为每个 claim_id 返回且只返回一项。`,
  { label: `verify:${group.node_id}`, phase: 'Verify', schema: BATCH_VERDICT_SCHEMA, model: 'sonnet' }
).then(r => ({ node_id: group.node_id, items: (r && r.items) || [] }))))

const verdictById = {}
verifiedGroups.forEach(group => (group.items || []).forEach(item => { verdictById[item.claim_id] = item }))
const graded = allClaims.map(claim => {
  const fetchResult = fetchById[claim.claim_id] || { status: 'not_found' }
  const externallyRetrievable = fetchResult.status === 'found' && fetchResult.excerpt &&
    /^https?:\/\//.test(fetchResult.url || '')
  if (!externallyRetrievable) {
    return {
      claim,
      fetchResult,
      verify: { verdict: 'NOT_FOUND', reasoning: '未抓到可核验的外部 http(s) 原文', supporting_quote: '' },
      verification_protocol: {
        independent: true,
        search_label: `search:${claim.node_id}`,
        verify_label: '',
        verify_skipped_reason: 'no_retrievable_external_source',
      },
    }
  }
  const verify = verdictById[claim.claim_id] ||
    { verdict: 'INSUFFICIENT', reasoning: '独立核验未返回该 claim_id', supporting_quote: '' }
  return {
    claim,
    fetchResult,
    verify,
    verification_protocol: {
      independent: true,
      search_label: `search:${claim.node_id}`,
      verify_label: `verify:${claim.node_id}`,
    },
  }
})

const summary = { CONFIRMED: 0, CONTRADICTED: 0, NOT_FOUND: 0, INSUFFICIENT: 0 }
graded.filter(Boolean).forEach(g => { summary[g.verify.verdict] = (summary[g.verify.verdict] || 0) + 1 })
log(`Verify 完成:${JSON.stringify(summary)}`)

return {
  protocol: { version: 'wiki-ku-v1', mode: 'extract' },
  claims: graded.filter(Boolean),
}
