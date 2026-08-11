#!/usr/bin/env python3
"""Repair only editorially rejected paragraphs, preserving frozen evidence mappings."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import json
import subprocess
from pathlib import Path

from run_wiki_content_capture import DISABLED, _claims, validate_result


def repair_one(root: Path, node_id: str, heading: str, paragraph_index: int, paragraph: dict,
               issues: list[dict], claim_rows: dict, schema: dict, out: Path, timeout: int) -> dict:
    key = f"{heading}-{paragraph_index:02d}"
    repair_dir = out / key; repair_dir.mkdir(parents=True, exist_ok=True)
    effective = copy.deepcopy(schema)
    effective["properties"]["node_id"] = {"type": "string", "const": node_id}
    effective["properties"]["heading"] = {"type": "string", "const": heading}
    effective["properties"]["paragraph_index"] = {"type": "integer", "const": paragraph_index}
    schema_path = repair_dir / "schema.json"
    schema_path.write_text(json.dumps(effective, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evidence_ids = [source_id for sentence in paragraph["sentences"] for source_id in sentence["evidence_claim_ids"]]
    evidence = [{"claim_id": source_id, "claim_kind": claim_rows[source_id]["claim"]["claim_kind"],
                 "claim_text": claim_rows[source_id]["claim"]["claim_text"]} for source_id in evidence_ids]
    prompt = (
        "你是节点 Wiki 段落修复编辑。无工具、无网络，只修给定段落。逐项落实 REVIEW_ISSUES，"
        "形成单一中心的论点—证据—解释—边界/应用链；统一使用‘刀片服务器’，必要时首次说明‘服务器刀片’仅为同义简称，"
        "不得虚构产品层级。保留原段所有有效信息，但可调整顺序、合并或拆句。"
        "所有 EVIDENCE claim_id 必须且只能在 evidence_claim_ids 中出现一次；不得增加其他 ID、不得扩大外部事实。"
        "2-4句，第一句为唯一 thesis，每段最多一个 external_fact。输出只匹配 schema。\n"
        f"CURRENT={json.dumps(paragraph, ensure_ascii=False)}\n"
        f"EVIDENCE={json.dumps(evidence, ensure_ascii=False)}\n"
        f"REVIEW_ISSUES={json.dumps(issues, ensure_ascii=False)}"
    )
    for attempt in range(1, 3):
        result = repair_dir / f"result-attempt-{attempt}.json"
        events = repair_dir / f"events-attempt-{attempt}.jsonl"
        stderr = repair_dir / f"stderr-attempt-{attempt}.log"
        command = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "-C", str(root),
                   "-s", "read-only", "-m", "gpt-5.6-terra", "-c", 'model_reasoning_effort="medium"']
        for feature in DISABLED: command.extend(["--disable", feature])
        command.extend(["--json", "--output-schema", str(schema_path), "-o", str(result), prompt])
        with events.open("w", encoding="utf-8") as event_stream, stderr.open("w", encoding="utf-8") as error_stream:
            try:
                completed = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, stdout=event_stream,
                                           stderr=error_stream, text=True, check=False, timeout=timeout)
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
        if exit_code != 0 or not result.is_file(): continue
        value = json.loads(result.read_text(encoding="utf-8"))
        repaired = value["paragraph"]
        actual_ids = [source_id for sentence in repaired["sentences"] for source_id in sentence["evidence_claim_ids"]]
        roles = [sentence["rhetorical_role"] for sentence in repaired["sentences"]]
        external = sum(sentence["claim_kind"] == "external_fact" for sentence in repaired["sentences"])
        if sorted(actual_ids) != sorted(evidence_ids) or len(actual_ids) != len(set(actual_ids)):
            continue
        if roles[0] != "thesis" or roles.count("thesis") != 1 or external > 1:
            continue
        final = repair_dir / "repair.json"; result.replace(final)
        return {"key": key, "exit_code": 0, "path": str(final), "paragraph": repaired, "attempt": attempt}
    return {"key": key, "exit_code": 1, "path": None, "paragraph": None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path); parser.add_argument("content_result", type=Path)
    parser.add_argument("blueprint", type=Path); parser.add_argument("editorial_review", type=Path)
    parser.add_argument("schema", type=Path); parser.add_argument("output_dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900); parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]; out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    blueprint = json.loads(args.blueprint.read_text(encoding="utf-8")); content = json.loads(args.content_result.read_text(encoding="utf-8"))
    rows = _claims(args.verify_output, blueprint["node_id"]); validate_result(args.content_result, blueprint, rows)
    claim_rows = {row["claim"]["claim_id"]: row for row in rows}; review = json.loads(args.editorial_review.read_text(encoding="utf-8"))
    if review.get("verdict") != "NO_GO" or not review.get("issues"): raise ValueError("只接受带定位 issues 的 NO_GO 审稿")
    issue_groups = {}
    for issue in review["issues"]: issue_groups.setdefault((issue["section"], issue["paragraph_index"]), []).append(issue)
    section_map = {section["heading"]: section for section in content["sections"]}; schema = json.loads(args.schema.read_text(encoding="utf-8"))
    reports = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(repair_one, root, blueprint["node_id"], heading, index,
            section_map[heading]["paragraphs"][index - 1], issues, claim_rows, schema, out, args.timeout_seconds)
            for (heading, index), issues in issue_groups.items()]
        for future in as_completed(futures): reports.append(future.result())
    failed = [item["key"] for item in reports if item["exit_code"] != 0]
    if failed:
        print(json.dumps({"verdict": "NO_GO", "failed": failed}, ensure_ascii=False)); return 2
    for item in reports:
        heading, raw_index = item["key"].rsplit("-", 1)
        section_map[heading]["paragraphs"][int(raw_index) - 1] = item["paragraph"]
    result = out / "content-result.json"; result.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        scorecard = validate_result(result, blueprint, rows); verdict, error = "GO", None
    except ValueError as exc:
        scorecard, verdict, error = None, "NO_GO", str(exc)
    report = {"protocol": "wiki-editorial-repair-v1", "verdict": verdict, "repairs": reports,
              "scorecard": scorecard, "error": error}
    (out / "repair-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": verdict, "repairs": len(reports), "scorecard": scorecard, "error": error}, ensure_ascii=False))
    return 0 if verdict == "GO" else 2


if __name__ == "__main__": raise SystemExit(main())
