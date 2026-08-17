#!/usr/bin/env python3
"""Run one frozen no-Web name-graph SOP phase and persist its attestation."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess

DISABLED = ["browser_use", "in_app_browser", "computer_use", "standalone_web_search",
            "remote_plugin", "plugins", "apps", "multi_agent"]


def graph_schema() -> dict:
    output = {"type":"object","required":["product","role"],"properties":{
        "product":{"type":"string"},"role":{"type":"string","enum":["reference","coproduct"]}},
        "additionalProperties":False}
    product = {"type":"object","required":["name","facets","provenance","confidence"],"properties":{
        "name":{"type":"string"},"facets":{"type":"object","additionalProperties":{"type":"string"}},
        "cpc":{"type":"string"},"hs":{"type":"string"},
        "boundary":{"type":"string","enum":["foreground","background"]},
        "home_industry":{"type":"string"},"provenance":{"type":"array","items":{"type":"string"}},
        "confidence":{"type":"string","enum":["core","longtail"]}},"additionalProperties":False}
    activity = {"type":"object","required":["name","facets","inputs","outputs","provenance","confidence"],
        "properties":{"name":{"type":"string"},"facets":{"type":"object","additionalProperties":{"type":"string"}},
        "isic":{"type":"string"},"bref":{"type":"string"},"inputs":{"type":"array","items":{"type":"string"}},
        "outputs":{"type":"array","items":output},"provenance":{"type":"array","items":{"type":"string"}},
        "confidence":{"type":"string","enum":["core","longtail"]}},"additionalProperties":False}
    return {"type":"object","required":["products","activities"],"properties":{
        "products":{"type":"array","items":product},"activities":{"type":"array","items":activity}},
        "additionalProperties":False}


def schema_for(phase: str) -> dict:
    if phase == "conventions":
        facet = {"type":"object","required":["name","description","controlled_values","rationale"],
                 "properties":{"name":{"type":"string"},"description":{"type":"string"},
                 "controlled_values":{"type":"array","items":{"type":"string"}},"rationale":{"type":"string"}},
                 "additionalProperties":False}
        return {"type":"object","required":["boundary","product_facets","activity_facets","non_identity_facets",
            "synonym_table","product_naming_template","activity_naming_template","entity_schema"],"properties":{
            "boundary":{"type":"object","additionalProperties":True},"product_facets":{"type":"array","items":facet},
            "activity_facets":{"type":"array","items":facet},"non_identity_facets":{"type":"array","items":{"type":"object","additionalProperties":True}},
            "synonym_table":{"type":"array","items":{"type":"object","additionalProperties":True}},
            "product_naming_template":{"type":"string"},"activity_naming_template":{"type":"string"},
            "entity_schema":{"type":"object","additionalProperties":True},
            "interface_contract_response":{"type":"array","items":{"type":"object","additionalProperties":True}}},
            "additionalProperties":False}
    if phase in {"seed", "build", "closure", "consolidate"}:
        return graph_schema()
    if phase == "mapping":
        mapped = {"type":"object","required":["node","code","kind"],"properties":{
            "node":{"type":"string"},"code":{"type":"string"},"kind":{"type":"string"}},
            "additionalProperties":False}
        gap = {"type":"object","required":["code","description","suggested_kind","suggested_node_json"],
               "properties":{"code":{"type":"string"},"description":{"type":"string"},
               "suggested_kind":{"type":"string","enum":["product","activity","none"]},
               "suggested_node_json":{"type":"string"}},"additionalProperties":False}
        return {"type":"object","required":["classification","mapped","gaps"],"properties":{
            "classification":{"type":"string"},"mapped":{"type":"array","items":mapped},
            "gaps":{"type":"array","items":gap}},"additionalProperties":False}
    if phase == "review":
        finding = {"type":"object","required":["kind","name","reason","facets","inputs","outputs"],
                   "properties":{"kind":{"type":"string","enum":["product","activity"]},
                   "name":{"type":"string"},"reason":{"type":"string"},
                   "facets":{"type":"object","additionalProperties":{"type":"string"}},
                   "inputs":{"type":"array","items":{"type":"string"}},
                   "outputs":{"type":"array","items":{"type":"object","required":["product","role"],
                       "properties":{"product":{"type":"string"},"role":{"type":"string","enum":["reference","coproduct"]}},
                       "additionalProperties":False}}},"additionalProperties":False}
        return {"type":"object","required":["missing"],"properties":{
            "missing":{"type":"array","items":finding}},"additionalProperties":False}
    return {"type":"object","required":["rows","overall","longtail_notes"],"properties":{
        "rows":{"type":"array","items":{"type":"object","additionalProperties":True}},"overall":{"type":"string"},
        "longtail_notes":{"type":"array","items":{"type":"string"}}},"additionalProperties":False}


def lean(value: object) -> object:
    if not isinstance(value, dict) or not isinstance(value.get("products"), list):
        return value
    keys = {"name", "facets", "confidence", "boundary", "home_industry", "cpc", "hs", "isic", "bref", "inputs", "outputs"}
    return {kind:[{k:v for k,v in row.items() if k in keys} for row in value.get(kind, [])]
            for kind in ("products", "activities")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("conventions","seed","build","closure","mapping","review","consolidate","scorecard"))
    parser.add_argument("plan", type=Path); parser.add_argument("output_dir", type=Path)
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument("--model", required=True); parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--scope", default="")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    supplied = []
    for path in args.inputs:
        value = json.loads(path.read_text(encoding="utf-8"))
        supplied.append({"name":path.name,"value":lean(value) if args.phase in {"closure","mapping","review","scorecard"} else value})
    instructions = {
      "conventions":"锁定边界、身份刻面、受控词表、同义词和确定性命名模板；逐条响应接口契约。不要产出节点。",
      "seed":"按计划中的四类来源分别枚举后合并候选产品和活动；保留 provenance。只提名，不裁决发布。",
      "build":"依据 conventions 对 seed 去同义、按身份键合并，建立闭合前的规范图。inputs/outputs 必须使用规范产品名。",
      "closure":"检查并修复 A无孤儿产品、B每活动恰一参考产出、C无悬空输入；最多做四轮语义闭合并返回最终图。",
      "mapping":"只按本任务 scope 对精简图做外部分类对表。mapped 给 node/code/kind；gaps 给 code/description/suggested_kind，并把完整建议节点编码为 suggested_node_json。源驱动，不确定的码留空；不直接改图。",
      "review":"只按本任务 scope 以独立审查者视角找边界、粒度、遗漏、共产品和身份碰撞问题；只返回当前图确实缺失的 missing，不得改图。",
      "consolidate":"把 mapping gaps 和 review findings 合并回全图，去重、补分类码并保持 provenance；不得生成 id/edges。",
      "scorecard":"为最终精简图输出完整性记分卡；代码 A-E Gate 尚未运行，不得声称 11/11 PASS。",
    }[args.phase]
    prompt = ("你是LCA行业名称图生产代理。不得联网、读文件、调用工具或其他代理。Agent输出永远只是 proposal/review。"
              + instructions + (f"\n本任务唯一视角/来源范围={args.scope}；不得替代其他并行任务。" if args.scope else "")
              + "\nPLAN=" + json.dumps(plan, ensure_ascii=False, separators=(",",":"))
              + "\nFROZEN_INPUTS=" + json.dumps(supplied, ensure_ascii=False, separators=(",",":")))
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    schema_path, raw, result = out/"output.schema.json", out/"result.raw.json", out/f"{args.phase}.json"
    events, stderr = out/"events.jsonl", out/"stderr.log"
    schema_path.write_text(json.dumps(schema_for(args.phase), ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    command = ["codex","exec","--ephemeral","--ignore-user-config","--ignore-rules","-C",str(out),"-s","read-only",
               "-m",args.model,"-c",f'model_reasoning_effort="{args.reasoning_effort}"']
    for feature in DISABLED: command += ["--disable",feature]
    command += ["--json","--output-schema",str(schema_path),"-o",str(raw),prompt]
    invocation = {"protocol":"graph-agent-invocation-v1","phase":args.phase,"started_at":dt.datetime.now(dt.timezone.utc).isoformat(),
                  "model":args.model,"reasoning_effort":args.reasoning_effort,"disabled_capabilities":DISABLED,
                  "plan_sha256":hashlib.sha256(args.plan.read_bytes()).hexdigest(),"input_sha256":[hashlib.sha256(p.read_bytes()).hexdigest() for p in args.inputs]}
    (out/"invocation.json").write_text(json.dumps(invocation,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    with events.open("w") as eo, stderr.open("w") as er:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=eo, stderr=er, text=True, timeout=1800)
    usage = {"protocol":"graph-agent-usage-v1","exit_code":completed.returncode,"phase":args.phase}
    (out/"usage.json").write_text(json.dumps(usage,indent=2)+"\n",encoding="utf-8")
    if completed.returncode != 0 or not raw.is_file(): return 1
    json.loads(raw.read_text(encoding="utf-8")); raw.replace(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
