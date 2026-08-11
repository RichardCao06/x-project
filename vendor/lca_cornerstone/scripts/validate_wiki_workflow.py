#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate node-wiki provenance Workflow scripts and frozen results.

This validator understands the repository's Workflow DSL.  The DSL is not a
standalone Node.js program (top-level ``return`` is intentional), so
``node --check`` is the wrong validation boundary.

Usage:
  python3 scripts/validate_wiki_workflow.py workflow <workflow.run.js>
  python3 scripts/validate_wiki_workflow.py result <task-output-or-result.json>
  python3 scripts/validate_wiki_workflow.py result <legacy.json> --allow-legacy
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROTOCOL_VERSION = "wiki-ku-v1"
NOMINATION_PROTOCOL_VERSION = "wiki-ku-nomination-v2"
VERDICTS = {"CONFIRMED", "CONTRADICTED", "NOT_FOUND", "INSUFFICIENT"}
FETCH_STATUSES = {"found", "not_found", "paywalled"}


class ValidationError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _is_external_http_url(value: str) -> bool:
    """Return True only for HTTP(S) locators that cannot be local IP evidence."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return True
    return address.is_global


def _normalized_evidence_text(value: Any) -> str:
    """Normalize only transport/layout noise before an exact quote check.

    This intentionally does not case-fold, translate, or remove punctuation:
    a supporting quote is required to be verbatim source text, not a semantic
    paraphrase accepted by the validator.
    """
    text = str(value or "").replace("\u00ad", "")
    return re.sub(r"\s+", " ", text).strip()


def _extract_binding(text: str) -> tuple[str, Any]:
    matches = list(
        re.finditer(
            r"/\* DATA-BINDING:START.*?\*/\s*"
            r"const\s+(NODES|CLAIMS|EVIDENCE)\s*=\s*(.*?)\s*"
            r"/\* DATA-BINDING:END \*/",
            text,
            re.S,
        )
    )
    _require(len(matches) == 1, "必须且只能有一个完整 DATA-BINDING 区块")
    name, raw = matches[0].group(1), matches[0].group(2)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{name} DATA-BINDING 不是合法 JSON: {exc}") from exc
    if name == "EVIDENCE":
        _require(isinstance(value, dict), "EVIDENCE 必须是对象")
    else:
        _require(isinstance(value, list) and value, f"{name} 必须是非空数组")
        _require(all(isinstance(item, dict) for item in value), f"{name} 的每项必须是对象")
    return name, value


def _require_agent_runtime(
    text: str, *, label: str, phase: str, schema: str, model: str, effort: str,
) -> dict[str, str]:
    """Freeze the actual canonical agent options, not matching comment text.

    Production templates deliberately keep ``label`` first and the options
    object flat.  Requiring that canonical shape lets this gate reject comments,
    duplicate JavaScript keys (whose last value wins), and unrelated strings
    that merely mention the expected model.
    """
    executable = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    executable = re.sub(r"(?m)//[^\n]*$", "", executable)
    block_pattern = (
        r"\{\s*label:\s*`" + re.escape(label)
        + r":\$\{[^}]+\}`\s*,(?P<body>[^{}]*)\}"
    )
    blocks = [match.group("body") for match in re.finditer(block_pattern, executable, re.S)]
    _require(len(blocks) == 1, f"Workflow 必须且只能有一个 {label} agent 配置块")
    body = blocks[0]
    canonical = (
        rf"\s*phase:\s*['\"]{re.escape(phase)}['\"]\s*,"
        rf"\s*schema:\s*{re.escape(schema)}\s*,"
        rf"\s*model:\s*['\"]{re.escape(model)}['\"]\s*,"
        rf"\s*effort:\s*['\"]{re.escape(effort)}['\"]\s*,?\s*"
    )
    _require(
        re.fullmatch(canonical, body, re.S) is not None,
        f"{label} agent 必须严格使用 {phase}/{schema}/{model}/{effort}，禁止重复键或 spread 覆盖",
    )
    return {"model": model, "reasoning_effort": effort}


def validate_workflow(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    _require("export const meta" in text, "缺少 export const meta")
    _require(text.count("DATA-BINDING:START") == 1, "DATA-BINDING:START 数量不是 1")
    _require(text.count("DATA-BINDING:END") == 1, "DATA-BINDING:END 数量不是 1")
    binding_name, items = _extract_binding(text)

    if binding_name == "EVIDENCE":
        protocol = items.get("protocol") if isinstance(items, dict) else None
        claims = items.get("claims") if isinstance(items, dict) else None
        _require(
            isinstance(protocol, dict) and protocol.get("version") == "wiki-source-evidence-v1",
            "verify-only Workflow 的 EVIDENCE 协议非法",
        )
        _require(isinstance(claims, list) and claims, "verify-only EVIDENCE.claims 必须非空")
        _require("phase('Verify')" in text, "verify-only Workflow 缺少 Verify")
        _require("agent(" in text, "verify-only Workflow 缺少 agent 调用")
        _require("phase('SearchFetch')" not in text, "verify-only Workflow 禁止 SearchFetch phase")
        _require(
            re.search(r"(?<![A-Za-z0-9_])label:\s*`search:", text) is None,
            "verify-only Workflow 禁止 search agent",
        )
        _require("label: `verify:" in text, "verify-only Workflow 缺少 verify agent")
        _require("web_search_allowed: false" in text, "verify-only 输出未冻结 web_search_allowed=false")
        _require(PROTOCOL_VERSION in text, f"输出未声明协议 {PROTOCOL_VERSION}")
        model_config = _require_agent_runtime(
            text, label="verify", phase="Verify", schema="BATCH_VERDICT_SCHEMA",
            model="gpt-5.6-sol", effort="medium",
        )
        for index, entry in enumerate(claims):
            _require(isinstance(entry, dict), f"EVIDENCE.claims[{index}] 必须是对象")
            claim = entry.get("claim")
            _require(isinstance(claim, dict), f"EVIDENCE.claims[{index}].claim 缺失")
            missing = sorted({
                "claim_id", "node_id", "industry", "section", "claim_text",
                "claim_kind", "believed_source",
            } - set(claim))
            _require(not missing, f"EVIDENCE.claims[{index}].claim 缺字段: {missing}")
            _require(
                claim.get("claim_kind") in {
                    "external_fact", "internal_graph_fact", "modeling_judgment", "evidence_gap",
                },
                f"EVIDENCE.claims[{index}].claim_kind 非法",
            )
        return {
            "mode": "verify_only", "binding": binding_name,
            "items": len(claims), "model_config": model_config,
        }

    if NOMINATION_PROTOCOL_VERSION in text:
        _require(binding_name == "NODES", "nomination Workflow 必须绑定 NODES")
        _require("phase('Extract')" in text, "nomination Workflow 缺少 Extract")
        _require("agent(" in text, "nomination Workflow 缺少 agent 调用")
        _require("phase('SearchFetch')" not in text, "nomination Workflow 禁止 SearchFetch")
        _require("phase('Verify')" not in text, "nomination Workflow 禁止 Verify")
        _require("label: `search:" not in text, "nomination Workflow 禁止 search agent")
        _require("label: `verify:" not in text, "nomination Workflow 禁止 verify agent")
        _require(
            re.search(r"return\s*\{\s*protocol\s*:", text) is not None,
            "nomination 顶层输出缺少 protocol",
        )
        model_config = _require_agent_runtime(
            text, label="nominate", phase="Extract", schema="NOMINATION_SCHEMA",
            model="gpt-5.6-terra", effort="medium",
        )
        required = {"node_id", "node_type", "industry", "name", "facets", "boundary", "dossier"}
        for index, item in enumerate(items):
            missing = sorted(required - set(item))
            _require(not missing, f"NODES[{index}] 缺字段: {missing}")
        return {
            "mode": "nomination", "binding": binding_name,
            "items": len(items), "model_config": model_config,
        }

    _require(
        "pipeline(" in text or "pipelineMode = 'node-batched'" in text
        or "const claimGroups =" in text,
        "缺少逐断言 pipeline 或节点级批处理协议",
    )
    _require("agent(" in text, "缺少 agent 调用")
    _require("label: `search:" in text, "SearchFetch agent 缺少 search: 标签")
    _require("label: `verify:" in text, "Verify agent 缺少 verify: 标签")
    _require("phase: 'SearchFetch'" in text, "缺少 SearchFetch phase 绑定")
    _require("phase: 'Verify'" in text, "缺少 Verify phase 绑定")
    _require("independent: true" in text, "输出未声明独立 Verify 协议")
    _require(PROTOCOL_VERSION in text, f"输出未声明协议 {PROTOCOL_VERSION}")
    _require(re.search(r"return\s*\{\s*protocol\s*:", text) is not None, "顶层输出缺少 protocol")

    mode = "extract" if binding_name == "NODES" else "repair"
    required = {"node_id", "industry"}
    if mode == "extract":
        required |= {"name", "facets", "boundary"}
        _require("phase('Extract')" in text, "extract Workflow 缺少 Extract 阶段")
        _require("parallel(" in text, "extract Workflow 缺少并行断言提取")
    else:
        required |= {"claim_id", "claim_text", "section", "old_tags", "believed_source"}
        _require("phase('Extract')" not in text, "repair Workflow 不应运行 Extract")

    for index, item in enumerate(items):
        missing = sorted(required - set(item))
        _require(not missing, f"{binding_name}[{index}] 缺字段: {missing}")
        if mode == "repair":
            _require(isinstance(item["old_tags"], list), f"CLAIMS[{index}].old_tags 必须是数组")
            _require(bool(str(item["claim_text"]).strip()), f"CLAIMS[{index}].claim_text 不能为空")

    return {"mode": mode, "binding": binding_name, "items": len(items)}


def validate_nomination_result(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"结果不是合法 JSON: {exc}") from exc
    result = _unwrap_result(data)
    protocol = result.get("protocol")
    _require(isinstance(protocol, dict), "缺少 nomination result.protocol")
    _require(
        protocol.get("version") == NOMINATION_PROTOCOL_VERSION,
        f"nomination 协议版本必须是 {NOMINATION_PROTOCOL_VERSION}",
    )
    _require(protocol.get("mode") == "extract", "nomination protocol.mode 必须是 extract")
    claims = result.get("claims")
    _require(isinstance(claims, list) and claims, "nomination claims 必须是非空数组")
    seen_ids: set[str] = set()
    by_node: dict[str, int] = {}
    required = {
        "claim_id", "requirement_id", "node_id", "industry", "section", "claim_text",
        "claim_kind", "node_identity", "believed_source", "believed_locator", "attribution_confidence",
    }
    for index, claim in enumerate(claims):
        _require(isinstance(claim, dict), f"claims[{index}] 必须是对象")
        missing = sorted(required - set(claim))
        _require(not missing, f"claims[{index}] 缺字段: {missing}")
        claim_id = str(claim.get("claim_id", ""))
        _require(bool(claim_id) and claim_id not in seen_ids, f"claim_id 缺失或重复: {claim_id!r}")
        seen_ids.add(claim_id)
        _require(
            bool(str(claim.get("requirement_id", "")).strip()),
            f"claims[{index}] requirement_id 为空",
        )
        node_id = str(claim.get("node_id", ""))
        _require(re.match(r"^[PA]\d{3}$", node_id) is not None, f"claims[{index}] node_id 非法")
        _require(bool(str(claim.get("claim_text", "")).strip()), f"claims[{index}] claim_text 为空")
        kind = claim.get("claim_kind")
        _require(
            kind in {"external_fact", "internal_graph_fact", "modeling_judgment", "evidence_gap"},
            f"claims[{index}] claim_kind 非法",
        )
        source = str(claim.get("believed_source", "")).strip()
        if kind == "modeling_judgment":
            _require(source == "INTERNAL_MODELING_JUDGMENT", f"claims[{index}] 建模判断来源标记非法")
        elif kind == "internal_graph_fact":
            _require(source == "LCA-CORNERSTONE_GRAPH", f"claims[{index}] 图谱事实来源标记非法")
        elif kind == "evidence_gap":
            _require(source == "INTERNAL_MODELING_JUDGMENT", f"claims[{index}] 证据缺口来源标记非法")
        else:
            _require(bool(source), f"claims[{index}] 外部事实缺 believed_source")
        identity = claim.get("node_identity")
        _require(isinstance(identity, dict), f"claims[{index}] 缺 node_identity")
        _require(
            set(identity) == {"display_name", "node_type", "facets", "boundary"}
            and identity.get("node_type") in {"product", "activity"}
            and isinstance(identity.get("facets"), dict)
            and bool(str(identity.get("display_name", "")).strip()),
            f"claims[{index}] node_identity 非法",
        )
        by_node[node_id] = by_node.get(node_id, 0) + 1
    return {"claims": len(claims), "nodes": len(by_node), "claims_by_node": by_node}


def _unwrap_result(data: Any) -> dict[str, Any]:
    _require(isinstance(data, dict), "结果顶层必须是对象")
    if isinstance(data.get("result"), dict):
        data = data["result"]
    _require(isinstance(data, dict), ".result 必须是对象")
    return data


def validate_result(path: Path, allow_legacy: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"结果不是合法 JSON: {exc}") from exc
    result = _unwrap_result(data)
    protocol = result.get("protocol")
    if not allow_legacy:
        _require(isinstance(protocol, dict), "缺少 result.protocol")
        _require(protocol.get("version") == PROTOCOL_VERSION, f"协议版本必须是 {PROTOCOL_VERSION}")
        _require(protocol.get("mode") in {"extract", "repair"}, "protocol.mode 必须是 extract/repair")
    claims = result.get("claims")
    _require(isinstance(claims, list), "result.claims 必须是数组")

    counts = {verdict: 0 for verdict in sorted(VERDICTS)}
    seen_ids: set[str] = set()
    for index, row in enumerate(claims):
        _require(isinstance(row, dict), f"claims[{index}] 必须是对象")
        claim = row.get("claim")
        verify = row.get("verify")
        fetch = row.get("fetchResult")
        _require(isinstance(claim, dict), f"claims[{index}].claim 缺失或非法")
        _require(isinstance(verify, dict), f"claims[{index}].verify 缺失或非法")
        claim_required = {
            "node_id", "industry", "section", "claim_text", "claim_kind",
            "node_identity", "believed_source",
        }
        missing = sorted(claim_required - set(claim))
        _require(not missing, f"claims[{index}].claim 缺字段: {missing}")
        claim_id = claim.get("claim_id")
        if not allow_legacy:
            _require(isinstance(claim_id, str) and claim_id, f"claims[{index}] 缺 claim_id")
            _require(claim_id not in seen_ids, f"claim_id 重复: {claim_id}")
            seen_ids.add(claim_id)
            _require(
                re.match(r"^[PA]\d{3}$", str(claim.get("node_id", ""))) is not None,
                f"claims[{index}] node_id 必须是批次内裸节点 ID，不得加行业前缀",
            )

        verdict = verify.get("verdict")
        _require(verdict in VERDICTS, f"claims[{index}] verdict 非法: {verdict!r}")
        _require(bool(str(verify.get("reasoning", "")).strip()), f"claims[{index}] 缺核验理由")
        counts[verdict] += 1

        claim_kind = str(claim.get("claim_kind", ""))
        _require(
            claim_kind in {"external_fact", "internal_graph_fact", "modeling_judgment", "evidence_gap"},
            f"claims[{index}] claim_kind 非法: {claim_kind!r}",
        )
        believed_source = str(claim.get("believed_source", "")).strip()
        identity = claim.get("node_identity")
        _require(
            isinstance(identity, dict)
            and set(identity) == {"display_name", "node_type", "facets", "boundary"}
            and identity.get("node_type") in {"product", "activity"}
            and isinstance(identity.get("facets"), dict),
            f"claims[{index}] node_identity 非法",
        )
        alignment = verify.get("node_alignment")
        _require(alignment in {"EXACT", "ADJACENT", "UNRELATED"},
                 f"claims[{index}] node_alignment 非法")
        if claim_kind == "internal_graph_fact":
            _require(believed_source == "LCA-CORNERSTONE_GRAPH",
                     f"claims[{index}] 图谱事实来源标记非法")
        elif claim_kind in {"modeling_judgment", "evidence_gap"}:
            _require(believed_source == "INTERNAL_MODELING_JUDGMENT",
                     f"claims[{index}] 内部判断来源标记非法")
        else:
            _require(bool(believed_source), f"claims[{index}] 外部事实缺来源身份")

        if fetch is not None:
            _require(isinstance(fetch, dict), f"claims[{index}].fetchResult 必须是对象或 null")
            _require(fetch.get("status") in FETCH_STATUSES, f"claims[{index}] fetch status 非法")
            if fetch.get("status") == "found":
                _require(bool(str(fetch.get("url", "")).strip()), f"claims[{index}] found 但无 URL")
                _require(bool(str(fetch.get("excerpt", "")).strip()), f"claims[{index}] found 但无摘录")
                url = str(fetch.get("url", ""))
                _require(
                    _is_external_http_url(url),
                    f"claims[{index}] found URL 必须是外部 http(s)，不得使用本地地址",
                )

        if verdict in {"CONFIRMED", "CONTRADICTED", "INSUFFICIENT"}:
            _require(isinstance(fetch, dict) and fetch.get("status") == "found",
                     f"claims[{index}] {verdict} 必须基于 found 原文")
        if claim_kind != "external_fact":
            _require(verdict == "NOT_FOUND", f"claims[{index}] 内部 claim 不得伪造外部核验 verdict")
            _require(alignment == "EXACT", f"claims[{index}] 内部 claim node_alignment 必须 EXACT")
            _require(not fetch or fetch.get("status") == "not_found",
                     f"claims[{index}] 内部 claim 不得绑定外部 fetch")
        if verdict == "CONFIRMED":
            _require(alignment == "EXACT", f"claims[{index}] 非 EXACT 节点证据不得 CONFIRMED")
            supporting_quote = _normalized_evidence_text(verify.get("supporting_quote"))
            excerpt = _normalized_evidence_text(fetch.get("excerpt") if isinstance(fetch, dict) else "")
            _require(bool(supporting_quote),
                     f"claims[{index}] CONFIRMED 缺 supporting_quote")
            _require(
                supporting_quote in excerpt,
                f"claims[{index}] supporting_quote 不是 fetch excerpt 的逐字子串",
            )

        audit = row.get("verification_protocol")
        if not allow_legacy:
            _require(isinstance(audit, dict), f"claims[{index}] 缺 verification_protocol")
            _require(audit.get("independent") is True, f"claims[{index}] 未声明独立核验")
            search_label = str(audit.get("search_label", ""))
            verify_label = str(audit.get("verify_label", ""))
            _require(search_label.startswith("search:"), f"claims[{index}] search_label 非法")
            if verdict == "NOT_FOUND":
                _require(not verify_label, f"claims[{index}] NOT_FOUND 不应伪造 Verify agent 标签")
                _require(bool(str(audit.get("verify_skipped_reason", "")).strip()),
                         f"claims[{index}] NOT_FOUND 缺 verify_skipped_reason")
            else:
                _require(verify_label.startswith("verify:"), f"claims[{index}] verify_label 非法")
                _require(search_label != verify_label, f"claims[{index}] Search/Verify 标签不得相同")

    return {"claims": len(claims), "verdicts": counts, "legacy": protocol is None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    wf = sub.add_parser("workflow", help="校验 Workflow DSL 模板或物料化 run-script")
    wf.add_argument("path", type=Path)
    result = sub.add_parser("result", help="校验 Workflow task output 或冻结的 .result")
    result.add_argument("path", type=Path)
    result.add_argument("--allow-legacy", action="store_true", help="兼容旧的无协议冻结文件")
    nomination = sub.add_parser("nomination-result", help="校验生产空页断言提名结果")
    nomination.add_argument("path", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "workflow":
            report = validate_workflow(args.path)
        elif args.command == "nomination-result":
            report = validate_nomination_result(args.path)
        else:
            report = validate_result(args.path, args.allow_legacy)
    except (OSError, ValidationError) as exc:
        print(f"❌ {args.path}: {exc}", file=sys.stderr)
        return 1
    print(f"✅ {args.path}: {json.dumps(report, ensure_ascii=False, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
