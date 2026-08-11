# LCA Skeleton Autonomous Production Platform

This repository is the engineering reconstruction of `lca-cornerstone`. It turns
conversation-driven production into a job-driven, replayable local control plane.

The original repository is a read-only migration source. Runtime state, generated
artifacts and releases belong to this repository and are never written back to the
source tree.

## Safety model

- Agents may only produce proposals, verdicts and attestations.
- Deterministic capabilities are registered and executed in isolated workspaces.
- Artifacts are immutable, content-addressed objects with explicit lineage.
- Cross-module communication uses versioned events, not shared-file edits.
- Authority changes require candidate-bound gates and a two-phase release.
- Retry is bounded; repeated or policy-sensitive failures are quarantined as an
  exception package for a human.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
lca-platform init
lca-platform validate
lca-platform reconcile --once
lca-platform wiki-rehearse
lca-platform status
lca-platform supervise var/workspaces/<job>/stage-plan.json
pytest
```

By default local state is stored in `var/state.db`, content-addressed artifacts in
`var/artifacts`, and execution workspaces in `var/workspaces`.

`wiki-rehearse` runs the frozen A017/P031/P003 Phase-2 cohort in an isolated
workspace, persists plan/prepare proof in the Kernel, and intentionally refuses
publish because the fixture contains no fresh external-evidence/Verify attestation.

`supervise` runs a frozen command batch as one bounded stage. Child processes are
waited inside the supervisor (there is no polling API), retries are bounded, model
calls are reserved before launch, and the 101st model call or first reported
context compaction creates a stage-specific `stage-checkpoint-<stage-id>.json`
instead of continuing. The `model_calls` counter is a fail-closed reservation:
for an internally batched command it records the declared worst case, not a
claim that every reserved call was consumed.

## Repository layout

| Path | Responsibility |
|---|---|
| `src/lca_project/kernel` | state, events, CAS, orchestration, repair and release |
| `src/lca_project/contracts` | versioned control-plane protocols |
| `src/lca_project/domains` | graph, Wiki, cross-link, LCA and BOM adapters |
| `capabilities` | deterministic executable inventory and side-effect contracts |
| `workflows` | declarative versioned DAGs |
| `agents` | frozen agent definitions, prompts and output contracts |
| `policies` | project invariants, retry, gate and autonomy policies |
| `skills` | thin intent-routing packages; never production state |
| `vendor` | hash-recorded code copied from the read-only source repository |
| `tests` | design-case traceability and automated acceptance tests |

The authoritative design and test baseline are copied under `docs/`. Migrated
assets are listed in `docs/migration-manifest.json` with their source hashes.

## Operating model

`reconcile` compares desired and observed state, creates idempotent Jobs and emits
events. The scheduler grants a fenced lease only when dependencies, budget and
policy permit execution. The executor invokes a registered capability, freezes its
outputs in CAS and dispatches candidate-bound gates. Release then stages a hash-
locked plan, applies it transactionally and performs post-verification. Any drift,
protocol failure or exhausted retry becomes a structured exception instead of a
silent publication.

## Definition of done

`pytest` is the regression gate for the implemented scope. The suite traces all
96 design IDs, but traceability is not execution coverage: the current behavioral
coverage and remaining release blockers are recorded in
`docs/重构实施与验收状态.md`. Full platform acceptance additionally requires every
P0/P1 case to have a real automated/canary runner and evidence artifact. A passing
test run must never be interpreted as universal open-domain LCA correctness.
