"""Governed source-control publication for validated system repairs.

The coding Agent never receives Git credentials.  This boundary owns the
repository Issue, isolated repair branch, commit, push, and Draft PR after the
ordinary sandbox/shadow/canary proofs have passed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable

from ...control import ControlPlane
from ..state import utcnow
from .store import canonical, digest


CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


class ScmPublicationError(RuntimeError):
    """A validated patch could not be published through the governed SCM boundary."""


@dataclass(frozen=True)
class ScmPolicy:
    enabled: bool = False
    provider: str = "github"
    repository: str = ""
    remote: str = "origin"
    base_branch: str = "main"
    branch_prefix: str = "autofix/system-repair"
    create_issues: bool = True
    create_draft_prs: bool = True
    required_for_promotion: bool = False
    require_base_head_match: bool = True


class SystemRepairScmPublisher:
    """Publish system-repair evidence without exposing Git to the coding Agent."""

    CONFIG = "config/system-repair-scm.json"
    _REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    _REMOTE = re.compile(r"^[A-Za-z0-9_.-]+$")
    _BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

    def __init__(self, root: str | Path, control: ControlPlane | None = None, *,
                 runner: CommandRunner | None = None) -> None:
        self.root = Path(root).resolve()
        self.control = control or ControlPlane(self.root)
        self.state = self.control.state
        self.runner = runner or self._default_runner
        self.policy = self._load_policy()

    @staticmethod
    def _default_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, cwd=cwd, text=True, capture_output=True, check=False,
            timeout=180,
        )

    def _load_policy(self) -> ScmPolicy:
        path = self.root / self.CONFIG
        if not path.is_file():
            return ScmPolicy()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != "system-repair-scm-v1":
            raise ValueError("system repair SCM config must be system-repair-scm-v1")
        policy = ScmPolicy(
            enabled=bool(value.get("enabled")),
            provider=str(value.get("provider") or "github"),
            repository=str(value.get("repository") or ""),
            remote=str(value.get("remote") or "origin"),
            base_branch=str(value.get("base_branch") or "main"),
            branch_prefix=str(value.get("branch_prefix") or "autofix/system-repair").rstrip("/"),
            create_issues=bool(value.get("create_issues", True)),
            create_draft_prs=bool(value.get("create_draft_prs", True)),
            required_for_promotion=bool(value.get("required_for_promotion", False)),
            require_base_head_match=bool(value.get("require_base_head_match", True)),
        )
        if policy.provider != "github":
            raise ValueError(f"unsupported system repair SCM provider: {policy.provider}")
        if policy.enabled and not self._REPOSITORY.fullmatch(policy.repository):
            raise ValueError("enabled SCM policy requires repository as owner/name")
        if not self._REMOTE.fullmatch(policy.remote):
            raise ValueError("invalid SCM remote name")
        if not self._BRANCH.fullmatch(policy.base_branch):
            raise ValueError("invalid SCM base branch")
        if not self._BRANCH.fullmatch(policy.branch_prefix):
            raise ValueError("invalid SCM branch prefix")
        if policy.enabled and policy.required_for_promotion and not policy.create_draft_prs:
            raise ValueError(
                "required system repair SCM promotion must create Draft PRs"
            )
        return policy

    @property
    def enabled(self) -> bool:
        return self.policy.enabled and os.environ.get(
            "LCA_DISABLE_SYSTEM_REPAIR_SCM", ""
        ).lower() not in {"1", "true", "yes"}

    def get(self, repair_run_id: str) -> dict[str, Any] | None:
        row = self.state._connection().execute(
            "SELECT * FROM system_repair_scm_publications WHERE repair_run_id=?",
            (repair_run_id,),
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def _record(self, repair_run_id: str, *, status: str, payload: dict[str, Any],
                error: str | None = None, **fields: Any) -> dict[str, Any]:
        existing = self.get(repair_run_id)
        now = utcnow()
        publication_id = str(
            (existing or {}).get("publication_id")
            or "scp_" + digest({"repair_run_id": repair_run_id,
                                "provider": self.policy.provider})[:32]
        )
        merged = {**((existing or {}).get("payload") or {}), **payload}
        columns = {
            "publication_id": publication_id,
            "repair_run_id": repair_run_id,
            "provider": self.policy.provider,
            "status": status,
            "repository": self.policy.repository,
            "remote_name": self.policy.remote,
            "base_branch": self.policy.base_branch,
            "head_branch": fields.get("head_branch", (existing or {}).get("head_branch")),
            "commit_sha": fields.get("commit_sha", (existing or {}).get("commit_sha")),
            "issue_number": fields.get("issue_number", (existing or {}).get("issue_number")),
            "issue_url": fields.get("issue_url", (existing or {}).get("issue_url")),
            "pr_number": fields.get("pr_number", (existing or {}).get("pr_number")),
            "pr_url": fields.get("pr_url", (existing or {}).get("pr_url")),
            "payload": canonical(merged),
            "last_error": error,
            "created_at": str((existing or {}).get("created_at") or now),
            "updated_at": now,
        }
        names = tuple(columns)
        with self.state.transaction() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO system_repair_scm_publications"
                f"({','.join(names)}) VALUES({','.join('?' for _ in names)})",
                tuple(columns[name] for name in names),
            )
        return self.get(repair_run_id) or columns

    def _run(self, command: list[str], *, cwd: Path | None = None,
             ok: set[int] | None = None) -> subprocess.CompletedProcess[str]:
        completed = self.runner(command, cwd or self.root)
        allowed = ok or {0}
        if completed.returncode not in allowed:
            detail = (completed.stderr or completed.stdout or "command failed").strip()[-2000:]
            raise ScmPublicationError(f"{command[0]} command failed: {detail}")
        return completed

    @staticmethod
    def _number_from_url(url: str) -> int | None:
        tail = url.strip().rstrip("/").rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else None

    @staticmethod
    def _created_url(stdout: str, resource: str) -> str:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines or not re.match(r"^https?://", lines[-1]):
            raise ScmPublicationError(f"GitHub did not return a {resource} URL")
        return lines[-1]

    @staticmethod
    def _safe_changed_files(sandbox: Path, changed_files: list[str]) -> list[str]:
        checked: list[str] = []
        for raw in changed_files:
            path = PurePosixPath(str(raw))
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise ScmPublicationError(f"unsafe repair path: {raw}")
            source = sandbox.joinpath(*path.parts)
            if not source.is_file() or source.is_symlink():
                raise ScmPublicationError(f"repair source is not a regular file: {raw}")
            checked.append(path.as_posix())
        if not checked:
            raise ScmPublicationError("SCM publication requires changed files")
        return sorted(set(checked))

    @staticmethod
    def _regular_file_hash(path: Path) -> str | None:
        if not path.exists():
            return None
        if not path.is_file() or path.is_symlink():
            raise ScmPublicationError(f"SCM repair path is not a regular file: {path}")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _apply_repair_delta(
        self, *, worktree: Path, sandbox: Path, safe_files: list[str],
        base_hashes: dict[str, str | None],
    ) -> list[dict[str, Any]]:
        """Apply only the coding Agent's delta onto the fetched PR base.

        The sandbox was copied from the live source tree before coding.  The
        source tree therefore remains the immutable merge base for this
        publication attempt as long as its hash still matches ``base_hashes``.
        A three-way file merge prevents pre-existing branch or dirty-tree
        changes from being smuggled into a PR targeting ``main``.
        """
        merges: list[dict[str, Any]] = []
        for relative in safe_files:
            expected_base = base_hashes.get(relative)
            live_base = self.root / relative
            sandbox_after = sandbox / relative
            target = worktree / relative
            actual_base = self._regular_file_hash(live_base)
            if actual_base != expected_base:
                raise ScmPublicationError(
                    f"repair baseline for {relative} changed after coding; "
                    "regenerate the repair against the current source revision"
                )
            target_hash = self._regular_file_hash(target)
            if target_hash == actual_base:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sandbox_after, target)
                merges.append({"path": relative, "mode": "exact_base"})
                continue
            if actual_base is None:
                if target_hash is not None:
                    raise ScmPublicationError(
                        f"new repair file conflicts with configured main: {relative}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sandbox_after, target)
                merges.append({"path": relative, "mode": "new_file"})
                continue
            if target_hash is None:
                raise ScmPublicationError(
                    f"repair base file is absent from configured main: {relative}"
                )
            with tempfile.TemporaryDirectory(prefix="system-repair-merge-") as raw:
                merge_dir = Path(raw)
                current = merge_dir / "current"
                base = merge_dir / "base"
                other = merge_dir / "repair"
                shutil.copy2(target, current)
                shutil.copy2(live_base, base)
                shutil.copy2(sandbox_after, other)
                merged = self._run(
                    ["git", "merge-file", str(current), str(base), str(other)],
                    # merge-file returns the number of conflict sections, not
                    # merely a boolean 0/1 status (capped at 127).
                    cwd=worktree, ok=set(range(128)),
                )
                if merged.returncode != 0:
                    raise ScmPublicationError(
                        f"repair delta conflicts with configured main: {relative}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, target)
                shutil.copymode(sandbox_after, target)
            merges.append({"path": relative, "mode": "three_way"})
        return merges

    def _issue_context(self, repair: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        deviation_id = str(candidate.get("source_deviation_id") or "")
        deviation: dict[str, Any] | None = None
        if deviation_id:
            row = self.state._connection().execute(
                "SELECT * FROM deviation_reports WHERE deviation_id=?", (deviation_id,),
            ).fetchone()
            if row is not None:
                deviation = dict(row)
                deviation["payload"] = json.loads(deviation["payload"])
        request = (repair.get("payload") or {}).get("request") or {}
        change = (candidate.get("payload") or {}).get("change") or {}
        diagnosis = str(
            request.get("cause_code") or change.get("diagnosis")
            or (deviation or {}).get("deviation_type") or "autonomous repair deviation"
        )
        marker_id = deviation_id or str(repair["repair_run_id"])
        return {
            "deviation_id": deviation_id,
            "deviation": deviation,
            "diagnosis": diagnosis,
            "marker": f"[system-repair:{marker_id}]",
        }

    def publish_issue(self, repair: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        """Create or reuse one repository Issue for the source deviation."""
        if not self.enabled or not self.policy.create_issues:
            return {"status": "disabled"}
        repair_run_id = str(repair["repair_run_id"])
        existing = self.get(repair_run_id)
        if existing and existing.get("issue_url"):
            return existing
        context = self._issue_context(repair, candidate)
        title = f"{context['marker']} {context['diagnosis']}"
        request = (repair.get("payload") or {}).get("request") or {}
        body = "\n".join([
            "## Autonomous system-repair deviation",
            "",
            f"- Repair run: `{repair_run_id}`",
            f"- Source job: `{repair.get('source_job_id')}`",
            f"- Workflow run: `{repair.get('source_run_id') or 'n/a'}`",
            f"- Change candidate: `{candidate.get('candidate_id')}`",
            f"- Deviation: `{context['deviation_id'] or 'unbound'}`",
            f"- Risk: `{candidate.get('risk')}`",
            f"- Diagnosis: `{context['diagnosis']}`",
            f"- Failure fingerprint: `{request.get('source_failure_fingerprint') or 'n/a'}`",
            "",
            "This Issue was created by the governed System Repair SCM publisher. "
            "The coding Agent has no GitHub credentials and cannot close or merge it.",
        ])
        try:
            listed = self._run([
                "gh", "issue", "list", "--repo", self.policy.repository,
                "--state", "all", "--search", f"{context['marker']} in:title",
                "--json", "number,url,title", "--limit", "20",
            ])
            matches = json.loads(listed.stdout or "[]")
            match = next((item for item in matches if context["marker"] in
                          str(item.get("title") or "")), None)
            if match:
                number, url = int(match["number"]), str(match["url"])
                action = "reused"
            else:
                created = self._run([
                    "gh", "issue", "create", "--repo", self.policy.repository,
                    "--title", title, "--body", body,
                ])
                url = self._created_url(created.stdout, "created Issue")
                number = self._number_from_url(url)
                action = "created"
            record = self._record(
                repair_run_id, status="issue_published",
                payload={"issue": {"action": action, "title": title, "url": url,
                                   "number": number, "published_at": utcnow()}},
                issue_number=number, issue_url=url,
            )
            self.control.events.append(
                "system_repair", repair_run_id, "system_repair.scm_issue_published",
                {"action": action, "issue_number": number, "issue_url": url},
                actor="system-repair-scm",
            )
            return record
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            record = self._record(
                repair_run_id, status="issue_deferred",
                payload={"issue": {"action": "deferred", "error": str(exc),
                                   "attempted_at": utcnow()}}, error=str(exc),
            )
            self.control.events.append(
                "system_repair", repair_run_id, "system_repair.scm_publication_deferred",
                {"stage": "issue", "error": str(exc)}, actor="system-repair-scm",
            )
            return record

    def _remove_worktree(self, worktree: Path) -> None:
        self._run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            ok={0, 128},
        )
        if worktree.exists():
            shutil.rmtree(worktree)
        self._run(["git", "worktree", "prune"], ok={0})

    def publish_patch(self, repair: dict[str, Any], candidate: dict[str, Any], *,
                      sandbox: Path, changed_files: list[str], patch_hash: str,
                      validations: list[dict[str, Any]],
                      base_hashes: dict[str, str | None] | None = None) -> dict[str, Any]:
        """Commit a validated patch on an isolated branch and open/reuse a Draft PR."""
        if not self.enabled:
            return {"status": "disabled"}
        repair_run_id = str(repair["repair_run_id"])
        existing = self.get(repair_run_id)
        if existing and existing.get("commit_sha") and (
            existing.get("pr_url") or not self.policy.create_draft_prs
        ):
            return existing
        issue = self.publish_issue(repair, candidate)
        if self.policy.create_issues and not issue.get("issue_url"):
            return issue
        if base_hashes is None:
            raise ScmPublicationError(
                "SCM publication requires source-bound repair baseline hashes"
            )
        safe_files = self._safe_changed_files(sandbox, changed_files)
        short_id = repair_run_id.removeprefix("srr_")[:12]
        branch = f"{self.policy.branch_prefix}/{short_id}"
        if not self._BRANCH.fullmatch(branch):
            raise ScmPublicationError("generated repair branch is invalid")
        worktree = self.root / "var/system-repairs" / repair_run_id / "scm-worktree"
        commit_sha = str((existing or {}).get("commit_sha") or "") or None
        merge_evidence = list(
            ((((existing or {}).get("payload") or {}).get("patch") or {})
             .get("merge_evidence") or [])
        )

        def run(command: list[str], *, cwd: Path | None = None,
                ok: set[int] | None = None) -> subprocess.CompletedProcess[str]:
            return self._run(command, cwd=cwd, ok=ok)

        try:
            git_dir = run(["git", "rev-parse", "--git-dir"]).stdout.strip()
            if not git_dir:
                raise ScmPublicationError("project is not a Git repository")
            run(["git", "fetch", self.policy.remote, self.policy.base_branch])
            source_head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
            base_head = run([
                "git", "rev-parse", f"{self.policy.remote}/{self.policy.base_branch}",
            ]).stdout.strip()
            base_is_ancestor = run(
                ["git", "merge-base", "--is-ancestor", base_head, source_head],
                ok={0, 1},
            ).returncode == 0
            if self.policy.require_base_head_match and not base_is_ancestor:
                raise ScmPublicationError(
                    "source HEAD is not based on configured remote main; rebase the "
                    "running system revision before creating an autonomous repair PR"
                )
            summary = str(((repair.get("payload") or {}).get("agent_result") or {}).get(
                "summary") or "apply governed autonomous repair"
            )
            remote_ref = run([
                "git", "ls-remote", "--heads", self.policy.remote,
                f"refs/heads/{branch}",
            ]).stdout.strip()
            remote_head = remote_ref.split()[0] if remote_ref else None
            resumable = bool(
                commit_sha and remote_head == commit_sha
                and (existing or {}).get("head_branch") == branch
            )
            if remote_head and not resumable:
                raise ScmPublicationError(
                    f"remote repair branch already exists with an unbound commit: {branch}"
                )
            if not resumable:
                if worktree.exists():
                    self._remove_worktree(worktree)
                branch_exists = run(
                    ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                    ok={0, 1},
                ).returncode == 0
                if branch_exists:
                    run(["git", "branch", "-D", branch])
                run([
                    "git", "worktree", "add", "--detach", str(worktree),
                    f"{self.policy.remote}/{self.policy.base_branch}",
                ])
                run(["git", "switch", "-c", branch], cwd=worktree)
                merge_evidence = self._apply_repair_delta(
                    worktree=worktree, sandbox=sandbox, safe_files=safe_files,
                    base_hashes=base_hashes,
                )
                run(["git", "add", "--", *safe_files], cwd=worktree)
                diff = run(
                    ["git", "diff", "--cached", "--quiet"], cwd=worktree, ok={0, 1}
                )
                if diff.returncode == 0:
                    raise ScmPublicationError("validated patch has no diff against configured base")
                deviation_id = str(candidate.get("source_deviation_id") or "unbound")
                message = "\n".join([
                    f"fix(system-repair): {summary[:72]}", "",
                    f"System-Repair-Run: {repair_run_id}",
                    f"Source-Job: {repair.get('source_job_id')}",
                    f"Deviation: {deviation_id}",
                    f"Patch-Hash: {patch_hash}",
                ])
                run([
                    "git", "-c", "user.name=LCA System Repair",
                    "-c", "user.email=system-repair@local.invalid",
                    "commit", "--no-gpg-sign", "-m", message,
                ], cwd=worktree)
                commit_sha = run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
                run(["git", "push", "--set-upstream", self.policy.remote, branch], cwd=worktree)
            pr_number: int | None = None
            pr_url: str | None = None
            if self.policy.create_draft_prs:
                listed = run([
                    "gh", "pr", "list", "--repo", self.policy.repository,
                    "--head", branch, "--state", "all", "--json", "number,url,state,isDraft",
                ])
                matches = json.loads(listed.stdout or "[]")
                if matches:
                    pr_number, pr_url = int(matches[0]["number"]), str(matches[0]["url"])
                    pr_action = "reused"
                else:
                    validation_lines = [
                        f"- `{item.get('phase')}`: "
                        f"{'passed' if item.get('passed') else 'failed'}"
                        for item in validations
                    ] or ["- No validation records supplied"]
                    issue_number = issue.get("issue_number") if isinstance(issue, dict) else None
                    body = "\n".join([
                        "## Governed autonomous repair", "",
                        f"- Repair run: `{repair_run_id}`",
                        f"- Source job: `{repair.get('source_job_id')}`",
                        f"- Candidate: `{candidate.get('candidate_id')}`",
                        f"- Risk: `{candidate.get('risk')}`",
                        f"- Patch hash: `{patch_hash}`", "",
                        "### Changed files", *[f"- `{path}`" for path in safe_files], "",
                        "### Validation", *validation_lines, "",
                        "This PR is intentionally Draft. Merge authority remains outside the coding Agent.",
                        f"\nCloses #{issue_number}" if issue_number else "",
                    ])
                    created = run([
                        "gh", "pr", "create", "--repo", self.policy.repository,
                        "--draft", "--base", self.policy.base_branch, "--head", branch,
                        "--title", f"[System Repair] {summary[:90]}", "--body", body,
                    ])
                    pr_url = self._created_url(created.stdout, "created PR")
                    pr_number = self._number_from_url(pr_url)
                    pr_action = "created"
            else:
                pr_action = "disabled"
            if self.policy.required_for_promotion and not pr_url:
                raise ScmPublicationError(
                    f"validated system repair has no Draft PR targeting "
                    f"{self.policy.base_branch}"
                )
            record = self._record(
                repair_run_id, status="published",
                payload={"patch": {
                    "branch": branch, "commit_sha": commit_sha, "patch_hash": patch_hash,
                    "source_head": source_head, "base_head": base_head,
                    "base_is_ancestor": base_is_ancestor,
                    "changed_files": safe_files, "merge_evidence": merge_evidence,
                    "pr_base": self.policy.base_branch, "pr_action": pr_action,
                    "published_at": utcnow(),
                }},
                head_branch=branch, commit_sha=commit_sha,
                issue_number=issue.get("issue_number") if isinstance(issue, dict) else None,
                issue_url=issue.get("issue_url") if isinstance(issue, dict) else None,
                pr_number=pr_number, pr_url=pr_url,
            )
            self.control.events.append(
                "system_repair", repair_run_id, "system_repair.scm_patch_published",
                {"branch": branch, "commit_sha": commit_sha,
                 "issue_url": record.get("issue_url"), "pr_url": pr_url},
                actor="system-repair-scm",
            )
            return record
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            error = str(exc)
            normalized = error.lower()
            failure_kind = (
                "base_revision_conflict"
                if any(marker in normalized for marker in (
                    "not based on configured remote main", "baseline conflict",
                    "patch does not apply", "merge conflict", "unbound commit",
                ))
                else "transient_publication_failure"
            )
            record = self._record(
                repair_run_id, status="publication_deferred",
                payload={"patch": {"branch": branch, "patch_hash": patch_hash,
                                   "changed_files": safe_files, "error": error,
                                   "failure_kind": failure_kind,
                                   "attempted_at": utcnow()}}, error=str(exc),
                head_branch=branch,
                commit_sha=commit_sha,
                issue_number=issue.get("issue_number") if isinstance(issue, dict) else None,
                issue_url=issue.get("issue_url") if isinstance(issue, dict) else None,
            )
            self.control.events.append(
                "system_repair", repair_run_id, "system_repair.scm_publication_deferred",
                {"stage": "patch", "branch": branch, "error": error,
                 "failure_kind": failure_kind},
                actor="system-repair-scm",
            )
            return record
        finally:
            if worktree.exists():
                try:
                    self._remove_worktree(worktree)
                except (OSError, RuntimeError):
                    pass
