#!/usr/bin/env python3
"""Generate a blueprint-bound, no-Web Wiki content draft and freeze the run."""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import math
import re
import subprocess
from collections import Counter
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


def sanitize_output_schema(value):
    """Remove API-unsupported constraints; local validation remains strict."""
    if isinstance(value, dict):
        return {key: sanitize_output_schema(item) for key, item in value.items() if key != "uniqueItems"}
    if isinstance(value, list):
        return [sanitize_output_schema(item) for item in value]
    return value


def _unwrapped(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("result") if isinstance(data.get("result"), dict) else data


def _claims(path: Path, node_id: str) -> list[dict]:
    rows = _unwrapped(path).get("claims")
    if not isinstance(rows, list):
        raise ValueError("Verify output 缺少 claims")
    selected = [row for row in rows if (row.get("claim") or {}).get("node_id") == node_id]
    if not selected or len(selected) != len(rows):
        raise ValueError("Content capture 一次只允许一个节点")
    return selected


def validate_result(path: Path, blueprint: dict, rows: list[dict]) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    node_id = blueprint["node_id"]
    if document.get("protocol") != "wiki-content-draft-v2" or document.get("node_id") != node_id:
        raise ValueError("Content result protocol/node_id 漂移")
    sections = document.get("sections")
    expected_headings = list(blueprint["sections"])
    if not isinstance(sections, list) or [s.get("heading") for s in sections] != expected_headings:
        raise ValueError("Content result 九节标题或顺序漂移")
    base = {row["claim"]["claim_id"]: row for row in rows}
    eligible = {
        claim_id for claim_id, row in base.items()
        if row["claim"].get("claim_kind") != "external_fact"
        or (row.get("verify") or {}).get("verdict") == "CONFIRMED"
    }
    used: list[str] = []
    texts: set[str] = set()
    normalized_texts: list[tuple[str, str]] = []
    focuses: set[str] = set()
    kinds: dict[str, int] = {}
    paragraphs = 0
    single = 0
    rendered: list[str] = []
    for section in sections:
        heading = section["heading"]
        parts = section.get("paragraphs") or []
        minimum = int(blueprint["sections"][heading]["minimum_paragraphs"])
        if len(parts) < minimum:
            raise ValueError(f"{heading} 段落不足: {len(parts)}<{minimum}")
        paragraphs += len(parts)
        for paragraph in parts:
            focus = re.sub(r"\s+", " ", str(paragraph.get("focus", ""))).strip()
            if len(focus) < 8 or focus in focuses:
                raise ValueError(f"{heading} 段落 focus 过短或重复")
            focuses.add(focus)
            sentences = paragraph.get("sentences") or []
            if len(sentences) == 1:
                single += 1
            maximum_sentences = int(blueprint["golden_target"].get("maximum_sentences_per_paragraph", 4))
            if not 2 <= len(sentences) <= maximum_sentences:
                raise ValueError(f"{heading} 段落必须包含 2-{maximum_sentences} 句")
            roles = [str(sentence.get("rhetorical_role", "")) for sentence in sentences]
            if roles[0] != "thesis" or roles.count("thesis") != 1:
                raise ValueError(f"{heading} 每段必须以唯一 thesis 开头")
            external_in_paragraph = sum(
                str(sentence.get("claim_kind", "")) == "external_fact"
                for sentence in sentences
            )
            maximum_external = int(blueprint["golden_target"].get("maximum_external_facts_per_paragraph", 1))
            if external_in_paragraph > maximum_external:
                raise ValueError(f"{heading} 单段外部事实锚点过多: {external_in_paragraph}>{maximum_external}")
            for sentence in sentences:
                text = re.sub(r"\s+", " ", str(sentence.get("text", ""))).strip()
                kind = str(sentence.get("claim_kind", ""))
                evidence_ids = sentence.get("evidence_claim_ids") or []
                if len(text) < 10 or text in texts:
                    raise ValueError("Content sentence 过短或重复")
                texts.add(text)
                comparable = re.sub(r"[\W_]+", "", text)
                normalized_texts.append((heading, comparable))
                kinds[kind] = kinds.get(kind, 0) + 1
                rendered.append(text)
                if not isinstance(evidence_ids, list) or len(evidence_ids) != len(set(evidence_ids)):
                    raise ValueError("evidence_claim_ids 必须是无重复数组")
                if kind in {"external_fact", "internal_graph_fact"} and not evidence_ids:
                    raise ValueError(f"{kind} 正文句必须映射冻结证据 claim")
                if not evidence_ids and kind not in {"modeling_judgment", "evidence_gap"}:
                    raise ValueError("无冻结证据的新增内容只能是建模判断或证据缺口")
                for source_id in evidence_ids:
                    row = base.get(str(source_id))
                    if row is None:
                        raise ValueError(f"未知 evidence claim: {source_id}")
                    claim = row["claim"]
                    if kind in {"external_fact", "internal_graph_fact"} and claim["claim_kind"] != kind:
                        raise ValueError(f"冻结 claim 类型映射漂移: {source_id}")
                    if claim["claim_kind"] == "external_fact" and (row.get("verify") or {}).get("verdict") != "CONFIRMED":
                        raise ValueError(f"未 CONFIRMED 的外部 claim 不得进入事实正文: {source_id}")
                    used.append(str(source_id))
    use_counts = Counter(used)
    missing = sorted(eligible - set(use_counts))
    overused = sorted(source_id for source_id, count in use_counts.items() if count > 3)
    if missing or overused:
        raise ValueError(f"冻结研究 claim 必须至少映射一次、最多三次: missing={missing}, overused={overused}")
    target = blueprint["golden_target"]
    duplicate_limit = float(target.get("maximum_near_duplicate_ratio", 1.0))
    worst_duplicate = 0.0
    duplicate_pair: tuple[str, str] | None = None
    for index, (left_heading, left) in enumerate(normalized_texts):
        if len(left) < 12:
            continue
        for right_heading, right in normalized_texts[index + 1:]:
            if len(right) < 12:
                continue
            ratio = difflib.SequenceMatcher(None, left, right).ratio()
            if ratio > worst_duplicate:
                worst_duplicate, duplicate_pair = ratio, (left_heading, right_heading)
    if worst_duplicate > duplicate_limit:
        raise ValueError(
            f"正文存在近重复句: ratio={worst_duplicate:.3f}>{duplicate_limit}; sections={duplicate_pair}"
        )
    body = "\n".join(rendered)
    checks = {
        "body_chars": len(body) >= int(target["minimum_body_chars"]),
        "assertions": len(rendered) >= int(target["minimum_assertions"]),
        "assertions_not_stuffed": len(rendered) <= int(target.get("maximum_assertions", 10**9)),
        "paragraphs": paragraphs >= int(target["minimum_paragraphs"]),
        "modeling": kinds.get("modeling_judgment", 0) >= int(target["minimum_modeling_judgments"]),
        "modeling_not_stuffed": kinds.get("modeling_judgment", 0) <= int(target.get("maximum_modeling_judgments", 10**9)),
        "single_sentence_ratio": (single / paragraphs if paragraphs else 1) <= float(target["maximum_single_sentence_paragraph_ratio"]),
        "paragraph_focuses_unique": len(focuses) == paragraphs,
        "near_duplicate_free": worst_duplicate <= duplicate_limit,
        "required_tokens": all(token in body for token in blueprint.get("required_tokens", [])),
        "forbidden_phrases": not any(phrase in body for phrase in blueprint.get("forbidden_phrases", [])),
    }
    if not all(checks.values()):
        raise ValueError(f"Content Golden contract 失败: {checks}")
    return {"body_chars": len(body), "assertions": len(rendered), "paragraphs": paragraphs,
            "modeling_judgments": kinds.get("modeling_judgment", 0),
            "maximum_near_duplicate_ratio": round(worst_duplicate, 4), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify_output", type=Path)
    parser.add_argument("blueprint", type=Path)
    parser.add_argument("schema", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--cost-usd", type=float, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--editorial-feedback", type=Path)
    parser.add_argument("--model", default="gpt-5.6-terra")
    parser.add_argument("--reasoning-effort", default="medium", choices=("low", "medium", "high"))
    args = parser.parse_args()
    if not math.isfinite(args.cost_usd) or args.cost_usd < 0:
        raise ValueError("--cost-usd 必须是非负有限数")
    root = Path(__file__).resolve().parents[1]
    verify_path, blueprint_path, schema_path = (args.verify_output.resolve(), args.blueprint.resolve(), args.schema.resolve())
    blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
    rows = _claims(verify_path, blueprint["node_id"])
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    effective_schema = out / "content-output.schema.json"
    schema = sanitize_output_schema(json.loads(schema_path.read_text(encoding="utf-8")))
    schema["properties"]["node_id"] = {"type": "string", "const": blueprint["node_id"]}
    effective_schema.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raw, result = out / "content-result.raw.json", out / "content-result.json"
    events, stderr = out / "content-events.jsonl", out / "content-stderr.log"
    invocation, usage = out / "content-invocation.json", out / "content-usage.json"
    allowed_rows = [r for r in rows if r["claim"].get("claim_kind") != "external_fact"
                    or (r.get("verify") or {}).get("verdict") == "CONFIRMED"]
    forbidden_ids = sorted({r["claim"]["claim_id"] for r in rows} -
                           {r["claim"]["claim_id"] for r in allowed_rows})
    claims_prompt = [{"claim_id": r["claim"]["claim_id"], "section": r["claim"]["section"],
                      "claim_kind": r["claim"]["claim_kind"], "claim_text": r["claim"]["claim_text"]} for r in allowed_rows]
    feedback = ""
    if args.editorial_feedback:
        feedback_path = args.editorial_feedback.resolve()
        feedback = "\n上一轮独立编辑审查意见，必须逐项修复：" + feedback_path.read_text(encoding="utf-8")
    prompt = (
        "你是节点 Wiki Content Architect。不得读取文件、调用工具、联网、搜索、调用浏览器或其他 agent。"
        "只根据下面冻结的 Content Blueprint 和已裁决 claim ledger 形成 Golden-depth 中文正文结构。"
        "九节标题和顺序必须与 blueprint 完全一致。研究 claim 是证据账，不是正文句：禁止逐条照抄或按输入顺序罗列。"
        "每条输入中的可用 claim 必须通过 evidence_claim_ids 至少映射一次、最多三次；同一证据可在相关章节复用，语义重复或互补的 claim 应融合成一条自然正文句，"
        "一条句子可映射多条 claim，但不得扩大外部事实含义。新增判断的 evidence_claim_ids=[]，只能标为 modeling_judgment"
        "或 evidence_gap。不能编造 LCI 数值、型号参数、法规要求或供应商事实。"
        "每段先写唯一、具体的 focus；2-4 句且第一句 rhetorical_role=thesis。其余句必须解释、限定、应用或说明该 thesis 的 LCA 含义，"
        "禁止把互不相干的字段句并排。每段最多一个 external_fact 事实锚点；需要两条来源时应在不扩大语义的前提下融合为一句并列引用。"
        "先统一术语和产品层级，尤其不得把同义的‘刀片服务器/服务器刀片’虚构成两个层级。"
        "字符深度是硬约束：按所有 sentence.text 拼接后的中文字符数至少 6800（为 6500 Gate 留出缓冲），但总断言不得超过 blueprint 上限；"
        "建模句通常应有 55-100 个中文字符，围绕段落 focus 具体交代对象、理由、记录字段、适用条件或失效条件。"
        "证据缺口必须明确：身份事实已有核验，缺的是型号级 BOM、质量、制造、运输或共享资源分配等前景 LCI；"
        "不得笼统写没有节点证据。达到 blueprint 的全部定量阈值、主题词和禁用短语约束。输出只匹配 JSON schema。\n"
        f"BLUEPRINT={json.dumps(blueprint, ensure_ascii=False, separators=(',', ':'))}\n"
        f"CLAIMS={json.dumps(claims_prompt, ensure_ascii=False, separators=(',', ':'))}\n"
        f"FORBIDDEN_EVIDENCE_CLAIM_IDS={json.dumps(forbidden_ids, ensure_ascii=False)}；这些 ID 已被确定性过滤，绝不能出现在 evidence_claim_ids 中。{feedback}"
    )
    command = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "-C", str(root),
               "-s", "read-only", "-m", args.model, "-c", f'model_reasoning_effort="{args.reasoning_effort}"']
    for feature in DISABLED: command.extend(["--disable", feature])
    command.extend(["--json", "--output-schema", str(effective_schema), "-o", str(raw), prompt])
    invocation.write_text(json.dumps({"protocol": "wiki-content-runtime-v1", "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cwd": str(root), "argv": command, "model": args.model, "reasoning_effort": args.reasoning_effort, "sandbox": "read-only",
        "disabled_capabilities": DISABLED, "node_id": blueprint["node_id"], "verify_output": str(verify_path),
        "verify_sha256": sha256(verify_path), "blueprint": str(blueprint_path), "blueprint_sha256": sha256(blueprint_path),
        "schema_sha256": sha256(effective_schema)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    exit_code, validation_error, scorecard = 124, None, None
    with events.open("w", encoding="utf-8") as event_stream, stderr.open("w", encoding="utf-8") as error_stream:
        try:
            completed = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, stdout=event_stream,
                                       stderr=error_stream, text=True, check=False, timeout=max(1, args.timeout_seconds))
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            validation_error = "Content runtime timeout"
    if exit_code == 0:
        try:
            raw.replace(result)
            scorecard = validate_result(result, blueprint, rows)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            validation_error, exit_code = str(exc), 2
    usage_rows: list[dict] = []
    for line in events.read_text(encoding="utf-8").splitlines():
        try: collect_usage(json.loads(line), usage_rows)
        except json.JSONDecodeError: pass
    usage.write_text(json.dumps({"protocol": "wiki-content-runtime-usage-v1", "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "exit_code": exit_code, "validation_error": validation_error, "scorecard": scorecard, "usage_records": usage_rows,
        "artifacts": {"invocation_sha256": sha256(invocation), "events_sha256": sha256(events), "stderr_sha256": sha256(stderr),
                      "result_sha256": sha256(result) if result.exists() else None}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "wiki-usage-v1.json").write_text(json.dumps({"protocol": {"version": "wiki-usage-v1", "kind": "usage"},
        "phase": "content", "model": args.model, "reasoning_effort": args.reasoning_effort, "search_requests": 0,
        "cost_usd": args.cost_usd, "runtime_usage_sha256": sha256(usage), "runtime_invocation_sha256": sha256(invocation)},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exit_code": exit_code, "result": str(result), "scorecard": scorecard,
                      "validation_error": validation_error}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
