export const meta = {
  name: 'wiki-ku-provenance-repair',
  description: '修复模式:对已有wiki正文的老断言做 SearchFetch→Verify(跳过Extract,老标签已自带声明来源,不用模型重新自报)',
  phases: [
    { title: 'SearchFetch', detail: '每节点一个批量agent:逐条按现有标签的登记来源名构造查询并抓取候选原文' },
    { title: 'Verify', detail: '每节点一个独立批量agent逐项裁决抓到的原文,默认偏向不确认' },
  ],
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

/* DATA-BINDING:START — 由 scripts/prep_node_wiki.py --mode repair 覆盖本段;下方占位样例供 validate_wiki_workflow.py 校验。
   修复模式喂"已有正文里、还挂着 seed-unverified 老标签的断言";已 verified(ku-*)的断言 prep 会自动跳过(幂等)。 */
const CLAIMS = [
  { "claim_id": "P001-example", "node_id": "P001", "industry": "steel", "section": "性质与形态", "old_tags": ["bref-is-2013"],
    "claim_text": "块状焦炭的宏观形态为不规则多孔块体，具有高碳含量、低挥发分、高抗压强度与良好的高温稳定性等定性特征。",
    "believed_source": "Iron & Steel Production BREF (Best Available Techniques Reference Document) — European Commission JRC / IPPC" }
]
/* DATA-BINDING:END */

// ``pipeline`` 保留为 Workflow DSL 协议标记；批量运行按节点分组，避免每条断言派两个 agent。
const claimGroups = Object.values(CLAIMS.reduce((acc, claim) => {
  if (!acc[claim.node_id]) acc[claim.node_id] = { node_id: claim.node_id, claims: [] }
  acc[claim.node_id].claims.push(claim)
  return acc
}, {}))
const pipelineMode = 'node-batched'

phase('SearchFetch')
const searchedGroups = await parallel(claimGroups.map(group => () => agent(
  `请核验同一节点现有 wiki 的以下老断言:${JSON.stringify(group.claims)}\n\n` +
  `每条断言最多执行 1 次 WebSearch 和 1 次 WebFetch。优先搜索 believed_source 本身；` +
  `只接受 http:// 或 https:// 的真实外部网页/PDF，严禁 file://、localhost、本地仓库文件、搜索摘要或模型自己的说明。` +
  `excerpt 必须是来源中的逐字原文，不得夹带你的判断、纠错、总结或“该断言正确/错误”等元话语。` +
  `必须为每个 claim_id 返回且只返回一项；无法取得逐字原文就报 not_found/paywalled。`,
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
  `你是独立核验员。不得联网搜索，也不知道断言由谁撰写。请逐项判断以下“老断言+外部原文”:` +
  `${JSON.stringify(group.items)}\n\n` +
  `原文明确、具体支持断言才给 CONFIRMED；明确相反给 CONTRADICTED；仅话题相关、只支持复合断言的一部分、` +
  `或摘录带有搜索者自己的说明而非原文，给 INSUFFICIENT。CONFIRMED 必须逐字复制 supporting_quote。` +
  `拿不准默认 INSUFFICIENT。必须为每个 claim_id 返回且只返回一项。`,
  { label: `verify:${group.node_id}`, phase: 'Verify', schema: BATCH_VERDICT_SCHEMA, model: 'sonnet' }
).then(r => ({ node_id: group.node_id, items: (r && r.items) || [] }))))

const verdictById = {}
verifiedGroups.forEach(group => (group.items || []).forEach(item => { verdictById[item.claim_id] = item }))
const graded = CLAIMS.map(claim => {
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
  protocol: { version: 'wiki-ku-v1', mode: 'repair' },
  claims: graded.filter(Boolean),
}
