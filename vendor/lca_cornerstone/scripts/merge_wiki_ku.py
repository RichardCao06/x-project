#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plan and apply bounded KU repairs to existing node Wiki pages.

The planner never asks an LLM to rewrite a whole page.  It only proposes an
automatic edit when the original source line is uniquely anchored, carries its
own citation, and contains exactly one claim from the current KU batch.
Contradictions, paragraph-level shared citations, stale hashes, and ambiguous
matches become ``manual_review`` items.

Usage:
  python3 scripts/merge_wiki_ku.py plan --ku runs/.../kus.json \
      --wiki wiki/steel --registry sources/steel/registry.json \
      --output runs/.../merge-plan.json
  python3 scripts/merge_wiki_ku.py apply --plan runs/.../merge-plan.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
BODY_RE = re.compile(r"<!-- BODY:START -->(.*?)<!-- BODY:END -->", re.S)
CITE_RE = re.compile(r"\[\^([a-z0-9\-]+)\](?!:)")


class MergeError(ValueError):
    pass


class RepoLease:
    """Small, dependency-free repository lease used by batch Apply.

    The lock is deliberately a file inside the repository, not an in-memory
    mutex: separate CLI processes must not update the shared source registry at
    the same time.  A crashed owner can be replaced only after the frozen lease
    expiry.
    """

    def __init__(self, path: Path, lease_seconds: int = 300):
        self.path = path
        self.lease_seconds = lease_seconds
        self.token = uuid.uuid4().hex

    def __enter__(self) -> "RepoLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": self.token,
            "pid": os.getpid(),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "expires_at_epoch": time.time() + self.lease_seconds,
        }
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                return self
            except FileExistsError:
                try:
                    current = json.loads(self.path.read_text(encoding="utf-8"))
                    expired = float(current.get("expires_at_epoch", 0)) <= time.time()
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    expired = False
                if not expired:
                    raise MergeError(f"Apply lock 正被占用: {self.path}")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        raise MergeError(f"无法取得 Apply lock: {self.path}")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if current.get("token") == self.token:
                self.path.unlink()
        except FileNotFoundError:
            pass


def repository_root_for_registry(registry_path: Path) -> Path:
    """Resolve ``<repo>/sources/<industry>/registry.json`` without using ROOT.

    Tests use temporary repositories; deriving the root from the artifact keeps
    their lock and transaction data out of the real checkout.
    """
    for parent in registry_path.resolve().parents:
        if parent.name == "sources":
            return parent.parent
    return registry_path.resolve().parent


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def repo_path(path: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def resolve_plan_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def find_pages(wiki_root: Path) -> dict[str, Path]:
    pages: dict[str, Path] = {}
    for folder in ("products", "activities"):
        for path in (wiki_root / folder).glob("*.md"):
            match = re.match(r"([PA]\d{3})--", path.name)
            if match:
                if match.group(1) in pages:
                    raise MergeError(f"节点 {match.group(1)} 有多个 Wiki 页面")
                pages[match.group(1)] = path
    return pages


def without_tags(line: str, tags: list[str]) -> str:
    result = line
    for tag in tags:
        result = re.sub(rf"\s*\[\^{re.escape(tag)}\]", "", result)
    result = re.sub(r"\s+(?=[。！？；,.!?;])", "", result)
    return result.rstrip()


def normalized_claim_line(line: str) -> str:
    """Normalize one source line for the single-claim safety check.

    Automatic replacement is only safe when the whole Markdown line represents
    exactly the frozen claim.  A direct footnote on the first sentence does not
    authorize replacing the other sentences on the same line.
    """
    result = re.sub(r"\[\^[a-z0-9\-]+\]", "", line)
    result = re.sub(r"\*\*([^*]+)\*\*", r"\1", result)
    result = re.sub(r"`([^`]+)`", r"\1", result)
    result = re.sub(r"^[\-\*]\s+", "", result.strip())
    return result.strip()


def render_replacement(line: str, ku: dict[str, Any]) -> str:
    base = without_tags(line, ku.get("old_tags", []))
    base = re.sub(r"\s*(?:✅已核实|〔未核实·模型回忆〕)\s*$", "", base).rstrip()
    if ku["authority"] == "reviewed":
        return f"{base} ✅已核实 [^{ku['ku_id']}]"
    return f"{base} 〔未核实·模型回忆〕"


def registry_entry(ku: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    old = next((registry[tag] for tag in ku.get("old_tags", []) if tag in registry), {})
    provenance = ku["provenance"]
    quote = str(provenance.get("quote") or "").strip()
    ref = str(provenance.get("ref") or "").strip()
    locator = str(provenance.get("locator") or "").strip() or ref
    return {
        "title": ku.get("believed_source") or old.get("title") or ref or ku["ku_id"],
        "type": old.get("type", "web"),
        "version": old.get("version", "-"),
        "locator": locator,
        "authority": old.get("authority", "claim-verified"),
        "hash": "",
        "ref_count": 1,
        "excerpt_seeds": [quote],
        "status": "verified",
        "region": old.get("region", "UNSPECIFIED"),
        "verified_via": f"generate-node-wiki {PROTOCOL_VERSION}; claim_id={ku.get('claim_id') or '-'}; verdict=CONFIRMED",
        "url": ref,
    }


def compatible_reverification(existing: dict[str, Any], replacement: dict[str, Any]) -> bool:
    """Allow a fresh Verify run to tighten only the decisive excerpt.

    KU ids bind the claim, not a model's chosen quote.  A rerun may select a
    shorter verbatim substring, but it may not silently change source,
    locator, authority, status or claim identity metadata.
    """
    left = dict(existing)
    right = dict(replacement)
    left.pop("excerpt_seeds", None)
    right.pop("excerpt_seeds", None)
    return left == right and bool(replacement.get("excerpt_seeds"))


PROTOCOL_VERSION = "wiki-ku-v1"


def footnote_line(ku: dict[str, Any], entry: dict[str, Any]) -> str:
    ref = ku["provenance"].get("ref") or entry.get("url") or entry["locator"]
    locator = ku["provenance"].get("locator") or ""
    quote = ku["provenance"].get("quote") or ""
    location_text = f"，{locator}" if locator and locator != ref else ""
    return f"[^{ku['ku_id']}]: {entry['title']} ({ref}{location_text}) —— 独立核验摘录:「{quote}」。"


def update_footnotes(body: str, applied_kus: list[dict[str, Any]], entries: dict[str, Any]) -> str:
    remaining = set(CITE_RE.findall(body))
    old_tags = {tag for ku in applied_kus for tag in ku.get("old_tags", [])}
    for tag in sorted(old_tags - remaining):
        body = re.sub(rf"^\[\^{re.escape(tag)}\]:.*(?:\n|$)", "", body, flags=re.M)

    reviewed = [ku for ku in applied_kus if ku["authority"] == "reviewed"]
    if not reviewed:
        return body
    if not re.search(r"^## 出处\s*$", body, re.M):
        body = body.rstrip() + "\n\n## 出处\n"
    for ku in reviewed:
        definition = footnote_line(ku, entries[ku["ku_id"]])
        pattern = rf"^\[\^{re.escape(ku['ku_id'])}\]:.*$"
        if re.search(pattern, body, re.M):
            body = re.sub(pattern, definition, body, flags=re.M)
        else:
            body = body.rstrip() + "\n" + definition + "\n"
    return body


def add_changelog(text: str, entry: str) -> str:
    if "<!-- CHANGELOG:START -->" in text:
        marker = "<!-- CHANGELOG:START -->\n## 修改日志\n"
        if marker not in text:
            raise MergeError("已有 CHANGELOG:START，但缺少规范的“## 修改日志”标题")
        return text.replace(marker, marker + "\n" + entry.rstrip() + "\n", 1)
    return (
        text.rstrip()
        + "\n\n<!-- CHANGELOG:START -->\n## 修改日志\n\n"
        + entry.rstrip()
        + "\n\n<!-- CHANGELOG:END -->\n"
    )


def force_partial_draft(text: str) -> str:
    """Content Apply must never leave a stale reviewed/empty frontmatter state."""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        raise MergeError("Wiki 页面缺少 frontmatter")
    block = match.group(1)
    verified_claim_refs = set(re.findall(r"\[\^(ku-[a-z0-9-]+)\](?!:)", text))
    # Draft materialization always retains the verified internal graph/review
    # provenance even when no external KU citation was confirmed.  The v2
    # status vocabulary represents that honest limited state as
    # source_verified/partial; claim_verified/complete remains privileged.
    provenance_status = "source_verified"
    claim_status = "partial"
    updates = {
        "schema_version": "wiki-v2",
        "body_status": "draft",
        "content_maturity": "candidate",
        "structure_status": "conformant",
        "provenance_status": provenance_status,
        "claim_verification_status": claim_status,
        "quantity_status": "not_populated",
        "dataset_readiness": "blocked_pending_node_specific_lci",
        "change_log_status": "recorded",
    }
    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}:\s*.*$", re.M)
        block = pattern.sub(f"{key}: {value}", block) if pattern.search(block) else block + f"\n{key}: {value}"
    # Rebuild may intentionally preserve rich evidence tables.  Their source
    # ids are not Markdown citations, so derive them directly from EV blocks.
    # Do not retain the whole old frontmatter set: that would keep citations
    # belonging only to the replaced BODY and create decorative stale refs.
    evidence_refs: set[str] = set()
    for evidence_block in re.findall(
        r"<!-- EV:[a-z_]+:START -->(.*?)<!-- EV:[a-z_]+:END -->", text, re.S
    ):
        evidence_refs.update(re.findall(r"\bku-[a-z0-9-]+\b", evidence_block))
    used = sorted(evidence_refs | set(CITE_RE.findall(text)) | {"internal-graph", "internal-review"})
    refs = "[" + ", ".join(used) + "]"
    pattern = re.compile(r"^provenance_refs:\s*.*$", re.M)
    block = pattern.sub(f"provenance_refs: {refs}", block) if pattern.search(block) else block + f"\nprovenance_refs: {refs}"
    return f"---\n{block}\n---\n" + text[match.end():]


def replace_evidence_sections(text: str, replacement: str) -> str:
    """Replace every typed EV section as one bounded v2 schema unit."""
    body_marker = "<!-- BODY:END -->"
    if body_marker not in text:
        raise MergeError("Wiki 页面缺少 BODY:END，不能安全替换证据表")
    split_at = text.index(body_marker) + len(body_marker)
    prefix, tail = text[:split_at], text[split_at:]
    for kind in ("props", "flows", "emissions", "indicators", "params", "quality"):
        matches = list(re.finditer(
            rf"(?s)<!-- EV:{kind}:START -->.*?<!-- EV:{kind}:END -->",
            tail,
        ))
        if len(matches) > 1:
            raise MergeError(f"Wiki 页面含重复 EV:{kind} 区块")
        if not matches:
            continue
        match = matches[0]
        heading_at = tail.rfind("\n## ", 0, match.start())
        if heading_at < 0:
            raise MergeError(f"EV:{kind} 前缺少二级标题，不能安全替换")
        intervening = tail[heading_at + 1:match.start()]
        if "\n## " in intervening:
            raise MergeError(f"EV:{kind} 标题边界不唯一")
        tail = tail[:heading_at] + "\n" + tail[match.end():].lstrip("\n")
    text = prefix + tail
    marker = "<!-- LCA_ASSOCIATION:START -->"
    if marker in text:
        return text.replace(marker, replacement.strip() + "\n\n" + marker, 1)
    marker = "<!-- CHANGELOG:START -->"
    if marker in text:
        return text.replace(marker, replacement.strip() + "\n\n" + marker, 1)
    return text.rstrip() + "\n\n" + replacement.strip() + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def make_changelog(date: str, operations: list[dict[str, Any]], manual: list[dict[str, Any]]) -> str:
    reviewed = sum(op["authority"] == "reviewed" for op in operations)
    downgraded = sum(op["authority"] == "draft" for op in operations)
    unresolved = len(manual)
    return (
        f"### {date} · 断言级溯源受控合并\n\n"
        f"- **发现的问题：** 本次送审断言中有 {reviewed} 条取得独立支持、"
        f"{downgraded} 条证据不足；另有 {unresolved} 条因冲突、共享引用或锚点不唯一未自动修改。\n"
        f"- **采取的修改：** 已核实断言改挂专属 `ku-*` 引用；证据不足断言移除装饰性旧引用并明确降级。"
        + (" 未决项保留在合并计划的 `manual_review` 中。" if unresolved else "")
        + "\n"
        "- **修改原则：** 只修改哈希一致且唯一命中的原文行；不整页重写；不机械处理矛盾；"
        "来源注册、正文引用与修改日志同步更新。\n"
        f"- **数据影响：** 本次处理 {len(operations)} 条可安全合并断言；未新增或推断任何 LCI 数值。"
    )


def plan_merge(ku_path: Path, wiki_root: Path, registry_path: Path, date: str) -> dict[str, Any]:
    data = json.loads(ku_path.read_text(encoding="utf-8"))
    kus = data.get("kus")
    if not isinstance(kus, list) or not kus:
        raise MergeError("KU 文件缺少非空 kus 数组")
    registry_doc = json.loads(registry_path.read_text(encoding="utf-8"))
    registry = registry_doc.get("sources")
    if not isinstance(registry, dict):
        raise MergeError("registry 缺少 sources 对象")
    pages = find_pages(wiki_root)

    by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ku in kus:
        by_node[ku["node_id"]].append(ku)

    files = []
    registry_entries: dict[str, Any] = {}
    for node_id, node_kus in sorted(by_node.items()):
        if node_id not in pages:
            raise MergeError(f"找不到节点 {node_id} 的 Wiki 页面")
        path = pages[node_id]
        text = path.read_text(encoding="utf-8")
        body_match = BODY_RE.search(text)
        if not body_match:
            raise MergeError(f"{path} 缺少 BODY 标记")
        body = body_match.group(1)
        line_counts = Counter(
            (ku.get("source_anchor") or {}).get("source_line", "")
            for ku in node_kus
            if (ku.get("source_anchor") or {}).get("source_line")
        )
        operations = []
        manual = []
        simulated = body

        for ku in node_kus:
            anchor = ku.get("source_anchor") or {}
            line = anchor.get("source_line") or ""
            reasons = []
            if ku["authority"] == "contradicted":
                reasons.append("CONTRADICTED 必须人工裁决")
            if not line:
                reasons.append("缺少 source_line 锚点（旧版冻结结果）")
            if anchor.get("citation_scope") != "direct":
                reasons.append("引用由段末继承，不能安全归属于单句")
            if line and normalized_claim_line(line) != str(ku.get("claim_text", "")).strip():
                reasons.append("原文行不只包含当前断言，禁止整行替换")
            if line and line_counts[line] != 1:
                reasons.append("同一原文行对应多条断言")
            if line and body.count(line) != 1:
                reasons.append(f"原文行命中次数为 {body.count(line)}，要求恰好 1")
            expected_body = anchor.get("body_sha256")
            if expected_body and expected_body != sha256(body):
                reasons.append("BODY 哈希已变化")
            expected_line = anchor.get("source_line_sha256")
            if expected_line and line and expected_line != sha256(line):
                reasons.append("source_line 哈希与锚点不一致")
            if ku["authority"] == "reviewed":
                provenance = ku.get("provenance") or {}
                if not str(provenance.get("ref") or "").strip():
                    reasons.append("CONFIRMED KU 缺来源 URL")
                if not str(provenance.get("quote") or "").strip():
                    reasons.append("CONFIRMED KU 缺原文摘录")
            if reasons:
                manual.append({
                    "claim_id": ku.get("claim_id"),
                    "ku_id": ku["ku_id"],
                    "claim_text": ku["claim_text"],
                    "authority": ku["authority"],
                    "reasons": reasons,
                })
                continue

            replacement = render_replacement(line, ku)
            operation = {
                "claim_id": ku.get("claim_id"),
                "ku_id": ku["ku_id"],
                "action": "replace" if ku["authority"] == "reviewed" else "downgrade",
                "authority": ku["authority"],
                "old_text": line,
                "new_text": replacement,
                "reason": ku.get("verify", {}).get("reasoning", ""),
                "old_tags": ku.get("old_tags", []),
            }
            operations.append(operation)
            simulated = simulated.replace(line, replacement, 1)
            if ku["authority"] == "reviewed":
                registry_entries[ku["ku_id"]] = registry_entry(ku, registry)

        safe_kus = [
            ku for ku in node_kus
            if any(op["ku_id"] == ku["ku_id"] for op in operations)
        ]
        simulated = update_footnotes(simulated, safe_kus, registry_entries)
        files.append({
            "node_id": node_id,
            "path": repo_path(path),
            "file_sha256": sha256(text),
            "body_sha256": sha256(body),
            "operations": operations,
            "manual_review": manual,
            "changelog": make_changelog(date, operations, manual),
            "body_diff": "".join(difflib.unified_diff(
                body.splitlines(keepends=True),
                simulated.splitlines(keepends=True),
                fromfile=f"{path.name}:before",
                tofile=f"{path.name}:after",
            )),
        })

    return {
        "protocol": {"version": PROTOCOL_VERSION, "kind": "wiki-ku-merge-plan"},
        "created_on": date,
        "ku_path": repo_path(ku_path),
        "ku_sha256": sha256(ku_path.read_text(encoding="utf-8")),
        "wiki_root": repo_path(wiki_root),
        "registry_path": repo_path(registry_path),
        "registry_sha256": sha256(registry_path.read_text(encoding="utf-8")),
        "registry_entries": registry_entries,
        "files": files,
    }


def plan_extract_merge(
    ku_path: Path,
    wiki_root: Path,
    registry_path: Path,
    date: str,
    node_contracts: dict[str, dict[str, Any]] | None = None,
    content_candidates: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a hash-locked whole-BODY plan for previously empty pages.

    This is intentionally separate from repair planning: an empty page has no
    line anchor to replace, but it still must enter the same registry+pages
    transaction instead of remaining a detached generated snippet.
    """
    from assemble_wiki_from_ku import (
        render_complete_node,
        render_evidence_tables,
        render_footnotes,
        render_node,
    )

    data = json.loads(ku_path.read_text(encoding="utf-8"))
    kus = data.get("kus")
    if not isinstance(kus, list) or not kus:
        raise MergeError("extract KU 文件缺少非空 kus 数组")
    registry_doc = json.loads(registry_path.read_text(encoding="utf-8"))
    registry = registry_doc.get("sources")
    if not isinstance(registry, dict):
        raise MergeError("registry 缺少 sources 对象")
    pages = find_pages(wiki_root)
    by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ku in kus:
        by_node[ku["node_id"]].append(ku)

    files = []
    entries: dict[str, Any] = {}
    for node_id, node_kus in sorted(by_node.items()):
        path = pages.get(node_id)
        if path is None:
            raise MergeError(f"找不到节点 {node_id} 的 Wiki 页面")
        text = path.read_text(encoding="utf-8")
        match = BODY_RE.search(text)
        if not match:
            raise MergeError(f"{path} 缺少 BODY 标记")
        old_body = match.group(1)
        contract = (node_contracts or {}).get(node_id)
        allow_rebuild = bool(contract and contract.get("write_mode") == "rebuild")
        if "正文待 workflow 填肉" not in old_body and old_body.strip() and not allow_rebuild:
            raise MergeError(f"{path} 已有正文，extract 计划拒绝整段覆盖")
        manual = []
        operations = []
        for ku in node_kus:
            if ku.get("authority") == "contradicted":
                manual.append({
                    "claim_id": ku.get("claim_id"),
                    "ku_id": ku.get("ku_id"),
                    "claim_text": ku.get("claim_text"),
                    "authority": "contradicted",
                    "reasons": ["CONTRADICTED 必须人工裁决"],
                })
            else:
                operations.append({
                    "claim_id": ku.get("claim_id"),
                    "ku_id": ku.get("ku_id"),
                    "action": "insert_body_claim",
                    "authority": ku.get("authority"),
                })
            if ku.get("authority") == "reviewed":
                entries[ku["ku_id"]] = registry_entry(ku, registry)
        replacement_evidence = None
        content_candidate = (content_candidates or {}).get(node_id)
        if content_candidate:
            new_body = "\n" + str(content_candidate["body"]).strip() + "\n"
            replacement_evidence = str(content_candidate["evidence_tables"])
        elif contract:
            node_type = str(contract.get("node_type") or "")
            try:
                new_body = "\n" + render_complete_node(node_kus, node_type).strip() + "\n"
                dossier = contract.get("dossier") or {}
                if not dossier.get("preserve_existing_evidence"):
                    replacement_evidence = render_evidence_tables(node_type, dossier)
            except ValueError as exc:
                raise MergeError(f"{node_id} wiki-v2 组装失败: {exc}") from exc
        else:
            new_body = "\n" + render_node(node_kus).strip() + "\n"
            footnotes = render_footnotes(node_kus).strip()
            if footnotes and footnotes != "## 出处":
                new_body += "\n" + footnotes + "\n"
            elif not re.search(r"^## 出处\s*$", new_body, re.M):
                new_body += "\n## 出处\n"
        files.append({
            "node_id": node_id,
            "write_mode": str(contract.get("write_mode")) if contract else "extract",
            "path": repo_path(path),
            "file_sha256": sha256(text),
            "body_sha256": sha256(old_body),
            "operations": operations,
            "manual_review": manual,
            "replacement_body": new_body,
            "replacement_evidence": replacement_evidence,
            "changelog": make_changelog(date, operations, manual),
            "body_diff": "".join(difflib.unified_diff(
                old_body.splitlines(keepends=True),
                new_body.splitlines(keepends=True),
                fromfile=f"{path.name}:before",
                tofile=f"{path.name}:after",
            )),
        })
    return {
        "protocol": {"version": PROTOCOL_VERSION, "kind": "wiki-ku-merge-plan"},
        "plan_mode": "extract",
        "created_on": date,
        "ku_path": repo_path(ku_path),
        "ku_sha256": sha256(ku_path.read_text(encoding="utf-8")),
        "wiki_root": repo_path(wiki_root),
        "registry_path": repo_path(registry_path),
        "registry_sha256": sha256(registry_path.read_text(encoding="utf-8")),
        "registry_entries": entries,
        "files": files,
    }


def _write_transaction(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _recover_transaction(transaction_path: Path) -> None:
    """Restore backups left by an interrupted transaction before resuming."""
    if not transaction_path.exists():
        return
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    if transaction.get("state") in {"committed", "rolled_back"}:
        return
    for item in reversed(transaction.get("targets", [])):
        target = Path(item["target"])
        backup = Path(item["backup"])
        staged = Path(item["staged"])
        if backup.exists():
            os.replace(backup, target)
        if staged.exists():
            staged.unlink()
    transaction["state"] = "rolled_back"
    transaction["recovered_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _write_transaction(transaction_path, transaction)


def _stage_text(target: Path, text: str, token: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.parent / f".{target.name}.{token}.stage"
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    return staged


def apply_text_transaction(
    changes: list[tuple[Path, str, str]],
    transaction_path: Path,
    repo_root: Path,
    *,
    dry_run: bool = False,
    lease_seconds: int = 300,
) -> dict[str, int | bool | str]:
    """Atomically apply hash-locked text changes under the repository lease."""
    with RepoLease(repo_root / ".wiki-ku-apply.lock", lease_seconds):
        _recover_transaction(transaction_path)
        for target, _, expected_hash in changes:
            if sha256(target.read_text(encoding="utf-8")) != expected_hash:
                raise MergeError(f"{target} hash 漂移，拒绝事务")
        if dry_run:
            return {"files": len(changes), "dry_run": True, "transaction": "preflight_passed"}
        token = uuid.uuid4().hex
        transaction: dict[str, Any] = {
            "protocol": {"version": "wiki-ku-transaction-v1"},
            "token": token,
            "state": "staging",
            "targets": [],
        }
        try:
            for target, content, expected_hash in changes:
                staged = _stage_text(target, content, token)
                transaction["targets"].append({
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(target.parent / f".{target.name}.{token}.backup"),
                    "old_sha256": expected_hash,
                    "new_sha256": sha256(content),
                })
            transaction["state"] = "staged"
            _write_transaction(transaction_path, transaction)
            transaction["state"] = "committing"
            _write_transaction(transaction_path, transaction)
            for item in transaction["targets"]:
                os.replace(item["target"], item["backup"])
                os.replace(item["staged"], item["target"])
            transaction["state"] = "committed"
            _write_transaction(transaction_path, transaction)
            for item in transaction["targets"]:
                backup = Path(item["backup"])
                if backup.exists():
                    backup.unlink()
            return {"files": len(changes), "dry_run": False, "transaction": "committed"}
        except Exception as exc:
            for item in reversed(transaction.get("targets", [])):
                target = Path(item["target"])
                backup = Path(item["backup"])
                staged = Path(item["staged"])
                if backup.exists():
                    os.replace(backup, target)
                if staged.exists():
                    staged.unlink()
            transaction["state"] = "rolled_back"
            transaction["error"] = str(exc)
            _write_transaction(transaction_path, transaction)
            raise MergeError(f"文件事务失败并已回滚: {exc}") from exc


def apply_plan(
    plan_path: Path,
    allow_partial: bool = False,
    *,
    dry_run: bool = False,
    lease_seconds: int = 300,
) -> dict[str, int | bool | str]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    protocol = plan.get("protocol") or {}
    if protocol.get("version") != PROTOCOL_VERSION or protocol.get("kind") != "wiki-ku-merge-plan":
        raise MergeError("不是受支持的 wiki-ku 合并计划")
    manual_count = sum(len(item.get("manual_review", [])) for item in plan["files"])
    if manual_count and not allow_partial:
        raise MergeError(f"计划含 {manual_count} 条 manual_review；默认拒绝部分应用，可复核后使用 --allow-partial")
    if allow_partial and any(
        item.get("replacement_body") is not None and item.get("manual_review")
        for item in plan["files"]
    ):
        raise MergeError("extract 整段计划含 manual_review，不能用 --allow-partial 绕过人工裁决")

    registry_path = resolve_plan_path(plan["registry_path"])
    repo_root = repository_root_for_registry(registry_path)
    lock_path = repo_root / ".wiki-ku-apply.lock"
    transaction_path = plan_path.resolve().parent / "apply-transaction.json"
    if transaction_path.is_file():
        try:
            previous = json.loads(transaction_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {}
        if previous.get("state") == "committed" and previous.get("plan") != str(plan_path.resolve()):
            transaction_path = plan_path.resolve().parent / f"apply-transaction-{sha256(plan_path.read_text(encoding='utf-8'))[:12]}.json"
    report: dict[str, int | bool | str] = {
        "files": sum(bool(item["operations"]) for item in plan["files"]),
        "operations": sum(len(item["operations"]) for item in plan["files"]),
        "manual_review": manual_count,
        "registry_entries": len(plan["registry_entries"]),
        "dry_run": dry_run,
    }

    with RepoLease(lock_path, lease_seconds):
        _recover_transaction(transaction_path)

        # Phase 1: preflight every immutable input before creating any stage.
        registry_text = registry_path.read_text(encoding="utf-8")
        if sha256(registry_text) != plan["registry_sha256"]:
            raise MergeError("registry 在计划生成后已变化，拒绝覆盖")
        ku_path = resolve_plan_path(plan["ku_path"])
        ku_text = ku_path.read_text(encoding="utf-8")
        if sha256(ku_text) != plan.get("ku_sha256"):
            raise MergeError("KU 文件在计划生成后已变化，拒绝应用")
        kus_by_id = {ku["ku_id"]: ku for ku in json.loads(ku_text)["kus"]}

        updated_files: list[tuple[Path, str]] = []
        for item in plan["files"]:
            path = resolve_plan_path(item["path"])
            text = path.read_text(encoding="utf-8")
            if sha256(text) != item["file_sha256"]:
                raise MergeError(f"{path} 在计划生成后已变化，拒绝覆盖")
            match = BODY_RE.search(text)
            if not match or sha256(match.group(1)) != item["body_sha256"]:
                raise MergeError(f"{path} BODY 锚点失效")
            body = match.group(1)
            applied_kus = []
            if "replacement_body" in item:
                body = item["replacement_body"]
                applied_kus = [
                    kus_by_id[op["ku_id"]]
                    for op in item["operations"]
                    if op.get("ku_id") in kus_by_id
                ]
            else:
                for op in item["operations"]:
                    if body.count(op["old_text"]) != 1:
                        raise MergeError(f"{path}: {op['ku_id']} 原文不再唯一命中")
                    body = body.replace(op["old_text"], op["new_text"], 1)
                    applied_kus.append(kus_by_id[op["ku_id"]])
                body = update_footnotes(body, applied_kus, plan["registry_entries"])
            new_text = text[:match.start(1)] + body + text[match.end(1):]
            if item.get("replacement_evidence"):
                new_text = replace_evidence_sections(new_text, item["replacement_evidence"])
            if item["operations"]:
                new_text = add_changelog(new_text, item["changelog"])
                new_text = force_partial_draft(new_text)
                updated_files.append((path, new_text))

        registry_doc = json.loads(registry_text)
        sources = registry_doc["sources"]
        for ku_id, entry in plan["registry_entries"].items():
            existing = sources.get(ku_id)
            if existing and existing != entry and not compatible_reverification(existing, entry):
                raise MergeError(f"registry 已有不同内容的 {ku_id}")
            sources[ku_id] = entry
        if isinstance(registry_doc.get("_meta"), dict):
            registry_doc["_meta"]["count"] = len(sources)

        targets = list(updated_files)
        if plan["registry_entries"]:
            # Registry and pages are one transaction.  Ordering is no longer a
            # safety assumption because rollback covers every committed target.
            targets.insert(0, (
                registry_path,
                json.dumps(registry_doc, ensure_ascii=False, indent=2) + "\n",
            ))
        if dry_run or not targets:
            report["transaction"] = "preflight_passed"
            return report

        # Phase 2: stage every new file, then journal exact rollback locations.
        token = uuid.uuid4().hex
        transaction: dict[str, Any] = {
            "protocol": {"version": "wiki-ku-transaction-v1"},
            "token": token,
            "state": "staging",
            "plan": str(plan_path.resolve()),
            "targets": [],
        }
        try:
            for target, new_text in targets:
                staged = _stage_text(target, new_text, token)
                backup = target.parent / f".{target.name}.{token}.backup"
                transaction["targets"].append({
                    "target": str(target),
                    "staged": str(staged),
                    "backup": str(backup),
                    "old_sha256": sha256(target.read_text(encoding="utf-8")),
                    "new_sha256": sha256(new_text),
                })
            transaction["state"] = "staged"
            _write_transaction(transaction_path, transaction)

            transaction["state"] = "committing"
            _write_transaction(transaction_path, transaction)
            for item in transaction["targets"]:
                os.replace(item["target"], item["backup"])
                os.replace(item["staged"], item["target"])
            transaction["state"] = "committed"
            transaction["committed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            _write_transaction(transaction_path, transaction)
            for item in transaction["targets"]:
                backup = Path(item["backup"])
                if backup.exists():
                    backup.unlink()
            report["transaction"] = "committed"
            return report
        except Exception as exc:
            # Best-effort synchronous rollback; the persisted journal lets the
            # next invocation finish recovery after process interruption.
            for item in reversed(transaction.get("targets", [])):
                target = Path(item["target"])
                backup = Path(item["backup"])
                staged = Path(item["staged"])
                if backup.exists():
                    os.replace(backup, target)
                if staged.exists():
                    staged.unlink()
            transaction["state"] = "rolled_back"
            transaction["error"] = str(exc)
            _write_transaction(transaction_path, transaction)
            raise MergeError(f"Apply 事务失败并已回滚: {exc}") from exc


def rehydrate_committed_plan(
    plan_path: Path, *, allow_partial: bool = False, lease_seconds: int = 300
) -> dict[str, Any]:
    """Replay a lost materialization only from its hash-bound commit."""
    plan_path = plan_path.resolve()
    transaction_path = plan_path.parent / "apply-transaction.json"
    if not transaction_path.is_file():
        raise MergeError("rehydrate requires the original committed transaction")
    original_text = transaction_path.read_text(encoding="utf-8")
    original = json.loads(original_text)
    if (original.get("state") != "committed"
            or Path(str(original.get("plan", ""))).resolve() != plan_path):
        raise MergeError("rehydrate transaction is not committed for the frozen plan")
    targets = original.get("targets")
    if not isinstance(targets, list) or not targets:
        raise MergeError("rehydrate transaction has no targets")
    states: list[str] = []
    expected_new: dict[Path, str] = {}
    for item in targets:
        target = Path(str(item.get("target", ""))).resolve()
        if not target.is_file() or target.is_symlink():
            raise MergeError(f"rehydrate target is missing or unsafe: {target}")
        current = sha256(target.read_text(encoding="utf-8"))
        old, new = str(item.get("old_sha256", "")), str(item.get("new_sha256", ""))
        if current == new:
            states.append("materialized")
        elif current == old:
            states.append("seed")
        else:
            raise MergeError(f"rehydrate target has an unrecognized hash: {target}")
        expected_new[target] = new
    if set(states) == {"materialized"}:
        return {"files": len(targets), "transaction": "committed",
                "rehydrated": False, "already_materialized": True}
    if set(states) != {"seed"}:
        raise MergeError("rehydrate targets are in a mixed state")

    preserved = plan_path.parent / "apply-transaction-pre-rehydrate.json"
    if preserved.exists() and preserved.read_text(encoding="utf-8") != original_text:
        raise MergeError("a different pre-rehydrate transaction is already preserved")
    if not preserved.exists():
        shutil.copy2(transaction_path, preserved)
    report = apply_plan(
        plan_path, allow_partial=allow_partial, dry_run=False,
        lease_seconds=lease_seconds,
    )
    for target, expected in expected_new.items():
        if sha256(target.read_text(encoding="utf-8")) != expected:
            raise MergeError(f"rehydrate result does not match original commit: {target}")
    return {**report, "rehydrated": True, "already_materialized": False,
            "original_transaction_sha256": sha256(original_text)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--ku", required=True, type=Path)
    plan_parser.add_argument("--wiki", required=True, type=Path)
    plan_parser.add_argument("--registry", required=True, type=Path)
    plan_parser.add_argument("--output", required=True, type=Path)
    plan_parser.add_argument("--date", default=dt.date.today().isoformat())
    plan_parser.add_argument(
        "--node-contracts",
        type=Path,
        help=(
            "JSON object keyed by node_id. When supplied, build a hash-locked "
            "whole-BODY extract/rebuild plan with the frozen wiki-v2 contract."
        ),
    )
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--plan", required=True, type=Path)
    apply_parser.add_argument("--allow-partial", action="store_true")
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--lease-seconds", type=int, default=300)
    args = parser.parse_args()

    try:
        if args.command == "plan":
            if args.node_contracts:
                node_contracts = json.loads(
                    args.node_contracts.resolve().read_text(encoding="utf-8")
                )
                if not isinstance(node_contracts, dict):
                    raise MergeError("node contracts 必须是 node_id -> contract 对象")
                plan = plan_extract_merge(
                    args.ku.resolve(),
                    args.wiki.resolve(),
                    args.registry.resolve(),
                    args.date,
                    node_contracts=node_contracts,
                )
            else:
                plan = plan_merge(
                    args.ku.resolve(), args.wiki.resolve(), args.registry.resolve(), args.date
                )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            automatic = sum(len(item["operations"]) for item in plan["files"])
            manual = sum(len(item["manual_review"]) for item in plan["files"])
            print(f"✅ 合并计划: 自动 {automatic} · 人工 {manual} → {args.output}")
            for item in plan["files"]:
                if item["body_diff"]:
                    print(item["body_diff"])
            return 0 if not manual else 2
        report = apply_plan(
            args.plan.resolve(),
            args.allow_partial,
            dry_run=args.dry_run,
            lease_seconds=args.lease_seconds,
        )
        print(f"✅ 合并完成: {json.dumps(report, ensure_ascii=False, sort_keys=True)}")
        return 0 if not report["manual_review"] else 2
    except (OSError, KeyError, MergeError, json.JSONDecodeError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
