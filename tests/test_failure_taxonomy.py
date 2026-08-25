from __future__ import annotations

from lca_project.kernel.goal_alignment.failure_taxonomy import (
    TAXONOMY_VERSION, classify_failure, taxonomy_record,
)


def test_stable_families_collapse_high_cardinality_messages() -> None:
    first = classify_failure(
        task_id="table_collect", failure_code="CAPABILITY_PROCESS_FAILED",
        payload={"message": "unexpected table bootstrap precondition for A013"},
    )
    second = classify_failure(
        task_id="table_collect", failure_code="VALUE_ERROR",
        payload={"message": "a different node emitted a different exception"},
    )

    assert first == second == "table_contract"


def test_taxonomy_record_is_versioned_and_preserves_unknown() -> None:
    assert taxonomy_record(
        task_id="mystery", failure_code="ODD", payload={"message": "opaque"},
    ) == {"taxonomy_version": TAXONOMY_VERSION, "mechanism_family": "unknown"}
