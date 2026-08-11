#!/usr/bin/env python3
"""Run an independent, no-Web paragraph-level editorial review and freeze it."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path

from run_wiki_content_capture import DISABLED, _claims, collect_usage, validate_result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("content_result", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("schema", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--cost-usd", type=float, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    verify_path = args.verify_output.resolve()
    content_path = args.content_result.resolve()
    blueprint_path = args.blueprint.resolve()
    schema_path = args.schema.resolve()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
    rows = _claims(verify_path, blueprint["node_id"])
    validate_result(content_path, blueprint, rows)

    result = out / "editorial-review.json"
    events = out / "editorial-review-events.jsonl"
    stderr = out / "editorial-review-stderr.log"
    invocation = out / "editorial-review-invocation.json"
    usage = out / "editorial-review-usage.json"
    prompt = (
        "你是独立的中文技术百科编辑，只审稿，不改稿，不读取文件、不调用工具、不联网。"
        "逐节逐段阅读 CONTENT。重点判断：每段是否只有一个中心；相邻句是否形成论点—证据—解释—边界/应用链；"
        "是否把证据字段机械并排；术语是否稳定，尤其不得把同义的刀片服务器与服务器刀片虚构成两个层级；"
        "是否存在换词重复、引用打断或为了字数堆句。不能因为结构字段齐全就判 GO。"
        "任何一段出现类似‘A。来源 B。’且 A/B 没有清楚语义关系，必须 NO_GO。"
        "只有所有 checks 为 true 且 issues 为空才能 verdict=GO；否则给出可直接反馈给写作 Agent 的定位和修复指令。"
        "reviewed_sections 必须按 blueprint 顺序完整列出。输出只匹配 JSON schema。\n"
        f"BLUEPRINT={json.dumps(blueprint, ensure_ascii=False, separators=(',', ':'))}\n"
        f"CONTENT={content_path.read_text(encoding='utf-8')}"
    )
    root = Path(__file__).resolve().parents[1]
    command = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "-C", str(root),
               "-s", "read-only", "-m", "gpt-5.6-sol", "-c", 'model_reasoning_effort="high"']
    for feature in DISABLED:
        command.extend(["--disable", feature])
    command.extend(["--json", "--output-schema", str(schema_path), "-o", str(result), prompt])
    invocation.write_text(json.dumps({
        "protocol": "wiki-editorial-review-runtime-v1",
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "argv": command,
        "content_sha256": sha256(content_path),
        "verify_sha256": sha256(verify_path),
        "blueprint_sha256": sha256(blueprint_path),
        "schema_sha256": sha256(schema_path),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    exit_code = 124
    error = None
    with events.open("w", encoding="utf-8") as event_stream, stderr.open("w", encoding="utf-8") as error_stream:
        try:
            completed = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, stdout=event_stream,
                                       stderr=error_stream, text=True, check=False,
                                       timeout=max(1, args.timeout_seconds))
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            error = "Editorial review timeout"
    review = None
    if exit_code == 0:
        try:
            review = json.loads(result.read_text(encoding="utf-8"))
            expected = list(blueprint["sections"])
            checks = review.get("checks") or {}
            coherent = (
                review.get("protocol") == "wiki-editorial-review-v1"
                and review.get("node_id") == blueprint["node_id"]
                and review.get("reviewed_sections") == expected
                and ((review.get("verdict") == "GO") == (all(checks.values()) and not review.get("issues")))
            )
            if not coherent:
                raise ValueError("Editorial review verdict/checks/issues 不自洽或章节未完整审阅")
            if review["verdict"] != "GO":
                exit_code = 2
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error, exit_code = str(exc), 2
    usage_rows: list[dict] = []
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            collect_usage(json.loads(line), usage_rows)
        except json.JSONDecodeError:
            pass
    usage.write_text(json.dumps({
        "protocol": "wiki-editorial-review-usage-v1",
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "exit_code": exit_code,
        "error": error,
        "verdict": review.get("verdict") if review else None,
        "usage_records": usage_rows,
        "artifacts": {"invocation_sha256": sha256(invocation), "events_sha256": sha256(events),
                      "stderr_sha256": sha256(stderr), "result_sha256": sha256(result) if result.exists() else None},
        "cost_usd": args.cost_usd,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exit_code": exit_code, "review": str(result),
                      "verdict": review.get("verdict") if review else None, "error": error}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
