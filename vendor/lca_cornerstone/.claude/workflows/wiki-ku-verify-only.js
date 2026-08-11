export const meta = {
  name: 'wiki-ku-verify-only',
  description: '只消费确定性 Search/Fetch evidence 的独立逐断言裁决；没有 SearchFetch phase，禁止 WebSearch/WebFetch',
  phases: [
    { title: 'Verify', detail: '独立核验冻结的 URL、原文摘录与 content hash；不得联网或补充证据' },
  ],
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
        required: ['claim_id', 'verdict', 'evidence_id', 'node_alignment', 'supporting_quote', 'reasoning'],
        properties: {
          claim_id: { type: 'string' },
          verdict: { type: 'string', enum: ['CONFIRMED', 'CONTRADICTED', 'INSUFFICIENT'] },
          evidence_id: { type: 'string' },
          node_alignment: { type: 'string', enum: ['EXACT', 'ADJACENT', 'UNRELATED'] },
          supporting_quote: { type: 'string' },
          reasoning: { type: 'string' },
        },
      },
    },
  },
}

/* DATA-BINDING:START */
const EVIDENCE = {
  "protocol": { "version": "wiki-source-evidence-v1", "kind": "claim-evidence", "mode": "repair" },
  "claims": [{
    "claim": {
      "claim_id": "P001-example", "node_id": "P001", "industry": "steel", "section": "性质与形态",
      "claim_text": "块状焦炭是多孔块体。", "claim_kind": "external_fact",
      "node_identity": {"display_name":"焦炭, 块状","node_type":"product","facets":{"base_material":"coke_carbon"},"boundary":"foreground"},
      "believed_source": "European Commission Iron and Steel BREF"
    },
    "query": { "query_id": "example", "text": "\"European Commission Iron and Steel BREF\"", "source_first": true },
    "candidates": [{
      "evidence_id": "ev-example", "status": "fetched", "url": "https://example.org/bref",
      "excerpt": "Coke is a porous solid.", "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }]
  }],
  "usage": { "network_queries": 1, "network_fetches": 1, "cache_hits": 0 },
  "hard_limits": { "max_searches": 1, "max_fetches": 1, "max_candidates_per_claim": 1 },
  "budget_exceeded": false,
  "compliance": { "all_evidence_compliant": true }
}
/* DATA-BINDING:END */

if (!EVIDENCE.protocol || EVIDENCE.protocol.version !== 'wiki-source-evidence-v1') {
  throw new Error('evidence protocol mismatch')
}

// Only items with already-fetched bytes enter an agent. There is intentionally
// no SearchFetch phase/agent in this Workflow. The Verify prompt explicitly
// forbids WebSearch/WebFetch and requires decisions from the frozen evidence.
const ready = EVIDENCE.claims.filter(item => Array.isArray(item.candidates) && item.candidates.length)
const groups = Object.values(ready.reduce((acc, item) => {
  const nodeId = item.claim.node_id
  if (!acc[nodeId]) acc[nodeId] = { node_id: nodeId, items: [] }
  acc[nodeId].items.push(item)
  return acc
}, {}))

phase('Verify')
const verifiedGroups = await parallel(groups.map(group => () => agent(
  `你是无联网权限的独立核验员。你不对断言作者的对错负声誉责任。` +
  `严禁使用 WebSearch、WebFetch、浏览器或模型记忆补充证据；只能阅读下列冻结 evidence：` +
  `${JSON.stringify(group.items)}\n\n` +
  `逐 claim_id 裁决。原文完整、具体支持整条断言才给 CONFIRMED；明确相反给 CONTRADICTED；` +
  `先依据 node_identity 判断证据对象与目标节点是否 EXACT；机箱与刀片服务器模块、裸板与PCBA、` +
  `上游组件与整机、相邻工艺等只能判 ADJACENT。node_alignment 非 EXACT 时不得给 CONFIRMED。` +
  `仅话题相关、只支持复合断言一部分或拿不准，一律 INSUFFICIENT。` +
  `evidence_id 必须取自该 claim 的候选；supporting_quote 必须逐字出现在对应 excerpt。` +
  `不得遗漏、增加或改写 claim_id。`,
  {
    label: `verify:${group.node_id}`, phase: 'Verify', schema: BATCH_VERDICT_SCHEMA,
    model: 'gpt-5.6-sol', effort: 'medium',
  }
).then(result => ({ node_id: group.node_id, items: (result && result.items) || [] }))))

const verdictById = {}
verifiedGroups.forEach(group => (group.items || []).forEach(item => { verdictById[item.claim_id] = item }))

const claims = EVIDENCE.claims.map(entry => {
  const claim = entry.claim
  const candidates = Array.isArray(entry.candidates) ? entry.candidates : []
  if (!candidates.length) {
    const internalJudgment = entry.disposition === 'internal_modeling_judgment'
    return {
      claim,
      fetchResult: { status: 'not_found' },
      verify: {
        verdict: 'NOT_FOUND', node_alignment: 'EXACT', supporting_quote: '',
        reasoning: internalJudgment
          ? '该项是 INTERNAL_MODELING_JUDGMENT，不属于外部检索断言，确定性安全降级为 draft'
          : '确定性 Search/Fetch evidence 中无可核验外部原文',
      },
      verification_protocol: {
        independent: true,
        search_label: `search:deterministic:${claim.node_id}`,
        verify_label: '',
        verify_skipped_reason: internalJudgment
          ? 'internal_modeling_judgment'
          : 'no_retrievable_external_source',
      },
    }
  }

  const proposed = verdictById[claim.claim_id] || {
    verdict: 'INSUFFICIENT', evidence_id: candidates[0].evidence_id,
    node_alignment: 'UNRELATED', supporting_quote: '', reasoning: '独立 Verify 未返回该 claim_id',
  }
  const selected = candidates.find(candidate => candidate.evidence_id === proposed.evidence_id)
  const quote = String(proposed.supporting_quote || '')
  const quoteValid = selected && quote && String(selected.excerpt || '').includes(quote)
  const decisive = proposed.verdict === 'CONFIRMED' || proposed.verdict === 'CONTRADICTED'
  const alignmentValid = proposed.node_alignment === 'EXACT'
  const valid = selected && (!decisive || (quoteValid && alignmentValid))
  const candidate = selected || candidates[0]
  const verify = valid ? proposed : {
    verdict: 'INSUFFICIENT', node_alignment: proposed.node_alignment || 'UNRELATED', supporting_quote: '',
    reasoning: `Verify 返回的 evidence_id 或逐字 quote 无法在冻结 evidence 中确定性复核：${proposed.reasoning || ''}`,
  }
  return {
    claim,
    fetchResult: {
      status: 'found', url: candidate.url, excerpt: candidate.excerpt,
      content_sha256: candidate.content_sha256, evidence_id: candidate.evidence_id,
    },
    verify,
    verification_protocol: {
      independent: true,
      search_label: `search:deterministic:${claim.node_id}`,
      verify_label: `verify:${claim.node_id}`,
      web_search_allowed: false,
      evidence_protocol: EVIDENCE.protocol.version,
    },
  }
})

const summary = { CONFIRMED: 0, CONTRADICTED: 0, NOT_FOUND: 0, INSUFFICIENT: 0 }
claims.forEach(row => { summary[row.verify.verdict] = (summary[row.verify.verdict] || 0) + 1 })
log(`Verify-only 完成:${JSON.stringify(summary)}`)

return {
  protocol: { version: 'wiki-ku-v1', mode: EVIDENCE.protocol.mode },
  claims,
}
