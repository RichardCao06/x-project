# Governed SCM publication for autonomous repairs

`SystemRepairAgent` keeps diagnosis and code generation separate from source-control
credentials. The coding Agent still works in a Git-free snapshot. After sandbox,
shadow, and canary validation pass, `SystemRepairScmPublisher` performs the repository
operations from the control plane.

## Lifecycle

1. Queueing a system repair creates or reuses a GitHub Issue keyed by the source
   `deviation_id` (or by `repair_run_id` when no deviation is bound).
2. A validated repair delta is three-way merged into an isolated Git worktree based
   on the configured remote base branch (`main` in the checked-in policy).
3. The publisher creates `autofix/system-repair/<repair-id>`, commits only the bound
   changed files, and adds the repair run, source Job, deviation, and `patch_hash` as
   commit trailers.
4. The branch is pushed and a Draft PR is created or reused. The PR links the Issue
   and includes risk, changed files, and validation results.
5. URLs, commit SHA, branch, status, errors, and publication timestamps are stored in
   `system_repair_scm_publications` and copied into the repair payload shown by the
   Dashboard.

The publisher never merges a PR. Merge authority remains outside the coding Agent.

## Safety boundaries

- The worktree is rooted under `var/system-repairs/<repair_run_id>/scm-worktree`; the
  user's current working tree is not staged or committed.
- By default, the configured remote base must be an ancestor of the running `HEAD`.
  A normal development branch ahead of `main` is therefore publishable, while a stale
  or unrelated source revision is rejected.
- Each changed file's pre-repair hash must still match the source-bound coding
  baseline. The publisher applies only the Agent's before/after delta to `main` with a
  three-way merge, so pre-existing branch or dirty-tree edits are not captured. A real
  overlap is persisted as an explicit merge conflict instead of silently broadening
  the PR.
- Issue, branch, and PR identifiers are deterministic and deduplicated. A retry after
  a successful push can resume PR creation from the already-bound commit.
- Provider failures are persisted as `issue_deferred` or `publication_deferred` and
  retried by the autonomous Supervisor after a bounded five-minute delay. The
  checked-in policy sets `required_for_promotion=true`, so a missing Draft PR targeting
  `main` sends the repair to `awaiting_scm_publication` before any live promotion. A
  required policy that disables Draft PR creation is rejected during startup.
- Set `LCA_DISABLE_SYSTEM_REPAIR_SCM=1` as an operational kill switch.

## Configuration

The checked-in policy is [`config/system-repair-scm.json`](../config/system-repair-scm.json).
It contains no credentials; Git uses the configured remote and GitHub operations use
the authenticated `gh` CLI.

To retry a publication that is configured as required:

```shell
lca-platform --root /path/to/project system-repair publish <repair_run_id>
```

Approval of medium/high-risk promotion remains a separate action:

```shell
lca-platform --root /path/to/project system-repair approve <repair_run_id>
```
