"""Uniform Capability.v1 adapter for frozen production tools.

Every invocation consumes one input JSON and produces one output JSON.  The
adapter never guesses script order; the Workflow task supplies an operation
and only allow-listed operation/argument shapes are accepted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit


class CapabilityAdapterError(RuntimeError):
    pass


WIKI_OPERATIONS = {
    "plan", "prepare", "validate", "research-ready", "verify", "finalize",
    "apply", "preview", "go-no-go", "gate", "publish", "content-blueprint",
    "draft-content-pipeline", "node-preview", "research-plan", "search-execution-gate", "terminology-verify",
    "source-diversity-gate", "research-plan-gate", "content-closure-gate", "maturity-gate",
    "table-search-execution-gate", "table_population_gate", "release-gate",
}


def _run(command: list[str], *, cwd: Path, timeout: int,
         blocked_code: str | None = None) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True,
                               timeout=timeout, check=False)
    if completed.returncode:
        stderr = completed.stderr[-8000:]
        if blocked_code and completed.returncode == 2:
            return {"status": "blocked", "failure": {
                "code": blocked_code, "category": "business_validation", "scope": "task",
                "message": stderr.strip() or f"gate returned blocked ({completed.returncode})",
            }, "stdout": completed.stdout[-8000:], "stderr": stderr}
        return {"status": "failed", "failure": {
            "code": "CAPABILITY_PROCESS_FAILED", "category": "infrastructure", "scope": "task",
            "message": stderr.strip() or f"child command exited {completed.returncode}",
        }, "stdout": completed.stdout[-8000:], "stderr": stderr,
            "returncode": completed.returncode}
    return {"status": "ok", "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]}


def _diversity_repair_scout(batch: Path, scout_path: Path) -> Path:
    """Freeze a replacement pool excluding prior fetch/verification failures."""
    gate_path = batch / "source-diversity-gate.json"
    if not gate_path.is_file():
        return scout_path
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return scout_path
    if gate.get("decision") not in {"REPAIR", "BLOCKED"}:
        return scout_path
    exclusion_reasons: dict[str, set[str]] = {}
    saw_failed_pdf = False
    for record_path in sorted((batch / "search-cache/fetch").glob("*.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8")).get("record") or {}
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record.get("status") == "fetched":
            continue
        url = str(record.get("url") or "").strip()
        if url:
            exclusion_reasons.setdefault(url, set()).add(
                f"fetch_status:{str(record.get('status') or 'unknown')}"
            )
            saw_failed_pdf = saw_failed_pdf or urlsplit(url).path.lower().endswith(".pdf")
    verify_path = batch / "verify-output.json"
    if verify_path.is_file():
        try:
            verified = json.loads(verify_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CapabilityAdapterError("diversity repair verify-output is unreadable") from exc
        rows = verified.get("claims") or verified.get("result", {}).get("claims") or []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            verdict = str((row.get("verify") or {}).get("verdict") or "").strip()
            url = str((row.get("fetchResult") or {}).get("url") or "").strip()
            if url and verdict != "CONFIRMED":
                exclusion_reasons.setdefault(url, set()).add(
                    f"verify_verdict:{verdict or 'MISSING'}"
                )
    scout = json.loads(scout_path.read_text(encoding="utf-8"))
    candidates: list[dict[str, Any]] = []
    for row in scout.get("candidates", []):
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if saw_failed_pdf and urlsplit(url).path.lower().endswith(".pdf"):
            exclusion_reasons.setdefault(url, set()).add("pdf_format_after_failed_pdf_fetch")
        if url not in exclusion_reasons:
            candidates.append({**row, "current_job_status": "candidate_unverified"})
    plan_path = batch / "research-plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CapabilityAdapterError("diversity repair research plan is unreadable") from exc
    minimum = plan.get("minimum_source_diversity") or {}
    required_domains = max(
        int(minimum.get("preview_distinct_domains", 3)),
        int(minimum.get("reviewed_distinct_domains", 3)),
    )
    declared_languages = {
        str(language).strip().lower().split("-", 1)[0]
        for language in plan.get("languages", []) if str(language).strip()
    }
    default_language_tracks = min(2, len(declared_languages))
    required_languages = max(
        int(minimum.get("preview_language_tracks", default_language_tracks)),
        int(minimum.get("reviewed_language_tracks", default_language_tracks)),
    )
    domains = {
        (urlsplit(str(row.get("url") or "")).hostname or "").lower()
        for row in candidates
    } - {""}
    languages = {
        str(row.get("language") or "").strip().lower().split("-", 1)[0]
        for row in candidates if str(row.get("language") or "").strip()
    }
    available_languages = languages & declared_languages if declared_languages else languages
    if len(domains) < required_domains or len(available_languages) < required_languages:
        raise CapabilityAdapterError(
            "diversity repair exclusions leave fewer candidates than the frozen "
            "domain/language availability preconditions"
        )
    output = batch / "research-scout-diversity-repair.json"
    repaired = {**scout, "candidates": candidates, "diversity_repair": {
        "protocol": "wiki-source-diversity-repair-v1",
        "excluded_urls": sorted(exclusion_reasons),
        "exclusion_reasons": {
            url: sorted(reasons) for url, reasons in sorted(exclusion_reasons.items())
        },
        "excluded_pdf_candidates": saw_failed_pdf,
        "candidate_pool_preconditions": {
            "required_domains": required_domains,
            "available_domains": len(domains),
            "required_language_tracks": required_languages,
            "available_language_tracks": sorted(available_languages),
            "satisfied": True,
            "candidate_status": "candidate_unverified",
        },
        "trigger_gate_sha256": hashlib.sha256(gate_path.read_bytes()).hexdigest(),
        "trigger_verify_sha256": _sha256(verify_path) if verify_path.is_file() else None,
    }}
    output.write_text(json.dumps(repaired, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return output


def _pipeline(commands: list[list[str]], *, cwd: Path, timeout: int) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for command in commands:
        result = _run(command, cwd=cwd, timeout=timeout)
        records.append({"argv": command, **result})
        if result["status"] != "ok":
            failure = result.get("failure") or {}
            return {"status": "failed", "failure": {**failure, "step": command[1:3]},
                    "steps": records}
    return {"status": "ok", "steps": records}


def _path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CapabilityAdapterError(f"{label} must be a path string")
    return Path(value).resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reusable_executed_table_matrix(plan_path: Path, executed_path: Path) -> bool:
    """Return true only for a completed execution bound to the current plan bytes."""
    if not plan_path.is_file() or not executed_path.is_file():
        return False
    try:
        executed = json.loads(executed_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    rows = executed.get("queries")
    manifest = Path(str(executed.get("execution_manifest") or ""))
    completed_statuses = {"fetched", "found", "not_found", "not_selected"}
    return (
        executed.get("protocol") == "wiki-table-search-executed-v2"
        and executed.get("coverage_status") == "executed"
        and executed.get("plan_sha256") == _sha256(plan_path)
        and isinstance(rows, list)
        and bool(rows)
        and all(isinstance(row, dict) and row.get("status") in completed_statuses for row in rows)
        and manifest.is_file()
    )


def graph_batch(value: dict[str, Any]) -> dict[str, Any]:
    """Protocol adapter for deterministic name-graph staging operations."""
    operation = str(value.get("operation", ""))
    if operation == "probe":
        return {"status": "ok", "adapter": "graph.batch",
                "operations": ["plan", "materialize_reconcile"]}
    workspace = _path(value.get("workspace"), "workspace")
    if operation == "plan":
        request = value.get("request")
        profile = _path(value.get("profile"), "profile")
        if not isinstance(request, dict) or not isinstance(request.get("industry"), str):
            raise CapabilityAdapterError("graph plan requires a frozen industry request")
        if not profile.is_file():
            raise CapabilityAdapterError("graph production profile is missing")
        plan = {
            "protocol": "graph-plan-v1", "request": request,
            "profile": json.loads(profile.read_text(encoding="utf-8")),
            "authority": {"agent_output": "proposal_only", "release": "deterministic_gate_only"},
        }
        output = _path(value.get("output"), "output")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"status": "ok", "plan": str(output), "plan_sha256": _sha256(output)}
    if operation != "materialize_reconcile":
        raise CapabilityAdapterError(f"unsupported graph.batch operation: {operation}")
    plan_path = _path(value.get("plan"), "plan")
    paths = [_path(value.get(key), key) for key in ("conventions", "final_graph")]
    mappings = value.get("mappings")
    if mappings is None and value.get("mapping"):
        mappings = [value["mapping"]]
    if not isinstance(mappings, list) or not mappings:
        raise CapabilityAdapterError("materialize_reconcile requires mapping artifacts")
    paths.extend(_path(item, "mapping") for item in mappings)
    paths.append(_path(value.get("scorecard"), "scorecard"))
    journal = _path(value.get("journal"), "journal")
    candidate = _path(value.get("candidate"), "candidate")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    request = plan["request"]
    records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("".join(json.dumps({"type": "result", "result": row},
                                           ensure_ascii=False, separators=(",", ":")) + "\n"
                               for row in records), encoding="utf-8")
    scripts = workspace / "scripts"
    materialize = _run([
        sys.executable, str(scripts / "materialize.py"), str(journal),
        str(request["industry"]), str(request.get("display_name") or request["industry"]),
        str(candidate),
    ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    if materialize["status"] != "ok":
        return materialize
    if request.get("generated"):
        document = json.loads(candidate.read_text(encoding="utf-8"))
        document["_meta"]["generated"] = request["generated"]
        candidate.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    reconcile = _run([sys.executable, str(scripts / "reconcile.py"), str(candidate)],
                     cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    result = {**reconcile, "steps": [materialize, reconcile]}
    if reconcile["status"] == "ok":
        result.update({"candidate": str(candidate), "candidate_sha256": _sha256(candidate)})
    return result


def graph_gate(value: dict[str, Any]) -> dict[str, Any]:
    """Run the original 11 deterministic checks and bind the report to bytes."""
    operation = str(value.get("operation", ""))
    if operation == "probe":
        return {"status": "ok", "adapter": "graph.gate", "checks": 11,
                "policy": "graph-quality-v1"}
    if operation != "validate_11":
        raise CapabilityAdapterError(f"unsupported graph.gate operation: {operation}")
    workspace = _path(value.get("workspace"), "workspace")
    candidate = _path(value.get("candidate"), "candidate")
    report_path = _path(value.get("report"), "report")
    completed = subprocess.run(
        [sys.executable, str(workspace / "scripts/validate_graph.py"), str(candidate)],
        cwd=workspace, text=True, capture_output=True,
        timeout=int(value.get("timeout_seconds", 900)), check=False,
    )
    checks = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("✅", "❌")):
            checks.append({"status": "pass" if stripped.startswith("✅") else "fail",
                           "check": stripped[1:].strip()})
    passed = completed.returncode == 0 and len(checks) == 11 and all(
        row["status"] == "pass" for row in checks)
    report = {
        "protocol": "graph-gate-report-v2", "gate_id": "G6",
        "policy_version": "graph-quality-v1", "decision": "PASS" if passed else "FAIL",
        "candidate_sha256": _sha256(candidate), "checks": checks,
        "summary": {"passed": sum(row["status"] == "pass" for row in checks), "total": len(checks)},
        "stdout": completed.stdout[-16000:], "stderr": completed.stderr[-8000:],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not passed:
        return {"status": "blocked", "failure": {
            "code": "GRAPH_GATE_FAILED", "category": "business_validation", "scope": "task",
            "message": f"deterministic graph gate passed {report['summary']['passed']}/{report['summary']['total']}",
        }, "report": str(report_path), "candidate_sha256": report["candidate_sha256"]}
    return {"status": "ok", "report": str(report_path),
            "candidate_sha256": report["candidate_sha256"], "checks": 11}


def _freeze_hint_searches(queue_path: Path, hints_path: Path, output: Path) -> None:
    """Bind the exact query queue to approved source identities in hints.

    Hints contain no excerpts or verdicts.  They only replace an unavailable
    discovery provider; the normal safe fetch and locator extraction remain
    mandatory and produce the evidence payload/hash.
    """
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    hints = json.loads(hints_path.read_text(encoding="utf-8"))
    sources = {str(item.get("name")): item for item in hints.get("sources", [])}
    routes = hints.get("requirement_routes") or {}
    rows = []
    for item in queue.get("queries", []):
        requirement = str(((item.get("claim") or {}).get("requirement_id") or ""))
        route = routes.get(requirement) or {}
        source = sources.get(str(route.get("source")))
        result = None
        if source and source.get("canonical_url"):
            result = {"url": str(source["canonical_url"]), "title": str(source["name"])}
        rows.append({
            "search_hash": item["search_hash"], "query": item["query"],
            "status": "found" if result else "not_found",
            "results": [result] if result else [],
        })
    if not rows:
        raise CapabilityAdapterError("source queue has no queries to freeze")
    document = {
        "protocol": {"version": "wiki-frozen-search-v1", "kind": "query-search-results"},
        "backend": "approved-source-hints",
        "usage": {"search_requests": len(rows), "cost_usd": 0.0},
        "queries": rows,
    }
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def wiki_batch(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("operation") == "probe":
        return {"status": "ok", "adapter": "wiki.batch", "operations": sorted(WIKI_OPERATIONS)}
    operation = str(value.get("operation", ""))
    if operation not in WIKI_OPERATIONS:
        raise CapabilityAdapterError(f"unsupported wiki.batch operation: {operation}")
    workspace = _path(value.get("workspace"), "workspace")
    project_scripts = Path(__file__).resolve().parents[2] / "scripts"
    if operation == "research-plan":
        workflow = _path(value.get("workflow"), "workflow")
        output = _path(value.get("output"), "output")
        command = [sys.executable, str(project_scripts / "build_wiki_research_plan.py"),
                   str(workflow), str(output)]
        if value.get("source_hints"):
            command.extend(["--source-hints", str(_path(value["source_hints"], "source_hints"))])
        if value.get("registry"):
            command.extend(["--registry", str(_path(value["registry"], "registry"))])
        return _run(command, cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    if operation == "research-plan-gate":
        return _run([
            sys.executable, str(project_scripts / "gate_wiki_research_plan.py"),
            str(_path(value.get("plan"), "plan")), str(_path(value.get("output"), "output")),
        ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)),
            blocked_code="RESEARCH_PLAN_INVALID")
    if operation in {"search-execution-gate", "source-diversity-gate"}:
        output = _path(value.get("output"), "output")
        command = [sys.executable, str(project_scripts / "wiki_search_gates.py")]
        if operation == "search-execution-gate":
            command += ["search", str(_path(value.get("evidence"), "evidence")), str(output)]
            if value.get("allow_partial"): command.append("--allow-partial")
        else:
            command += ["diversity", str(_path(value.get("verified"), "verified")),
                        str(_path(value.get("plan"), "plan")), str(output)]
            if value.get("reviewed"): command.append("--reviewed")
            command += ["--attempt", str(int(value.get("attempt", 0))),
                        "--repair-budget", str(int(value.get("repair_budget", 2)))]
        return _run(
            command, cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)),
            blocked_code=("SOURCE_DIVERSITY_BLOCKED"
                          if operation == "source-diversity-gate" else "SEARCH_EXECUTION_BLOCKED"),
        )
    if operation == "terminology-verify":
        return _run([sys.executable, str(project_scripts / "verify_terminology.py"),
                     str(_path(value.get("plan"), "plan")),
                     str(_path(value.get("verified"), "verified")),
                     str(_path(value.get("output"), "output"))], cwd=workspace,
                    timeout=int(value.get("timeout_seconds", 1800)))
    if operation == "table-search-execution-gate":
        matrix, output = _path(value.get("matrix"), "matrix"), _path(value.get("output"), "output")
        script = project_scripts / "gate_table_search_execution.py"
        command = [sys.executable, str(script), str(matrix), str(output)]
        if value.get("allow_partial"): command.append("--allow-partial")
        return _run(command, cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    if operation == "content-blueprint":
        argv = value.get("argv")
        if not isinstance(argv, list) or len(argv) != 3 or not all(isinstance(item, str) for item in argv):
            raise CapabilityAdapterError("content-blueprint argv must be [graph,node_id,output]")
        if any(item.startswith("/") and not Path(item).resolve().is_relative_to(workspace) for item in argv):
            raise CapabilityAdapterError("content-blueprint argv escapes workspace")
        builder = Path(__file__).resolve().parents[2] / "scripts" / "build_wiki_content_blueprint.py"
        return _run([sys.executable, str(builder), *argv], cwd=workspace,
                    timeout=int(value.get("timeout_seconds", 1800)))
    if operation == "content-closure-gate":
        batch = _path(value.get("batch"), "batch")
        return _run([
            sys.executable, str(project_scripts / "gate_wiki_content_closure.py"),
            str(batch / "content-blueprint.json"),
            str(batch / "content-runtime/content-result.json"),
            str(batch / "verify-output.json"), str(batch / "source-diversity-gate.json"),
            str(batch / "content-closure-gate.json"),
        ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)),
            blocked_code="CONTENT_LOCAL_ISSUES")
    if operation == "draft-content-pipeline":
        batch = _path(value.get("batch"), "batch")
        scripts = workspace / "scripts"
        enriched = batch / "content-enriched.json"
        commands = [
            [sys.executable, str(scripts / "wiki_content_enrich.py"),
             str(batch / "verify-output.json"), str(batch / "content-runtime/content-result.json"),
             str(batch / "content-blueprint.json"), str(batch / "editorial-loop/editorial-review.json"),
             str(batch / "editorial-loop/editorial-policy-decision.json"),
             str(value.get("publication_mode") or "preview"),
             str(enriched)],
            [sys.executable, str(scripts / "wiki_batch.py"), "finalize",
             str(batch / "prepared.json"), "--content-output", str(enriched),
             "--allow-partial", "--resume"],
            [sys.executable,
             str(Path(__file__).resolve().parents[2] / "scripts/run_wiki_draft_content_gate.py"),
             str(batch / "batch-merge-plan.json"), "--gate-script",
             str(scripts / "wiki_draft_content_gate.py"), "--publication-mode",
             str(value.get("publication_mode") or "preview"), "--output",
             str(batch / "draft-content-gate.json"), "--source-gate",
             str(batch / "source-diversity-gate.json"), "--closure-gate",
             str(batch / "content-closure-gate.json")],
        ]
        return _pipeline(commands, cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    if operation == "maturity-gate":
        batch = _path(value.get("batch"), "batch")
        return _run([
            sys.executable, str(project_scripts / "gate_wiki_maturity.py"),
            str(batch), str(batch / "maturity-gate.json"),
        ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    if operation == "release-gate":
        batch = _path(value.get("batch"), "batch")
        prepared = batch / "prepared.json"
        coverage = batch / "coverage.json"
        scripts = workspace / "scripts"
        commands = [
            [
                sys.executable, str(scripts / "wiki_claim_coverage.py"), "plan",
                str(prepared), "--repo-root", str(workspace), "--output", str(coverage),
            ],
            [
                sys.executable, str(scripts / "wiki_batch.py"), "go-no-go",
                str(prepared), "--coverage", str(coverage), "--output",
                str(batch / "go-no-go.json"), "--resume",
            ],
            [
                sys.executable, str(scripts / "wiki_batch.py"), "gate",
                str(prepared), "--coverage", str(coverage), "--output",
                str(batch / "gate-report.json"), "--resume",
            ],
        ]
        return _pipeline(
            commands, cwd=workspace, timeout=int(value.get("timeout_seconds", 1800))
        )
    if operation == "table_population_gate":
        batch = _path(value.get("batch"), "batch")
        page = _path(value.get("page"), "page")
        registry = _path(value.get("registry"), "registry")
        table_dir = batch / "table-data"
        stage = table_dir / "table-stage"
        gate = table_dir / "table-population-gate.json"
        script = workspace / "scripts/wiki_table_population.py"
        commands = [
            [sys.executable, str(script), "stage", "--collection", str(table_dir / "collection.json"),
             "--page", str(page), "--registry", str(registry), "--root", str(workspace),
             "--output", str(stage)],
            [sys.executable, str(script), "gate", "--collection", str(table_dir / "collection.json"),
             "--page", str(stage / "candidate.md"), "--registry", str(stage / "registry.json"),
             "--root", str(workspace), "--output", str(gate)],
        ]
        return _pipeline(commands, cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    if operation == "node-preview":
        argv = value.get("argv")
        if (not isinstance(argv, list) or len(argv) != 4
                or not all(isinstance(item, str) and item for item in argv)):
            raise CapabilityAdapterError(
                "node-preview argv must be [industry,chinese_name,node_id,report]"
            )
        industry, chinese_name, node_id, report_value = argv
        if not node_id.startswith(("P", "A")) or not node_id[1:].isdigit():
            raise CapabilityAdapterError("node-preview node_id is invalid")
        report = Path(report_value).resolve()
        if not report.is_relative_to(workspace):
            raise CapabilityAdapterError("node-preview report escapes workspace")
        scripts = workspace / "scripts"
        generic = _run([
            sys.executable, str(scripts / "wiki_batch.py"), "preview", industry,
            "--chinese-name", chinese_name, "--output", str(report),
        ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
        if generic["status"] != "ok":
            return generic
        targeted = _run([
            sys.executable, str(scripts / "build_wiki_viewer.py"), industry,
            chinese_name, "--preview", "--start-node", node_id,
        ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
        if targeted["status"] != "ok":
            return targeted
        viewer = workspace / f"docs/{industry}-wiki-{node_id}-preview.html"
        document = json.loads(report.read_text(encoding="utf-8"))
        maturity_path = Path(report_value).parent / "maturity-gate.json"
        if maturity_path.is_file():
            maturity = json.loads(maturity_path.read_text(encoding="utf-8"))
            document["maturity"] = maturity.get("maturity")
            document["candidate_eligible"] = maturity.get("candidate_eligible") is True
            document["quality_debt"] = maturity.get("quality_debt") or {}
            document["maturity_gate_sha256"] = _sha256(maturity_path)
            document["all_passed"] = bool(document.get("all_passed")) and bool(
                maturity.get("candidate_eligible")
            )
        document.setdefault("artifacts", {})["viewer"] = {
            "path": str(viewer),
            "sha256": __import__("hashlib").sha256(viewer.read_bytes()).hexdigest(),
        }
        document["start_node"] = node_id
        report.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"status": "ok", "stdout": targeted.get("stdout", ""),
                "node_id": node_id, "viewer": str(viewer)}
    script = workspace / "scripts" / "wiki_batch.py"
    if not script.is_file():
        raise CapabilityAdapterError("workspace has no frozen wiki_batch.py")
    argv = value.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise CapabilityAdapterError("argv must be a string list")
    if any(item.startswith("/") and not Path(item).resolve().is_relative_to(workspace) for item in argv):
        raise CapabilityAdapterError("wiki.batch argv escapes workspace")
    return _run([sys.executable, str(script), operation, *argv], cwd=workspace,
                timeout=int(value.get("timeout_seconds", 1800)))


AGENT_LAUNCHERS = {
    "nomination": "run_wiki_nomination_capture.py",
    "verify": "run_wiki_verify_capture.py",
    "content": "run_wiki_content_capture.py",
    "editorial_review": "run_wiki_editorial_review_capture.py",
}


def agent(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("operation") == "probe":
        return {"status": "ok", "adapter": "agent-runtime", "launchers": sorted(AGENT_LAUNCHERS)}
    phase = str(value.get("phase", ""))
    if phase.startswith("graph_"):
        graph_phase = phase.removeprefix("graph_")
        allowed = {"conventions", "seed", "build", "closure", "mapping", "review",
                   "consolidate", "scorecard"}
        if graph_phase not in allowed:
            raise CapabilityAdapterError(f"unsupported graph agent phase: {graph_phase}")
        workspace = _path(value.get("workspace"), "workspace")
        plan = _path(value.get("plan"), "plan")
        output_dir = _path(value.get("output_dir"), "output_dir")
        inputs = value.get("inputs") or []
        if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs):
            raise CapabilityAdapterError("graph agent inputs must be a path list")
        input_paths = [_path(item, "graph agent input") for item in inputs]
        if any(not path.is_relative_to(workspace) for path in [plan, output_dir, *input_paths]):
            raise CapabilityAdapterError("graph agent path escapes workspace")
        script = workspace / "scripts/run_graph_phase_capture.py"
        command = [sys.executable, str(script), graph_phase, str(plan), str(output_dir),
                   *(str(path) for path in input_paths), "--model", str(value.get("model")),
                   "--reasoning-effort", str(value.get("reasoning_effort", "medium"))]
        if value.get("scope"):
            command.extend(["--scope", str(value["scope"])])
        result = _run(command, cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
        if result["status"] == "ok":
            result["attestation_required"] = True
        return result
    if phase == "research_ready":
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        prepared = batch / "prepared.json"
        workflow = batch / "nomination.workflow.run.js"
        nomination = batch / "nomination-runtime"
        evidence = batch / "source-evidence.json"
        queue = batch / "source-queue.json"
        verify_workflow = batch / "verify-only.workflow.run.js"
        domains = value.get("allowed_domains")
        hints_value = value.get("source_hints")
        hints = _path(hints_value, "source_hints") if hints_value else None
        research_plan_value = value.get("research_plan")
        research_plan = _path(research_plan_value, "research_plan") if research_plan_value else None
        research_scout = batch / "research-scout.json"
        scout_current = False
        if research_scout.is_file():
            try:
                scout_current = (
                    json.loads(research_scout.read_text(encoding="utf-8"))
                    .get("query_policy_version") == "activity-process-focus-v2"
                )
            except (OSError, ValueError, json.JSONDecodeError):
                scout_current = False
        active_research_scout = (
            _diversity_repair_scout(batch, research_scout)
            if scout_current else research_scout
        )
        open_discovery = bool(value.get("open_discovery"))
        if (not prepared.is_file() or not workflow.is_file() or not isinstance(domains, list)
                or (not open_discovery and not domains)
                or not all(isinstance(item, str) and item for item in domains)):
            raise CapabilityAdapterError("research_ready requires prepared inputs and allowed_domains")
        allow_args = ["--open-discovery"] if open_discovery else [
            part for domain in domains for part in ("--allow-domain", domain)
        ]
        scripts = workspace / "scripts"
        nomination_command = [
            sys.executable, str(scripts / "run_wiki_nomination_capture.py"), str(workflow),
            str(workspace / "schemas/wiki-nomination-claims.schema.json"), str(nomination),
            "--cost-usd", "0",
        ]
        if hints:
            nomination_command.extend(["--source-hints", str(hints)])
        if research_plan:
            nomination_command.extend(["--research-plan", str(research_plan)])
            nomination_command.extend(["--research-scout", str(active_research_scout)])
        plan_command = [sys.executable, str(scripts / "wiki_source_discovery.py"), "plan",
                        str(nomination / "nomination-result.json"), *allow_args,
                        "--output", str(queue), "--max-candidates-per-claim", "5"]
        if research_plan:
            plan_command.extend(["--research-plan", str(research_plan)])
        nomination_ready = all((nomination / name).is_file() for name in (
            "nomination-result.json", "wiki-usage-v1.json", "nomination-invocation.json"
        ))
        if nomination_ready and research_plan:
            try:
                invocation = json.loads((nomination / "nomination-invocation.json").read_text(encoding="utf-8"))
                usage = json.loads((nomination / "nomination-usage.json").read_text(encoding="utf-8"))
                scout_record = invocation.get("research_scout") or {}
                nomination_ready = (active_research_scout.is_file()
                    and scout_record.get("sha256") == hashlib.sha256(active_research_scout.read_bytes()).hexdigest()
                    and invocation.get("nomination_policy_version") == "research-scout-source-specific-v9"
                    and usage.get("exit_code") == 0
                    and usage.get("validation_error") is None)
                nomination_doc = json.loads((nomination / "nomination-result.json").read_text(encoding="utf-8"))
                external_sources = {
                    str(claim.get("believed_source", "")).strip()
                    for claim in nomination_doc.get("claims", [])
                    if claim.get("claim_kind") == "external_fact"
                }
                nomination_ready = nomination_ready and len(external_sources) >= 3
            except (OSError, ValueError, json.JSONDecodeError):
                nomination_ready = False
        if active_research_scout.is_file():
            try:
                active_scout_doc = json.loads(active_research_scout.read_text(encoding="utf-8"))
                repair = active_scout_doc.get("diversity_repair") or {}
                if repair.get("protocol") == "wiki-source-diversity-repair-v1":
                    nomination_ready = False
            except (OSError, ValueError, json.JSONDecodeError):
                nomination_ready = False
        scout_command = [
            sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/scout_wiki_research_plan.py"),
            str(research_plan), str(Path(__file__).resolve().parents[2] / "config/search-providers.json"),
            str(research_scout),
        ] if research_plan else None
        initial_commands = ([scout_command] if scout_command and not scout_current else [])
        initial_commands += [plan_command] if nomination_ready else [nomination_command, plan_command]
        initial = _pipeline(initial_commands, cwd=workspace,
                            timeout=int(value.get("timeout_seconds", 1800)))
        if initial["status"] != "ok":
            return initial
        search_args: list[str] = []
        if hints and not open_discovery:
            frozen_search = batch / "frozen-search-results.json"
            _freeze_hint_searches(queue, hints, frozen_search)
            search_args = ["--search-results", str(frozen_search)]
        search_results = batch / "frozen-provider-search-results.json"
        provider_command = [
            sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/search_provider_runtime.py"),
            str(queue), str(Path(__file__).resolve().parents[2] / "config/search-providers.json"),
            str(search_results),
        ]
        commands = [
            provider_command,
            [sys.executable, str(scripts / "wiki_source_discovery.py"), "run", str(queue),
             *([] if open_discovery else allow_args), "--search-results", str(search_results),
             *(["--allow-synthetic-proxy-dns"] if open_discovery else []),
             "--cache-dir", str(batch / "search-cache"), "--max-excerpt-chars", "40000",
             "--output", str(evidence)],
            [sys.executable, str(scripts / "wiki_source_discovery.py"), "materialize", str(evidence),
             "--output", str(verify_workflow)],
            [sys.executable, str(scripts / "wiki_batch.py"), "research-ready", str(prepared),
             "--evidence", str(evidence), "--verify-workflow", str(verify_workflow),
             "--nomination-usage", str(nomination / "wiki-usage-v1.json"), "--resume",
             "--repair-rewind"],
        ]
        result = _pipeline(commands, cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
        result["steps"] = initial.get("steps", []) + result.get("steps", [])
        if result["status"] == "ok":
            result["attestation_required"] = True
        return result
    if phase == "verify_pipeline":
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        prepared = batch / "prepared.json"
        evidence = batch / "source-evidence.json"
        runtime = batch / "verify-runtime"
        composed = batch / "verify-output.json"
        scripts = workspace / "scripts"
        commands = [
            [sys.executable, str(scripts / "run_wiki_verify_capture.py"), str(evidence),
             str(workspace / "schemas/wiki-verify-verdicts.schema.json"), str(runtime),
             "--cost-usd", "0"],
            [sys.executable, str(scripts / "wiki_verify_compose.py"), str(evidence),
             str(runtime / "verify-verdicts.runtime.json"), "--output", str(composed)],
            [sys.executable, str(scripts / "wiki_batch.py"), "verify", str(prepared),
             "--result", str(composed), "--runtime-dir", str(runtime),
             "--usage", str(runtime / "wiki-usage-v1.json"), "--resume"],
        ]
        result = _pipeline(commands, cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
        if result["status"] == "ok":
            result["attestation_required"] = True
        return result
    if phase == "editorial_policy_reuse":
        return {"status": "ok", "attestation_required": True,
                "editorial_policy_reused": True}
    if phase == "content_normalize":
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        normalizer = Path(__file__).resolve().parents[2] / "scripts/normalize_wiki_content_claims.py"
        return _run([sys.executable, str(normalizer), str(batch / "verify-output.json"),
                     str(batch / "content-blueprint.json"),
                     str(batch / "content-runtime/content-result.json"),
                     str(workspace / "scripts/run_wiki_content_capture.py")], cwd=workspace,
                    timeout=int(value.get("timeout_seconds", 1800)),
                    blocked_code="CONTENT_LOCAL_ISSUES")
    if phase == "editorial_patch":
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        script = Path(__file__).resolve().parents[2] / "scripts/run_wiki_editorial_patch.py"
        result = _run([
            sys.executable, str(script),
            str(batch / "verify-output.json"),
            str(batch / "content-runtime/content-result.json"),
            str(batch / "content-blueprint.json"),
            str(batch / "editorial-loop/editorial-review.json"),
            str(workspace / "scripts/run_wiki_content_capture.py"),
            str(batch / "editorial-loop/patch-runtime"),
        ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)),
            blocked_code="CONTENT_LOCAL_ISSUES")
        usage_path = batch / "editorial-loop/patch-runtime/editorial-patch-usage.json"
        if result["status"] == "blocked" and usage_path.is_file():
            try:
                patch_error = str(json.loads(
                    usage_path.read_text(encoding="utf-8")
                ).get("error") or "").strip()
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                patch_error = ""
            if patch_error:
                result["failure"]["message"] = patch_error
        return result
    if phase == "editorial_preview_fallback":
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        script = Path(__file__).resolve().parents[2] / "scripts/deterministic_preview_editorial_review.py"
        return _run([sys.executable, str(script), str(batch / "verify-output.json"),
                     str(batch / "content-runtime/content-result.json"),
                     str(batch / "content-blueprint.json"),
                     str(workspace / "scripts/run_wiki_content_capture.py"),
                     str(batch / "editorial-loop/editorial-review.json")], cwd=workspace,
                    timeout=int(value.get("timeout_seconds", 1800)))
    if phase in {"table_collect", "table_verify"}:
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        script_name = ("build_wiki_table_collection.py" if phase == "table_collect"
                       else "verify_wiki_table_collection.py")
        script = Path(__file__).resolve().parents[2] / "scripts" / script_name
        argv = [str(batch / "content-blueprint.json"), str(batch / "verify-output.json"),
                str(batch / "table-data")]
        hints = value.get("source_hints")
        if phase == "table_collect" and hints:
            argv.extend(["--source-hints", str(_path(hints, "source_hints"))])
        if phase == "table_collect" and value.get("research_plan"):
            argv.extend(["--research-plan", str(_path(value["research_plan"], "research_plan"))])
        first = _run(
            [sys.executable, str(script), *argv], cwd=workspace,
            timeout=int(value.get("timeout_seconds", 1800)),
            blocked_code="TABLE_LOCAL_ISSUES" if phase == "table_verify" else None,
        )
        if first["status"] != "ok" or phase != "table_collect": return first
        plan_path = batch / "table-data/search-matrix.json"
        executed_path = batch / "table-data/search-matrix.executed.json"
        if _reusable_executed_table_matrix(plan_path, executed_path):
            executed = {"status": "ok", "reused": True,
                        "output": str(executed_path), "plan_sha256": _sha256(plan_path)}
        else:
            executed = _run([
                sys.executable,
                str(Path(__file__).resolve().parents[2] / "scripts/execute_table_search_matrix.py"),
                str(plan_path), "--output", str(executed_path),
            ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
        if executed["status"] != "ok":
            return {**executed, "steps": [first, executed]}
        selected = _run([
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts/select_wiki_table_evidence.py"),
            str(batch / "table-data/collection.json"),
            str(executed_path),
            str(batch / "table-data/evidence-selection.json"),
            "--workspace", str(workspace),
        ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
        return {**selected, "steps": [first, executed, selected]}
    launcher = AGENT_LAUNCHERS.get(phase)
    if launcher is None:
        raise CapabilityAdapterError(f"unsupported agent phase: {phase}")
    workspace = _path(value.get("workspace"), "workspace")
    script = workspace / "scripts" / launcher
    argv = value.get("argv")
    if not script.is_file() or not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise CapabilityAdapterError("invalid frozen agent launcher or argv")
    if any(item.startswith("/") and not Path(item).resolve().is_relative_to(workspace) for item in argv):
        raise CapabilityAdapterError("agent argv escapes workspace")
    result = _run(
        [sys.executable, str(script), *argv],
        cwd=workspace,
        timeout=int(value.get("timeout_seconds", 1800)),
        blocked_code=(
            "EDITORIAL_LOCAL_ISSUES" if phase == "editorial_review"
            else "CONTENT_LOCAL_ISSUES" if phase == "content"
            else None
        ),
    )
    if phase == "editorial_review" and len(argv) >= 5:
        from .domains.editorial_policy import apply_editorial_policy
        review_dir = Path(argv[4])
        try:
            policy_result = apply_editorial_policy(
                review_dir / "editorial-review.json", Path(argv[1]),
                str(value.get("publication_mode") or "preview"),
                usage_path=review_dir / "editorial-review-usage.json",
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if result["status"] == "ok":
                return {"status": "failed", "failure": {
                    "code": "CAPABILITY_PROCESS_FAILED", "category": "infrastructure",
                    "scope": "task", "message": f"editorial policy failed: {exc}",
                }}
            policy_result = {"decision": "block"}
        if policy_result["decision"] != "block":
            result.pop("failure", None)
            result["status"] = "ok"
            result["editorial_policy"] = policy_result["decision"]
    if phase == "content" and result["status"] == "blocked" and len(argv) >= 4:
        usage_path = Path(argv[3]) / "content-usage.json"
        if usage_path.is_file():
            try:
                validation_error = str(json.loads(
                    usage_path.read_text(encoding="utf-8")
                ).get("validation_error") or "").strip()
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                validation_error = ""
            if validation_error:
                result["failure"]["message"] = validation_error
    if phase == "editorial_review" and result["status"] == "blocked" and len(argv) >= 5:
        review_path = Path(argv[4]) / "editorial-review.json"
        if review_path.is_file():
            try:
                review = json.loads(review_path.read_text(encoding="utf-8"))
                signature = [{
                    "section": str(issue.get("section") or ""),
                    "paragraph_index": int(issue.get("paragraph_index") or 0),
                    "issue_type": str(issue.get("issue_type") or ""),
                } for issue in review.get("issues") or []]
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                signature = []
            if signature:
                result["failure"]["message"] = "editorial NO_GO: " + json.dumps(
                    signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
    if result["status"] == "ok":
        result["attestation_required"] = True
    return result


def release(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("operation") == "probe":
        return {"status": "ok", "adapter": "release.apply", "authority": "ReleaseManager"}
    if value.get("operation") == "draft_apply":
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        script = workspace / "scripts/wiki_batch.py"
        return _run([sys.executable, str(script), "apply", str(batch / "prepared.json"),
                     "--resume", "--rehydrate",
                     "--draft-gate", str(batch / "draft-content-gate.json"),
                     "--output", str(batch / "content-apply-report.json")],
                    cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    if value.get("operation") == "table_apply":
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        page = _path(value.get("page"), "page")
        registry = _path(value.get("registry"), "registry")
        table_dir = batch / "table-data"
        script = workspace / "scripts/wiki_table_population.py"
        return _run([sys.executable, str(script), "apply", "--stage",
                     str(table_dir / "table-stage"), "--gate",
                     str(table_dir / "table-population-gate.json"), "--page", str(page),
                     "--registry", str(registry), "--output",
                     str(table_dir / "table-apply-report.json")], cwd=workspace,
                    timeout=int(value.get("timeout_seconds", 1800)))
    if value.get("operation") == "reviewed_apply":
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        script = workspace / "scripts/wiki_batch.py"
        return _run([
            sys.executable, str(script), "apply", str(batch / "prepared.json"),
            "--coverage", str(batch / "coverage.json"), "--output",
            str(batch / "reviewed-apply-report.json"), "--resume",
        ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    if value.get("operation") == "wiki_publish_candidate":
        workspace = _path(value.get("workspace"), "workspace")
        batch = _path(value.get("batch"), "batch")
        script = workspace / "scripts/wiki_batch.py"
        return _run([
            sys.executable, str(script), "publish", str(batch / "prepared.json"),
            "--output", str(batch / "publish-report.json"), "--resume",
        ], cwd=workspace, timeout=int(value.get("timeout_seconds", 1800)))
    if value.get("operation") == "graph_publish":
        workspace = _path(value.get("workspace"), "workspace")
        candidate = _path(value.get("candidate"), "candidate")
        gate = _path(value.get("gate_report"), "gate_report")
        destination = _path(value.get("destination"), "destination")
        record_path = _path(value.get("record"), "record")
        report = json.loads(gate.read_text(encoding="utf-8"))
        digest = _sha256(candidate)
        if (report.get("protocol") != "graph-gate-report-v2" or report.get("decision") != "PASS"
                or report.get("policy_version") != "graph-quality-v1"
                or report.get("candidate_sha256") != digest
                or (report.get("summary") or {}) != {"passed": 11, "total": 11}):
            return {"status": "blocked", "failure": {
                "code": "GRAPH_RELEASE_ELIGIBILITY_REQUIRED", "category": "business_validation",
                "scope": "task", "message": "release requires an exact candidate-bound 11/11 gate report"}}
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(candidate.read_bytes())
        if _sha256(temporary) != digest:
            temporary.unlink(missing_ok=True)
            raise CapabilityAdapterError("staged graph hash drift")
        temporary.replace(destination)
        record = {"protocol": "release-record-v1", "publication_status": "published",
                  "candidate_sha256": digest, "gate_report_sha256": _sha256(gate),
                  "destination": str(destination), "destination_sha256": _sha256(destination)}
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"status": "ok", **record, "workspace": str(workspace)}
    # Unknown production release operations are deliberately not a shell
    # escape.  Only the controller-owned job release service can apply the
    # allow-listed Wiki candidate prepared above to an authoritative target.
    if not isinstance(value.get("eligibility_receipt"), dict):
        return {"status": "blocked", "failure": {"code": "RELEASE_ELIGIBILITY_REQUIRED"}}
    return {"status": "blocked", "failure": {"code": "RELEASE_SERVICE_REQUIRED",
            "message": "use the job-driven ReleaseManager service"}}


HANDLERS = {"wiki.batch": wiki_batch, "graph.batch": graph_batch, "graph.gate": graph_gate,
            "agent.propose": agent, "agent.review": agent, "release.apply": release}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capability", choices=sorted(HANDLERS))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise CapabilityAdapterError("input must be an object")
        output = HANDLERS[args.capability](value)
    except (OSError, ValueError, CapabilityAdapterError, subprocess.TimeoutExpired) as exc:
        output = {"status": "failed", "failure": {"code": type(exc).__name__, "message": str(exc)}}
    args.output.write_text(json.dumps(output, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
