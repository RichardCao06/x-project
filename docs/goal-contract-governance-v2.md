# Goal Contract Governance v2

This package implements the governed self-evolution and production-enforcement slice of the v2 autonomous-production design. It preserves the existing `goal-contract-v1` trajectory and self-repair controllers, then adds the boundary that determines:

- what outcome the system is actually trying to achieve;
- which actions an Agent may execute without a person in the normal path;
- what independent evidence is sufficient to claim completion;
- where the current model, Prompt, tools, Workflow, budget, and input distribution are empirically certified;
- how Goal semantics and governance policies may change without a running Agent moving the goalposts.

The implementation is intentionally fail-closed for autonomous publication. A true Goal may exceed the current Agent capability. In that case the system blocks or enters an honest incomplete state; it does not weaken the Goal merely to increase completion metrics.

## Four immutable governance objects

A single Goal file cannot safely answer four different questions:

| Object | Governing question |
|---|---|
| `goal-contract-v2` | What outcome is valuable, what is forbidden, and which honest terminal states exist? |
| `autonomy-contract-v1` | Which actions may the system perform automatically at each risk level? |
| `assurance-contract-v1` | What independent proof is required before a Goal clause or release may be accepted? |
| `capability-envelope-v1` | Under which runtime, input scope, budget, cohort, and measured error boundary is the implementation certified? |

Every object is immutable by `(kind, contract_id, version)`. Re-registering the same reference with different content is rejected as contract drift.

## Non-compensatory alignment

`GovernanceController.assess_alignment()` evaluates Goal clauses as `hard`, `required`, `optimize`, or `diagnostic`.

Hard and required proof is non-compensatory: more citations, longer prose, or a higher aggregate score cannot offset an identity error, missing provenance, a prohibited outcome, or an unsupported completion claim. A clause marked `proved` must carry a structured proof record containing:

- an immutable artifact reference;
- a SHA-256 certificate hash;
- the evaluator declared by the Assurance Contract;
- all required evidence types;
- distinct producer and evaluator actors when independence is required.

The evaluator emits one of four explicit verdicts:

- `aligned_complete`
- `aligned_incomplete`
- `misaligned`
- `human_judgment_required`

`aligned_incomplete` is a valid aligned result. A Job that truthfully enters `needs_research` or `data_required` can be aligned even though it did not reach `modeling_ready`.

## Immutable Job binding

Before an autonomous Job is eligible to publish, it receives a `job-contract-binding-v1` containing exact Goal, Autonomy, Assurance, and Capability references plus a binding hash. The binding is immutable by `job_id`.

This prevents a running Job from silently adopting a newer Goal, a looser policy, or a different model certification. Goal amendments and policy replacements apply to new bindings. Existing Jobs retain the frozen versions unless a governance owner explicitly suspends one of those versions.

Binding also verifies that:

- all four contracts are active at binding time;
- the Autonomy and Assurance contracts target the selected Goal;
- Assurance covers every hard and required Goal clause;
- all four contracts share a compatible domain scope.

## Goal amendment protocol

An active Goal Contract is never edited in place. The sequence is:

1. register a new draft Goal version;
2. submit `goal-change-proposal-v1`;
3. provide an acceptance-set difference (`newly_allowed`, `newly_blocked`, unchanged samples, and unknowns);
4. statically compare purpose, scope, all clause semantics, prohibited outcomes, terminal states, authority, cohorts, and risk budgets;
5. classify the semantic change and risk;
6. obtain the required authority;
7. activate the new version and supersede the old version.

The controller infers the change class rather than trusting the proposing Agent. Purpose or scope changes are `goal_redirection`; reserved-authority changes and acceptance-set widening are critical; any other semantic change requires human approval; only a structural refactor with no semantic or acceptance-set effect may use policy pre-authorization.

Any change that makes a previously failing sample pass requires `human_goal_owner` approval.

Activation also creates durable reassessment work for affected Job eligibility, historical Alignment Assessments, dependent Autonomy/Assurance compilation, and Capability recertification. The old records are not rewritten. Running Jobs retain their frozen Goal; the reassessment queue governs migration to the new Goal and reevaluation of historical maturity. A human Goal or governance owner must resolve every item with evidence.

## Non-Goal policy evolution and emergency suspension

Autonomy, Assurance, and Capability contracts are also immutable. `replace_active_contract()` activates a new version and supersedes the old version only when a `human_governance_owner` supplies a rationale and evidence references.

`suspend_contract()` is the emergency revocation path for a specific immutable version. It is useful when a newly discovered false pass invalidates a Capability certification or an Assurance defect makes the existing proof policy unsafe. A suspended bound contract blocks subsequent autonomous eligibility checks even for an already-running Job.

Goal suspension requires `human_goal_owner`; all other contract suspensions require `human_governance_owner`.

## Capability-conditioned autonomy

A Capability Envelope has an explicit certification status:

- `shadow`: evaluate behavior but never authorize autonomous action;
- `certified`: eligible for autonomous action within the declared boundary;
- `suspended`: revoked pending investigation or recertification;
- `expired`: no longer eligible.

The repository example is deliberately `shadow`; a governance owner must activate a separately evidenced `certified` version before enforced autonomous publication. `certify_capability()` computes coverage, selective risk, a one-sided 95% Wilson upper bound, and abstention recall from an immutable Cohort. A passing report is signed by the project Proof Authority and can replace the active Capability Envelope only when the independent evaluator and human authorizer are distinct.

Online outcomes are append-only `capability_observations`. A new false pass creates a pending recertification invalidation for the exact Capability version. Enforced Job admission and publication then fail closed until governed reassessment completes.

`check_autonomy()` combines all four contracts. Authorization requires:

- the action is present and not forbidden;
- the Job risk is below the action ceiling;
- no reserved human authority is requested;
- the runtime model, Prompt, toolset, and Workflow exactly match the certified fingerprint;
- the input lies within the certified scope and does not use empty scope values;
- the Capability selective-risk upper bound is below the Goal ceiling;
- the Capability status is `certified` and its validity period has not expired;
- the bound contracts are not suspended or expired;
- every action requirement has valid evidence.

Security-sensitive requirements such as `release_attestation`, `rollback`, `independent_evaluator`, `immutable_evidence`, and `proof_contract` cannot be satisfied by passing a string. They require an artifact reference, issuer identity, and a valid SHA-256 certificate hash. When an evidence payload is embedded, the hash is checked against that payload. In `enforced` mode the record must also carry a registered HMAC receipt bound to the project Proof Authority, CAS, SQLite receipt table, and append-only event ledger. `alignment_assessment` is always derived from the persisted assessment for the exact Job binding and cannot be self-asserted by the caller.

Every eligibility result is persisted with its inputs, binding hash, contract hashes, evidence hashes, decision, and reasons.

## Governed release adapter

`GovernedReleaseManager` composes the existing hash-locked `ReleaseManager` instead of replacing it. It supports three migration modes:

| Mode | Behavior |
|---|---|
| `disabled` | Preserve the existing release path. |
| `shadow` | Evaluate and persist governance, but allow the existing release to proceed. |
| `enforced` | Refuse `apply()` unless the publish action is authorized. |

The adapter generates deterministic `release_attestation` and `rollback` evidence from the staged manifest, expected-current hashes, Job binding, destination, and release ID. Callers cannot override these controller-owned evidence records. An append-oriented governance record is written outside the immutable staged candidate directory for every evaluated, blocked, failed, or applied release.

Example integration:

```python
from lca_project.kernel.governed_release import GovernedReleaseManager
from lca_project.kernel.release import ReleaseManager

base = ReleaseManager(release_root, proof_authority=proof_authority)
release = GovernedReleaseManager(base, governance, mode="shadow")

staged = release.stage(files, expected_current=current, gate_results=gates)
release.apply(
    staged,
    destination,
    job_id=job_id,
    risk="low",
    runtime_fingerprint=runtime_fingerprint,
    input_scope=input_scope,
)
```

Rollout should begin in `shadow`, compare decisions on fixed Golden/Mutation/Cohort samples, certify the Capability Envelope, and only then switch low-risk publication to `enforced`.

`ControlPlane` now loads `config/governance-v2.json`, registers the configured policy bundle, and automatically binds matching production Jobs. Mapping is exact by Workflow version; wildcard or unmapped Jobs are rejected before persistence in `enforced` mode. Worker startup checks the binding and revocation state. The reviewed Wiki tail now derives claim coverage, executes Go/No-Go and G10, performs the reviewed workspace apply, and prepares the bundle/viewers before handing the exact snapshot to the job-driven release service. That service issues candidate-bound G10/G11 receipts and applies through the compare-and-swap `ReleaseManager`; `GovernanceRuntime.wrap_release_manager()` adds the controller-owned, signed release and rollback evidence. `enforced` mode still refuses the authoritative apply without a complete Alignment Assessment.

Configured references are stable contract identities, not permanently pinned obsolete versions. After an authorized replacement, the runtime follows the persisted `superseded_by` chain only when kind and contract ID remain unchanged, then binds new Jobs to the active replacement. Existing Jobs keep their immutable old binding. A missing replacement, identity change, cycle, suspension, or incoherent Goal/Autonomy/Assurance bundle fails closed.

Production release evaluation does not accept model, Prompt, toolset, or scope claims from extra request fields. At Job binding time the control plane hashes the versioned Agent definitions, Prompts, Skill route, production Policy, Workflow, and referenced Capability manifests; it combines that inventory with the request fields permitted by the Capability Envelope and persists a `governed-release-context-v1`. The Worker recomputes and compares this context before publication, so repository drift or Job payload drift invalidates eligibility instead of becoming `unknown` or caller-asserted evidence.

`policies/wiki-capability-envelope-v1.1.json` is the shadow replacement candidate that names this controller-derived runtime and the schema-valid A039 reviewed input slice. The original `1.0.0` document remains byte-for-byte compatible with already registered state; operators must use the governed replacement command before independently certifying `1.1.0` as a new `1.2.0` contract. The rollout never mutates an immutable contract version in place.

Runtime readiness is evaluated per configured Workflow. Every reported bundle must resolve to active, type-correct and mutually coherent contracts; its Capability must be certified, unexpired, free of pending drift invalidation, and match the current repository fingerprint. Readiness no longer depends on whether an unrelated historical Job binding happens to exist.

## Persistence

Migration 13 installs the core governance records. Migration 14 adds reassessment, independent Cohort certification, and online drift records. Contract payload versions and Job bindings are immutable; lifecycle events, approvals, assessments, eligibility decisions, certifications, and observations are append-oriented:

- `governance_contracts`
- `contract_lifecycle_events`
- `goal_change_proposals`
- `governance_approvals`
- `job_contract_bindings`
- `alignment_assessments`
- `autonomy_eligibility_assessments`
- `governance_reassessments`
- `capability_certifications`
- `capability_observations`

The runtime installs the same idempotent schema for old fixtures and pre-v2 databases. Production schema history remains recorded in `schema_migrations`.

## CLI

The companion command keeps governance actions separate from Worker execution:

```bash
lca-governance --root . register policies/wiki-goal-contract-v2.json \
  --activate --actor lca-owner --role human_goal_owner

lca-governance --root . register policies/wiki-autonomy-contract-v1.json \
  --activate --actor platform-owner --role human_governance_owner

lca-governance --root . replace-contract \
  capability://wiki-node-production-capability@1.0.0 \
  policies/wiki-capability-envelope-v1.1.json \
  --actor platform-owner --role human_governance_owner \
  --rationale "Bind certification to the controller-derived production runtime" \
  --evidence change://p0-runtime-fingerprint

lca-governance --root . suspend-contract \
  capability://wiki-node-production-capability@1.1.0 \
  --actor platform-owner --role human_governance_owner \
  --reason "New false pass invalidated certification" \
  --evidence incident://false-pass/42

lca-governance --root . bind-job job_A039 \
  --goal goal://wiki-node-goal@2.0.0 \
  --autonomy autonomy://wiki-node-autonomy@1.0.0 \
  --assurance assurance://wiki-node-assurance@1.0.0 \
  --capability capability://wiki-node-production-capability@1.1.0

lca-governance --root . assess-alignment job_A039 \
  --clause-results clause-results.json \
  --prohibited-outcomes prohibited-outcomes.json \
  --terminal-state needs_research --capability-match

lca-governance --root . check-autonomy job_A039 publish --risk low \
  --runtime runtime-fingerprint.json \
  --input-scope input-scope.json \
  --requirement-evidence release-requirement-evidence.json

lca-governance --root . certify-capability \
  capability://wiki-node-production-capability@1.1.0 cohort.json \
  --target-version 1.2.0 --cohort-id wiki-node-governance@2 \
  --evaluator independent-assurance --authorizer platform-owner \
  --valid-until 2027-08-19T00:00:00Z

lca-governance --root . observe-capability \
  capability://wiki-node-production-capability@1.2.0 production-escape-42 \
  --outcome incorrect --actor post-release-monitor

lca-governance --root . reassessments
lca-governance --root . readiness
```

## Deliberate scope of this change

The checked-in runtime configuration maps only `wiki-node-production@9` and starts in `shadow`. Other Workflows remain unchanged in shadow mode and are rejected if the operator switches to `enforced` without adding exact contract bundles. Moving to enforcement still requires a separately certified Capability replacement, zero pending reassessment work, and a green machine-readable `readiness` result. A successful governed replacement advances the configured contract identity automatically; editing the config merely to copy the new version is neither required nor sufficient.
