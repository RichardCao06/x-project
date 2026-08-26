from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from lca_project import capability_runtime
from lca_project.dashboard.service import DashboardService
from lca_project.kernel.failures import FailureEnvelope
from lca_project.kernel.worker import WorkerLoop
from lca_project.kernel.goal_alignment.quality_trajectory import _question_closure_score


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_plan(tmp_path: Path, output_name: str = "research-plan.json") -> dict:
    requirements = [
        "activity.identity.definition",
        "activity.boundary.included_operations",
        "activity.boundary.modeling_cutoff",
        "activity.route.modeling_resolution",
        "activity.reference.unit_handoff",
        "activity.boundary.product_handoff",
        "activity.collection.fields",
    ]
    workflow = tmp_path / "nomination.workflow.run.js"
    workflow.write_text(
        'const NODES = [{"node_id":"A040","name":"系统集成, 整机总装 | 笔记本电脑"}] '
        '/* DATA-BINDING:END */\n' + "\n".join(
            json.dumps({
                "requirement_id": item,
                "claim_kind": (
                    "modeling_judgment" if item in {
                        "activity.boundary.modeling_cutoff",
                        "activity.route.modeling_resolution",
                        "activity.collection.fields",
                    } else "external_fact"
                ),
            }) for item in requirements
        ),
        encoding="utf-8",
    )
    output = tmp_path / output_name
    subprocess.run([
        sys.executable, str(ROOT / "scripts/build_wiki_research_plan.py"),
        str(workflow), str(output),
    ], check=True)
    return json.loads(output.read_text(encoding="utf-8"))


def verified_row(requirement_id: str, *, verdict: str = "CONFIRMED") -> dict:
    return {
        "claim": {
            "claim_id": requirement_id,
            "requirement_id": requirement_id,
            "claim_kind": "external_fact",
            "believed_source": "Manufacturer technical guide",
        },
        "fetchResult": {
            "url": "https://manufacturer.example/guide",
            "language": "en",
            "excerpt": "Technical manufacturing process and handoff definition.",
        },
        "verify": {"verdict": verdict, "support_type": "direct"},
    }


def test_question_contract_is_stable_and_binds_frozen_requirements(tmp_path: Path) -> None:
    first = build_plan(tmp_path, "first.json")
    second = build_plan(tmp_path, "second.json")

    assert first["question_contract_sha256"] == second["question_contract_sha256"]
    assert first["research_question_contracts"] == second["research_question_contracts"]
    questions = {
        question["question_id"]: question
        for contract in first["research_question_contracts"]
        for question in contract["subquestions"]
    }
    assert questions["identity.activity_definition"]["requirement_ids"] == [
        "activity.identity.definition"
    ]
    assert questions["quantity.reference_flow"]["requirement_ids"] == []
    composition = next(
        contract for contract in first["research_question_contracts"]
        if contract["dimension"] == "composition_and_quantity"
    )
    assert composition["required_question_ids"] == []
    assert load_script("gate_wiki_research_plan.py").evaluate(first)["decision"] == "PASS"


def test_research_plan_gate_rejects_semantic_contract_tampering(tmp_path: Path) -> None:
    plan = build_plan(tmp_path)
    plan["research_question_contracts"][0]["subquestions"][0]["question"]["zh"] = "被篡改"
    gate = load_script("gate_wiki_research_plan.py").evaluate(plan)

    assert gate["decision"] == "REPAIR"
    assert "research_question_contracts_valid" in gate["failed_requirement_ids"]
    assert "question_contract_hash_mismatch" in gate["question_contract_validation"]["errors"]


def test_v2_gate_blocks_on_question_closure_but_not_portfolio_counts(tmp_path: Path) -> None:
    plan = build_plan(tmp_path)
    required = [
        "activity.identity.definition",
        "activity.boundary.included_operations",
        "activity.reference.unit_handoff",
        "activity.boundary.product_handoff",
    ]
    gate_module = load_script("wiki_search_gates.py")
    complete = gate_module.diversity_gate(
        {"claims": [verified_row(item) for item in required]}, plan, reviewed=True
    )

    assert complete["decision"] == "PASS_WITH_DEBT"
    assert complete["pipeline_continue"] is True
    assert complete["question_evidence_ledger"]["critical_questions_closed"] is True
    assert complete["quality_assessment"]["warnings"]

    partial = gate_module.diversity_gate(
        {"claims": [verified_row(item) for item in required[:-1]]},
        plan, reviewed=True, attempt=0, repair_budget=2,
    )
    exhausted_preview = gate_module.diversity_gate(
        {"claims": [verified_row(item) for item in required[:-1]]},
        plan, reviewed=False, attempt=2, repair_budget=2,
    )
    exhausted_reviewed = gate_module.diversity_gate(
        {"claims": [verified_row(item) for item in required[:-1]]},
        plan, reviewed=True, attempt=2, repair_budget=2,
    )

    assert partial["decision"] == "RESEARCH_MORE"
    assert partial["failed_requirement_ids"] == ["handoff.entry_exit_state"]
    assert exhausted_preview["decision"] == "EVIDENCE_LIMITED"
    assert exhausted_preview["pipeline_continue"] is True
    assert exhausted_reviewed["pipeline_continue"] is True
    assert exhausted_reviewed["candidate_eligible"] is False
    assert exhausted_reviewed["materialization_branch"]["kind"] == (
        "explicit_gap_evidence_limited"
    )
    assert exhausted_reviewed["materialization_branch"]["release_prohibited"] is True
    assert len(exhausted_reviewed["materialization_branch"]["gap_provenance_sha256"]) == 64
def test_research_more_scout_binds_failed_questions_and_prior_strategy(
    tmp_path: Path,
) -> None:
    plan = build_plan(tmp_path)
    config = tmp_path / "search-providers.json"
    config.write_text(json.dumps({"providers": {}, "routing": {}}), encoding="utf-8")
    prior_strategy_hash = (
        "c85b68d093754100148936e05e48121ac964a1383f4166f921351ef22277baf8"
    )
    failed_question_ids = ["identity.activity_definition"]
    gate = tmp_path / "source-diversity-gate.json"
    gate.write_text(json.dumps({
        "decision": "RESEARCH_MORE",
        "attempt": 0,
        "failed_requirement_ids": failed_question_ids,
        "strategy_hash": prior_strategy_hash,
    }), encoding="utf-8")
    previous = tmp_path / "research-scout.json"
    previous.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1",
        "query_policy_version": "question-contract-adaptive-v3",
        "node_id": plan["node_id"],
        "candidates": [{
            "url": "https://unaffected.example/guide",
            "question_id": "process.origin_boundary",
        }],
    }), encoding="utf-8")
    output = tmp_path / "research-scout-diversity-repair.json"

    completed = subprocess.run([
        sys.executable, str(ROOT / "scripts/scout_wiki_research_plan.py"),
        str(tmp_path / "research-plan.json"), str(config), str(output),
        "--repair-gate", str(gate), "--previous-scout", str(previous),
    ], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    repair_scout = json.loads(output.read_text(encoding="utf-8"))
    repair = repair_scout["diversity_repair"]
    assert repair["failed_question_ids"] == failed_question_ids
    assert repair["previous_strategy_hash"] == prior_strategy_hash
    assert len(repair["strategy_hash"]) == 64
    assert repair["strategy_hash"] != prior_strategy_hash
    assert repair["trigger_gate_sha256"] == hashlib.sha256(gate.read_bytes()).hexdigest()
    assert repair_scout["query_audit"]
    assert {
        row["question_id"] for row in repair_scout["query_audit"]
    } == set(failed_question_ids)


def test_failed_question_repair_excludes_insufficient_urls_and_changes_bilingual_strategy(
    tmp_path: Path,
) -> None:
    plan = build_plan(tmp_path)
    config = tmp_path / "search-providers.json"
    config.write_text(json.dumps({"providers": {}, "routing": {}}), encoding="utf-8")
    failed_question_ids = [
        "identity.activity_definition", "process.origin_boundary",
    ]
    prior_strategy_hash = "a" * 64
    gate = tmp_path / "source-diversity-gate.json"
    gate.write_text(json.dumps({
        "decision": "RESEARCH_MORE", "attempt": 0,
        "failed_requirement_ids": failed_question_ids,
        "strategy_hash": prior_strategy_hash,
    }), encoding="utf-8")
    old_urls = {
        "identity.activity_definition": "https://old.example/generic-server-description",
        "process.origin_boundary": "https://old.example/incomplete-production-steps",
    }
    previous = tmp_path / "research-scout.json"
    previous.write_text(json.dumps({
        "protocol": "wiki-research-scout-v1",
        "query_policy_version": "question-contract-adaptive-v3",
        "node_id": plan["node_id"],
        "candidates": [
            {"url": url, "question_id": question_id}
            for question_id, url in old_urls.items()
        ],
    }), encoding="utf-8")
    output = tmp_path / "research-scout-diversity-repair.json"

    completed = subprocess.run([
        sys.executable, str(ROOT / "scripts/scout_wiki_research_plan.py"),
        str(tmp_path / "research-plan.json"), str(config), str(output),
        "--repair-gate", str(gate), "--previous-scout", str(previous),
    ], capture_output=True, text=True, check=False)

    # With providers disabled there are deliberately no novel candidates, but
    # the governed repair artifact is still written for causal-delta proof.
    assert completed.returncode == 2
    repaired = json.loads(output.read_text(encoding="utf-8"))
    repair = repaired["diversity_repair"]
    assert repaired["query_policy_version"] == "question-contract-adaptive-v4"
    assert set(repair["excluded_urls"]) == set(old_urls.values())
    assert set(repair["excluded_url_hashes"]) == {
        hashlib.sha256(url.encode()).hexdigest() for url in old_urls.values()
    }
    assert repair["strategy_hash"] != prior_strategy_hash
    assert {row["question_id"] for row in repaired["query_audit"]} == set(
        failed_question_ids
    )
    assert {row["language"] for row in repaired["query_audit"]} == {"zh", "en"}
    query_by_question = {
        question_id: " ".join(
            row["query"] for row in repaired["query_audit"]
            if row["question_id"] == question_id
        )
        for question_id in failed_question_ids
    }
    assert "manufacturer technical documentation" in query_by_question[
        "identity.activity_definition"
    ]
    assert "assembly configuration firmware burn-in functional test" in query_by_question[
        "process.origin_boundary"
    ]


def test_repair_scout_cache_requires_new_policy_and_hash_bound_exclusions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research-scout-diversity-repair.json"
    gate_hash = "b" * 64
    excluded_url = "https://old.example/insufficient"
    value = {
        "query_policy_version": "question-contract-adaptive-v3",
        "diversity_repair": {
            "trigger_gate_sha256": gate_hash,
            "excluded_urls": [excluded_url],
            "excluded_url_hashes": [hashlib.sha256(excluded_url.encode()).hexdigest()],
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")

    assert capability_runtime._repair_scout_is_current(path, gate_hash) is False

    value["query_policy_version"] = "question-contract-adaptive-v4"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert capability_runtime._repair_scout_is_current(path, gate_hash) is True

    value["diversity_repair"]["excluded_url_hashes"] = ["0" * 64]
    path.write_text(json.dumps(value), encoding="utf-8")
    assert capability_runtime._repair_scout_is_current(path, gate_hash) is False


def test_gate_evidence_survives_blocked_command_and_changes_failure_fingerprint(
    tmp_path: Path,
) -> None:
    output = tmp_path / "source-diversity-gate.json"
    output.write_text(json.dumps({
        "protocol": "wiki-source-diversity-gate-v2",
        "gate_id": "question_evidence_sufficiency_gate",
        "gate_version": "question-evidence-governance-v2",
        "decision": "RESEARCH_MORE",
        "failed_requirement_ids": ["quantity.reference_flow"],
        "strategy_hash": "a" * 64,
        "metrics": {"critical_questions_confirmed": 3},
    }), encoding="utf-8")
    result = capability_runtime._attach_gate_evidence({
        "status": "blocked",
        "failure": {
            "code": "SOURCE_DIVERSITY_BLOCKED",
            "category": "business_validation",
            "scope": "task",
            "message": "more evidence required",
        },
    }, output)
    envelope = FailureEnvelope.from_capability(result["failure"]).asdict()

    assert envelope["gate_decision"] == "RESEARCH_MORE"
    assert envelope["failed_requirement_ids"] == ("quantity.reference_flow",)
    first = WorkerLoop._failure_fingerprint("source_diversity_gate", envelope["code"], envelope)
    changed = {**envelope, "strategy_hash": "b" * 64}
    second = WorkerLoop._failure_fingerprint("source_diversity_gate", envelope["code"], changed)
    assert first != second


def test_dashboard_separates_blocking_question_closure_from_quality_targets(
    tmp_path: Path,
) -> None:
    plan = build_plan(tmp_path)
    gate_module = load_script("wiki_search_gates.py")
    gate_value = gate_module.diversity_gate({"claims": [
        verified_row("activity.identity.definition"),
    ]}, plan, reviewed=True)
    facts = DashboardService._audit_document_facts(gate_value)
    projected = DashboardService._gate_projection(
        "source_diversity_gate", "failed", facts
    )

    assert projected is not None
    assert projected["decision"] == "RESEARCH_MORE"
    assert projected["passed"] is False
    assert projected["blocking_failures"] == ["critical_questions_closed"]
    assert projected["advisory_failures"]
    assert projected["question_evidence_ledger"]["questions"]
    assert projected["failed_requirement_ids"]


def test_content_and_quality_trajectory_understand_v2_gate_decisions() -> None:
    closure = load_script("gate_wiki_content_closure.py")
    blueprint = {"sections": {name: {} for name in ("定义", "边界", "投入", "产出", "数据")}}
    content = {"sections": [{
        "heading": name,
        "paragraphs": [{"sentences": [{
            "text": "已核验事实。", "claim_kind": "external_fact",
            "evidence_claim_ids": [f"claim-{index}"],
        }]}],
    } for index, name in enumerate(blueprint["sections"], 1)]}
    verified = {"claims": [{
        "claim": {"claim_id": f"claim-{index}", "requirement_id": f"req-{index}"},
        "verify": {"verdict": "CONFIRMED"},
    } for index in range(1, 6)]}
    source_gate = {
        "decision": "PASS_WITH_DEBT", "candidate_eligible": True,
        "question_contract_sha256": "a" * 64,
        "question_evidence_ledger": {"metrics": {
            "critical_questions_total": 4, "critical_questions_confirmed": 4,
        }},
    }

    result = closure.evaluate(blueprint, content, verified, source_gate)
    assert result["decision"] == "PASS_WITH_DEBT"
    assert result["pipeline_continue"] is True
    assert result["candidate_eligible"] is True
    assert _question_closure_score({"question_evidence_ledger": {"metrics": {
        "critical_questions_total": 4, "critical_questions_confirmed": 3,
    }}}) == 0.75
