#!/usr/bin/env python3
"""Review each Wiki section independently and aggregate a fail-closed verdict."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import json
import subprocess
from pathlib import Path

from run_wiki_content_capture import DISABLED, _claims, validate_result


def review_one(root: Path, node_id: str, section: dict, schema: dict, out: Path, timeout: int) -> dict:
    heading = section["heading"]
    section_dir = out / heading
    section_dir.mkdir(parents=True, exist_ok=True)
    effective = copy.deepcopy(schema)
    effective["properties"]["node_id"] = {"type": "string", "const": node_id}
    effective["properties"]["heading"] = {"type": "string", "const": heading}
    schema_path = section_dir / "schema.json"
    schema_path.write_text(json.dumps(effective, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompt = (
        "你是独立中文技术百科编辑，只审稿，不改稿、无工具、无网络。逐段检查本节是否围绕唯一中心，"
        "相邻句是否形成论点—证据—解释—边界/应用链，是否有机械 claim 拼接、换词重复、术语或产品层级漂移。"
        "claim_kind=modeling_judgment 是允许不带外部引用的正式方法内容；不得仅因其没有外部证据而判错，"
        "但仍要检查它是否围绕单一中心、是否把互不相关的方法要求机械融合。"
        "特别检查刀片服务器与服务器刀片不得被虚构为两个产品层级。不能因为字段齐全就 GO。"
        "只有全部 checks=true 且 issues=[] 才能 GO；否则必须准确定位段落并给修复指令。输出只匹配 schema。\n"
        f"SECTION={json.dumps(section, ensure_ascii=False, separators=(',', ':'))}"
    )
    for attempt in range(1, 3):
        result = section_dir / f"review-attempt-{attempt}.json"
        events = section_dir / f"events-attempt-{attempt}.jsonl"
        stderr = section_dir / f"stderr-attempt-{attempt}.log"
        command = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                   "-C", str(root), "-s", "read-only", "-m", "gpt-5.6-sol",
                   "-c", 'model_reasoning_effort="high"']
        for feature in DISABLED:
            command.extend(["--disable", feature])
        command.extend(["--json", "--output-schema", str(schema_path), "-o", str(result), prompt])
        with events.open("w", encoding="utf-8") as event_stream, stderr.open("w", encoding="utf-8") as error_stream:
            try:
                completed = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, stdout=event_stream,
                                           stderr=error_stream, text=True, check=False, timeout=timeout)
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
        if exit_code != 0 or not result.is_file():
            continue
        review = json.loads(result.read_text(encoding="utf-8"))
        coherent = ((review["verdict"] == "GO") == (all(review["checks"].values()) and not review["issues"]))
        if not coherent:
            continue
        final = section_dir / "review.json"
        result.replace(final)
        return {"heading": heading, "exit_code": 0, "path": str(final), "review": review, "attempt": attempt}
    return {"heading": heading, "exit_code": 1, "path": None, "review": None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("content_result", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("section_schema", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    blueprint = json.loads(args.blueprint.read_text(encoding="utf-8"))
    content = json.loads(args.content_result.read_text(encoding="utf-8"))
    validate_result(args.content_result, blueprint, _claims(args.verify_output, blueprint["node_id"]))
    schema = json.loads(args.section_schema.read_text(encoding="utf-8"))
    reports = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(review_one, root, blueprint["node_id"], section, schema, out,
                                   args.timeout_seconds) for section in content["sections"]]
        for future in as_completed(futures):
            reports.append(future.result())
    order = list(blueprint["sections"])
    reports.sort(key=lambda item: order.index(item["heading"]))
    transport_failed = [item["heading"] for item in reports if item["exit_code"] != 0]
    issues = []
    checks = {key: True for key in ("paragraph_focus", "adjacency_logic", "term_identity_consistency",
                                     "redundancy_control", "citation_readability", "overall_readability")}
    for item in reports:
        review = item.get("review") or {}
        for key, value in (review.get("checks") or {}).items():
            checks[key] = checks[key] and bool(value)
        for issue in review.get("issues") or []:
            issues.append({"section": item["heading"], **issue})
    verdict = "GO" if not transport_failed and all(checks.values()) and not issues else "NO_GO"
    aggregated = {"protocol": "wiki-editorial-review-v1", "node_id": blueprint["node_id"],
                  "verdict": verdict, "reviewed_sections": order if not transport_failed else
                  [item["heading"] for item in reports if item["exit_code"] == 0],
                  "checks": checks, "issues": issues}
    (out / "editorial-review.json").write_text(json.dumps(aggregated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "section-review-report.json").write_text(json.dumps({"verdict": verdict,
        "transport_failed": transport_failed, "sections": [{k: v for k, v in item.items() if k != "review"} for item in reports]},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": verdict, "transport_failed": transport_failed, "issues": issues}, ensure_ascii=False))
    return 0 if verdict == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
