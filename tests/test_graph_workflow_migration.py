from __future__ import annotations

import json
from pathlib import Path
import shutil

from lca_project.capability_runtime import graph_batch, graph_gate, release
from lca_project.kernel.orchestrator import PersistentOrchestrator
from lca_project.kernel.skills import SkillInvoker
from lca_project.kernel.worker import WorkerLoop


ROOT = Path(__file__).resolve().parents[1]


def project_copy(tmp_path: Path) -> Path:
    root = tmp_path / "project"; root.mkdir()
    for name in ("skills", "workflows", "capabilities", "contracts", "policies", "agents", "vendor"):
        shutil.copytree(ROOT / name, root / name)
    return root


def test_graph_skill_materializes_full_sop_and_worker_executes_plan(tmp_path: Path) -> None:
    root = project_copy(tmp_path)
    accepted = SkillInvoker(root).invoke("industry-graph", {
        "industry": "steel", "display_name": "钢铁", "generated": "2026-08-13",
    })
    run_id = PersistentOrchestrator(root).materialize(accepted["job_id"])
    tasks = PersistentOrchestrator(root).tasks(run_id)
    assert [row.task_id for row in tasks] == ["plan", "conventions", "seed_lca", "seed_products",
        "seed_activities", "seed_engineering", "build", "closure", "mapping_products",
        "mapping_activities", "mapping_technology", "review_engineer", "review_curator",
        "review_classifier", "review_adversarial", "consolidate", "scorecard",
        "materialize", "gate", "release"]
    cycle = WorkerLoop(root, worker_id="graph-plan-worker").run_once(run_id=run_id)
    assert cycle.status == "succeeded" and cycle.task_id == "plan"
    workspace = root / "var/workspaces/jobs" / accepted["job_id"]
    plan = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
    assert plan["request"]["industry"] == "steel"
    assert plan["authority"] == {"agent_output": "proposal_only", "release": "deterministic_gate_only"}
    assert PersistentOrchestrator(root).tasks(run_id)[1].status == "ready"


def test_graph_gate_and_release_are_bound_to_exact_candidate_hash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"; scripts = workspace / "scripts"; scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "vendor/lca_cornerstone/scripts/validate_graph.py", scripts / "validate_graph.py")
    candidate = workspace / "candidate.json"
    shutil.copyfile(ROOT / "vendor/lca_cornerstone/fixtures/wiki-phase2/docs/ict_equipment-name-graph.json", candidate)
    report = workspace / "gate.json"
    result = graph_gate({"operation": "validate_11", "workspace": str(workspace),
                         "candidate": str(candidate), "report": str(report)})
    assert result["status"] == "ok" and result["checks"] == 11
    gate = json.loads(report.read_text(encoding="utf-8"))
    assert gate["decision"] == "PASS" and gate["summary"] == {"passed": 11, "total": 11}

    destination = tmp_path / "published/graph.json"
    published = release({"operation": "graph_publish", "workspace": str(workspace),
                         "candidate": str(candidate), "gate_report": str(report),
                         "destination": str(destination),
                         "record": str(workspace / "release.json")})
    assert published["status"] == "ok" and destination.read_bytes() == candidate.read_bytes()

    candidate.write_text(candidate.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    blocked = release({"operation": "graph_publish", "workspace": str(workspace),
                       "candidate": str(candidate), "gate_report": str(report),
                       "destination": str(tmp_path / "published/tampered.json"),
                       "record": str(workspace / "tampered-release.json")})
    assert blocked["status"] == "blocked"
    assert not (tmp_path / "published/tampered.json").exists()


def test_materialize_reconcile_adapter_preserves_original_sop_contract(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"; scripts = workspace / "scripts"; scripts.mkdir(parents=True)
    for name in ("materialize.py", "reconcile.py"):
        shutil.copyfile(ROOT / f"vendor/lca_cornerstone/scripts/{name}", scripts / name)
    original = json.loads((ROOT / "vendor/lca_cornerstone/fixtures/wiki-phase2/docs/ict_equipment-name-graph.json").read_text(encoding="utf-8"))
    plan = workspace / "plan.json"
    plan.write_text(json.dumps({"request": {"industry": "ict_equipment",
                                             "display_name": "ICT设备制造",
                                             "generated": "2026-08-13"}}), encoding="utf-8")
    inputs = {
        "conventions": original["conventions"],
        "final_graph": {"products": original["products"], "activities": original["activities"]},
        "mapping": {"classification": "fixture", "rows": [], "gaps": []},
        "scorecard": original["scorecard"],
    }
    paths = {}
    for name, document in inputs.items():
        paths[name] = workspace / f"{name}.json"
        paths[name].write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    candidate, journal = workspace / "candidate.json", workspace / "journal.jsonl"
    result = graph_batch({"operation": "materialize_reconcile", "workspace": str(workspace),
                          "plan": str(plan), **{key: str(path) for key, path in paths.items()},
                          "journal": str(journal), "candidate": str(candidate)})
    assert result["status"] == "ok" and len(result["candidate_sha256"]) == 64
    materialized = json.loads(candidate.read_text(encoding="utf-8"))
    assert materialized["_meta"]["generated"] == "2026-08-13"
    assert materialized["conventions"]["identity_scope"]
    assert materialized["edges"] and journal.is_file()


def test_graph_gate_blocks_a_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"; scripts = workspace / "scripts"; scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "vendor/lca_cornerstone/scripts/validate_graph.py", scripts / "validate_graph.py")
    source = ROOT / "vendor/lca_cornerstone/fixtures/wiki-phase2/docs/ict_equipment-name-graph.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    target = next(product["id"] for product in document["products"]
                  if product.get("boundary") == "foreground")
    document["edges"] = [edge for edge in document["edges"]
                         if not (edge.get("type") == "PRODUCES" and edge.get("to") == target)]
    candidate = workspace / "mutant.json"
    candidate.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    result = graph_gate({"operation": "validate_11", "workspace": str(workspace),
                         "candidate": str(candidate), "report": str(workspace / "gate.json")})
    assert result["status"] == "blocked"
    assert json.loads((workspace / "gate.json").read_text(encoding="utf-8"))["decision"] == "FAIL"
