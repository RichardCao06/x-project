#!/usr/bin/env python3
"""Generate Wiki content as isolated sections, then deterministically merge and validate."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import json
import subprocess
from pathlib import Path

from run_wiki_content_capture import DISABLED, _claims, sanitize_output_schema, validate_result


def section_minimum_sentences(heading: str) -> int:
    # A section needs a coherent paragraph, not a fixed amount of padding.
    return 2


def generate_section(
    root: Path,
    heading: str,
    node_id: str,
    contract: dict,
    claims: list[dict],
    schema: dict,
    out: Path,
    timeout: int,
    feedback: str,
) -> dict:
    section_dir = out / heading
    section_dir.mkdir(parents=True, exist_ok=True)
    effective_schema = sanitize_output_schema(copy.deepcopy(schema))
    effective_schema["properties"]["node_id"] = {"type": "string", "const": node_id}
    effective_schema["properties"]["heading"] = {"type": "string", "const": heading}
    schema_path = section_dir / "schema.json"
    schema_path.write_text(json.dumps(effective_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target_sentences = section_minimum_sentences(heading)
    claim_payload = [{
        "claim_id": row["claim"]["claim_id"],
        "claim_kind": row["claim"]["claim_kind"],
        "claim_text": row["claim"]["claim_text"],
        "verdict": (row.get("verify") or {}).get("verdict"),
    } for row in claims]
    prompt = (
        "你是节点 Wiki 的章节编辑。无工具、无网络，只写一个中文技术百科章节。"
        "研究 claim 是证据账，不得逐条照抄或按输入顺序罗列；只选用与本节论点直接相关的 claim，未选用项保留在证据账中；"
        "选用的 claim 通过 evidence_claim_ids 映射且最多出现三次，"
        "语义重复或互补者应融合成一句自然事实句，一句可映射多 claim，但不得扩大原义。"
        "每段有唯一 focus、2-4 句、第一句为唯一 thesis，后句必须解释、限定、应用或给出 LCA 含义；"
        "每段最多一个 external_fact，新增内容只能是 modeling_judgment/evidence_gap 且 evidence_claim_ids=[]。"
        "统一产品身份和术语；不得把刀片服务器与服务器刀片虚构成不同层级。"
        f"本节至少形成一个完整段落（不少于 {target_sentences} 句），优先保证逻辑密度；不设最低字符数且不得堆句。"
        "不得编造数值、法规、供应商事实或具名型号。输出只匹配 schema。\n"
        f"NODE_ID={node_id}\nHEADING={heading}\nCONTRACT={json.dumps(contract, ensure_ascii=False)}\n"
        f"CLAIMS={json.dumps(claim_payload, ensure_ascii=False)}"
        + (f"\nEDITORIAL_FEEDBACK={feedback}" if feedback else "")
    )
    last_exit = 1
    for attempt in range(1, 3):
        raw = section_dir / f"result-attempt-{attempt}.json"
        events = section_dir / f"events-attempt-{attempt}.jsonl"
        stderr = section_dir / f"stderr-attempt-{attempt}.log"
        command = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                   "-C", str(root), "-s", "read-only", "-m", "gpt-5.6-terra",
                   "-c", 'model_reasoning_effort="medium"']
        for feature in DISABLED:
            command.extend(["--disable", feature])
        command.extend(["--json", "--output-schema", str(schema_path), "-o", str(raw), prompt])
        with events.open("w", encoding="utf-8") as event_stream, stderr.open("w", encoding="utf-8") as error_stream:
            try:
                completed = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, stdout=event_stream,
                                           stderr=error_stream, text=True, check=False, timeout=timeout)
                last_exit = completed.returncode
            except subprocess.TimeoutExpired:
                last_exit = 124
        if last_exit == 0 and raw.is_file():
            value = json.loads(raw.read_text(encoding="utf-8"))
            text_chars = sum(len(sentence["text"]) for paragraph in value["paragraphs"] for sentence in paragraph["sentences"])
            sentence_count = sum(len(paragraph["sentences"]) for paragraph in value["paragraphs"])
            if sentence_count >= target_sentences:
                final = section_dir / "section.json"
                raw.replace(final)
                return {"heading": heading, "exit_code": 0, "path": str(final),
                        "text_chars": text_chars, "sentences": sentence_count, "attempt": attempt}
            last_exit = 2
    return {"heading": heading, "exit_code": last_exit, "path": None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("section_schema", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--editorial-feedback", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    blueprint = json.loads(args.blueprint.resolve().read_text(encoding="utf-8"))
    rows = _claims(args.verify_output.resolve(), blueprint["node_id"])
    by_section = {heading: [] for heading in blueprint["sections"]}
    for row in rows:
        by_section[row["claim"]["section"]].append(row)
    schema = json.loads(args.section_schema.resolve().read_text(encoding="utf-8"))
    feedback = args.editorial_feedback.resolve().read_text(encoding="utf-8") if args.editorial_feedback else ""
    reports = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(generate_section, root, heading, blueprint["node_id"], contract,
                            by_section[heading], schema, out, args.timeout_seconds, feedback): heading
            for heading, contract in blueprint["sections"].items()
        }
        for future in as_completed(futures):
            reports.append(future.result())
    reports.sort(key=lambda item: list(blueprint["sections"]).index(item["heading"]))
    failed = [item for item in reports if item["exit_code"] != 0]
    report_path = out / "sectioned-capture-report.json"
    if failed:
        report = {"protocol": "wiki-content-sectioned-capture-v1", "verdict": "NO_GO", "sections": reports}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 2
    document = {"protocol": "wiki-content-draft-v2", "node_id": blueprint["node_id"], "sections": []}
    for item in reports:
        section = json.loads(Path(item["path"]).read_text(encoding="utf-8"))
        document["sections"].append({"heading": section["heading"], "paragraphs": section["paragraphs"]})
    result = out / "content-result.json"
    result.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        scorecard = validate_result(result, blueprint, rows)
        verdict, error = "GO", None
    except ValueError as exc:
        scorecard, verdict, error = None, "NO_GO", str(exc)
    report = {"protocol": "wiki-content-sectioned-capture-v1", "verdict": verdict,
              "sections": reports, "scorecard": scorecard, "error": error}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if verdict == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
