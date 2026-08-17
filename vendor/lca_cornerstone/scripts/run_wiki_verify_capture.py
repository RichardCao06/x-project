#!/usr/bin/env python3
"""Run the no-Web Wiki Verify model and freeze runtime evidence.

The launcher records the exact argv before execution, streams Codex JSONL
events verbatim, keeps stderr, and writes a post-run hash/usage attestation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import subprocess
from pathlib import Path


DISABLED = [
    "browser_use", "in_app_browser", "computer_use", "standalone_web_search",
    "remote_plugin", "plugins", "apps", "multi_agent",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_usage(value, found: list[dict]) -> None:
    if isinstance(value, dict):
        if any("token" in str(key).lower() for key in value):
            found.append(value)
        for child in value.values():
            collect_usage(child, found)
    elif isinstance(value, list):
        for child in value:
            collect_usage(child, found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("schema", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--cost-usd", type=float, required=True,
        help="运行平台账单给出的本次 Verify 成本；免费额度显式传 0",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    evidence = args.evidence.resolve()
    schema = args.schema.resolve()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not math.isfinite(args.cost_usd) or args.cost_usd < 0:
        raise ValueError("--cost-usd 必须是非负有限数")
    verdicts = out / "verify-verdicts.runtime.json"
    events = out / "verify-events.jsonl"
    stderr = out / "verify-stderr.log"
    invocation = out / "verify-invocation.json"
    usage = out / "verify-usage.json"
    batch_usage = out / "wiki-usage-v1.json"

    evidence_doc = json.loads(evidence.read_text(encoding="utf-8"))
    external_claims = [
        item for item in evidence_doc.get("claims", [])
        if isinstance(item, dict) and (item.get("candidates") or [])
    ]
    if not external_claims:
        raise ValueError("evidence 中没有需要 Verify 的外部断言")
    node_ids = {str((item.get("claim") or {}).get("node_id", "")) for item in external_claims}
    if len(node_ids) != 1 or not next(iter(node_ids)):
        raise ValueError("capture 一次只能核验一个节点的外部断言")
    external_count = len(external_claims)

    prompt = (
        f"你是独立 Verify Agent。只读取 {evidence}。不得联网、搜索、调用浏览器、"
        f"补充来源或修改文件。只裁决 candidates 非空的 {external_count} 条断言；每条必须选择唯一冻结 "
        "evidence_id。CONFIRMED/CONTRADICTED 的 supporting_quote 必须从对应 excerpt 原样复制为逐字连续子串，"
        "不得改写、合并、删除或规范化空格；只引用足以支持裁决的最短原文片段，避免跨句长引文；"
        "先把候选原文对象与 claim.node_identity 对齐为 EXACT/ADJACENT/UNRELATED；只有 EXACT 才能 CONFIRMED。"
        "机箱与刀片服务器模块、裸板与PCBA、上游组件与整机、相邻工艺均只能判 ADJACENT；"
        "若原文仅支持部分或断言含原文未支持的推论，裁决 INSUFFICIENT 且 supporting_quote 可为空。"
        f"输出必须严格匹配给定 JSON schema，items 精确覆盖 {external_count} 条外部断言。"
    )
    command = [
        "codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
        "-C", str(root), "-s", "read-only", "-m", "gpt-5.6-sol",
        "-c", 'model_reasoning_effort="medium"',
    ]
    for feature in DISABLED:
        command.extend(["--disable", feature])
    command.extend(["--json", "--output-schema", str(schema), "-o", str(verdicts), prompt])
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    invocation.write_text(json.dumps({
        "protocol": "wiki-verify-runtime-v1", "started_at": started,
        "cwd": str(root), "argv": command, "model": "gpt-5.6-sol",
        "reasoning_effort": "medium", "sandbox": "read-only",
        "disabled_capabilities": DISABLED, "evidence": str(evidence),
        "evidence_sha256": sha256(evidence), "schema_sha256": sha256(schema),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    exit_code = 124
    validation_error = None
    with events.open("w", encoding="utf-8") as event_stream, stderr.open("w", encoding="utf-8") as error_stream:
        try:
            completed = subprocess.run(
                command, cwd=root, stdin=subprocess.DEVNULL, stdout=event_stream, stderr=error_stream,
                text=True, check=False, timeout=max(1, args.timeout_seconds),
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            validation_error = "Verify runtime timeout"
    if exit_code == 0:
        try:
            result_doc = json.loads(verdicts.read_text(encoding="utf-8"))
            if not isinstance(result_doc.get("items"), list) or len(result_doc["items"]) != external_count:
                raise ValueError("Verify verdict item count mismatch")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            validation_error = str(exc)
            exit_code = 2

    usage_rows: list[dict] = []
    event_count = 0
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_count += 1
        collect_usage(event, usage_rows)
    report = {
        "protocol": "wiki-verify-runtime-usage-v1",
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "exit_code": exit_code, "event_count": event_count,
        "validation_error": validation_error,
        "usage_records": usage_rows, "artifacts": {
            "invocation_sha256": sha256(invocation), "events_sha256": sha256(events),
            "stderr_sha256": sha256(stderr),
            "verdicts_sha256": sha256(verdicts) if verdicts.exists() else None,
        },
    }
    usage.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    batch_usage.write_text(json.dumps({
        "protocol": {"version": "wiki-usage-v1", "kind": "usage"},
        "phase": "verify_only", "model": "gpt-5.6-sol",
        "reasoning_effort": "medium", "search_requests": 0,
        "cost_usd": args.cost_usd,
        "runtime_usage_sha256": sha256(usage),
        "runtime_invocation_sha256": sha256(invocation),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
