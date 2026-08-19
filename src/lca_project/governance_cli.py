"""Command line surface for v2 Goal governance.

The existing ``lca-platform`` CLI remains the production execution surface.
This focused companion command governs versioned contracts and their audit
records without allowing a running Worker to mutate active Goal semantics.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from lca_project.contracts.governance import JobContractBinding
from lca_project.kernel.goal_alignment.governance import GovernanceController
from lca_project.kernel.state import StateStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON root must be an object")
    return value


def _dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="lca-governance")
    result.add_argument("--root", type=Path, default=PROJECT_ROOT)
    commands = result.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register", help="register an immutable draft contract")
    register.add_argument("contract", type=Path)
    register.add_argument("--activate", action="store_true")
    register.add_argument("--actor")
    register.add_argument(
        "--role",
        choices=("human_goal_owner", "human_governance_owner"),
    )

    commands.add_parser("status", help="inspect the contract governance control plane")

    replace = commands.add_parser(
        "replace-contract",
        help="replace an active Autonomy, Assurance, or Capability version",
    )
    replace.add_argument("from_ref")
    replace.add_argument("target", type=Path)
    replace.add_argument("--actor", required=True)
    replace.add_argument(
        "--role",
        required=True,
        choices=("human_goal_owner", "human_governance_owner"),
    )
    replace.add_argument("--rationale", required=True)
    replace.add_argument("--evidence", action="append", required=True)

    suspend = commands.add_parser(
        "suspend-contract",
        help="revoke autonomous use of one immutable contract version",
    )
    suspend.add_argument("contract_ref")
    suspend.add_argument("--actor", required=True)
    suspend.add_argument(
        "--role",
        required=True,
        choices=("human_goal_owner", "human_governance_owner"),
    )
    suspend.add_argument("--reason", required=True)
    suspend.add_argument("--evidence", action="append", required=True)

    propose = commands.add_parser("propose-goal-change", help="create a versioned Goal amendment")
    propose.add_argument("from_ref")
    propose.add_argument("target", type=Path)
    propose.add_argument("--acceptance-delta", required=True, type=Path)
    propose.add_argument("--rationale", required=True)
    propose.add_argument("--evidence", action="append", required=True)
    propose.add_argument("--proposed-by", required=True)
    propose.add_argument("--migration-plan", type=Path)

    approve = commands.add_parser("approve-goal-change")
    approve.add_argument("proposal_id")
    approve.add_argument("--actor", required=True)
    approve.add_argument(
        "--role",
        required=True,
        choices=("human_goal_owner", "governance_policy"),
    )
    approve.add_argument("--decision", choices=("approve", "reject"), default="approve")
    approve.add_argument("--rationale", required=True)

    activate = commands.add_parser("activate-goal-change")
    activate.add_argument("proposal_id")
    activate.add_argument("--actor", required=True)

    bind = commands.add_parser("bind-job", help="freeze all governance versions for one Job")
    bind.add_argument("job_id")
    bind.add_argument("--goal", required=True)
    bind.add_argument("--autonomy", required=True)
    bind.add_argument("--assurance", required=True)
    bind.add_argument("--capability", required=True)

    eligible = commands.add_parser("check-autonomy")
    eligible.add_argument("job_id")
    eligible.add_argument("action")
    eligible.add_argument("--risk", required=True, choices=("low", "medium", "high", "critical"))
    eligible.add_argument("--runtime", required=True, type=Path)
    eligible.add_argument("--input-scope", required=True, type=Path)
    eligible.add_argument("--authority", action="append", default=[])
    eligible.add_argument("--satisfied-requirement", action="append", default=[])
    eligible.add_argument(
        "--requirement-evidence",
        type=Path,
        help="JSON object containing hashed evidence for security-sensitive requirements",
    )

    assess = commands.add_parser("assess-alignment")
    assess.add_argument("job_id")
    assess.add_argument("--clause-results", required=True, type=Path)
    assess.add_argument("--prohibited-outcomes", required=True, type=Path)
    assess.add_argument("--terminal-state", required=True)
    assess.add_argument("--claimed-complete", action="store_true")
    capability = assess.add_mutually_exclusive_group(required=True)
    capability.add_argument("--capability-match", action="store_true")
    capability.add_argument("--capability-mismatch", action="store_true")
    assess.add_argument("--evidence", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.root.resolve()
    state = StateStore(root / "var" / "state.db")
    controller = GovernanceController(state)
    try:
        if args.command == "register":
            registered = controller.register_contract(_load(args.contract))
            if args.activate:
                if not args.actor or not args.role:
                    raise ValueError("--activate requires --actor and --role")
                registered = controller.activate_initial_contract(
                    registered["contract_ref"], actor=args.actor, actor_role=args.role
                )
            _dump(registered)
            return 0
        if args.command == "status":
            _dump(controller.status())
            return 0
        if args.command == "replace-contract":
            _dump(controller.replace_active_contract(
                from_ref=args.from_ref,
                target_payload=_load(args.target),
                actor=args.actor,
                actor_role=args.role,
                rationale=args.rationale,
                evidence=args.evidence,
            ))
            return 0
        if args.command == "suspend-contract":
            _dump(controller.suspend_contract(
                args.contract_ref,
                actor=args.actor,
                actor_role=args.role,
                reason=args.reason,
                evidence=args.evidence,
            ))
            return 0
        if args.command == "propose-goal-change":
            result = controller.propose_goal_change(
                from_ref=args.from_ref,
                target_payload=_load(args.target),
                acceptance_delta=_load(args.acceptance_delta),
                rationale=args.rationale,
                evidence=args.evidence,
                proposed_by=args.proposed_by,
                migration_plan=_load(args.migration_plan) if args.migration_plan else None,
            )
            _dump(result)
            return 0
        if args.command == "approve-goal-change":
            _dump(controller.approve_goal_change(
                args.proposal_id,
                actor=args.actor,
                actor_role=args.role,
                decision=args.decision,
                rationale=args.rationale,
            ))
            return 0
        if args.command == "activate-goal-change":
            _dump(controller.activate_goal_change(args.proposal_id, actor=args.actor))
            return 0
        if args.command == "bind-job":
            _dump(controller.bind_job(JobContractBinding(
                job_id=args.job_id,
                goal_ref=args.goal,
                autonomy_ref=args.autonomy,
                assurance_ref=args.assurance,
                capability_ref=args.capability,
            )))
            return 0
        if args.command == "check-autonomy":
            _dump(controller.check_autonomy(
                job_id=args.job_id,
                action=args.action,
                risk=args.risk,
                runtime_fingerprint=_load(args.runtime),
                input_scope=_load(args.input_scope),
                requested_authority=args.authority,
                satisfied_requirements=args.satisfied_requirement,
                requirement_evidence=(
                    _load(args.requirement_evidence)
                    if args.requirement_evidence
                    else None
                ),
            ).asdict())
            return 0
        if args.command == "assess-alignment":
            _dump(controller.assess_alignment(
                job_id=args.job_id,
                clause_results=_load(args.clause_results),
                prohibited_outcomes=_load(args.prohibited_outcomes),
                capability_match=args.capability_match and not args.capability_mismatch,
                terminal_state=args.terminal_state,
                claimed_complete=args.claimed_complete,
                evidence=_load(args.evidence) if args.evidence else None,
            ).asdict())
            return 0
    except (ValueError, KeyError, RuntimeError) as exc:
        _dump({"status": "error", "error": type(exc).__name__, "message": str(exc)})
        return 2
    finally:
        state.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
