from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading

import pytest

from lca_project.contracts import Job, JobState
from lca_project.control import ProtocolError
from lca_project.kernel.leases import LeaseLost


def valid_job(**changes: object) -> Job:
    return replace(Job(target="steel", workflow="graph-industry-production", scope={"industry": "steel"},
                       policy_version="graph-quality-v1", input_hashes=("a" * 64,)), **changes)


def test_ctl_001_rejects_missing_required_job_protocol_fields(plane) -> None:
    with pytest.raises(ProtocolError):
        plane.submit_job(valid_job(input_hashes=()))


def test_ctl_002_rejects_illegal_transition(plane) -> None:
    job_id, _ = plane.submit_job(valid_job())
    with pytest.raises(ProtocolError):
        plane.transition_job(job_id, JobState.PUBLISHED, reason="bypass")
    assert plane.state.get("jobs", job_id)["status"] == JobState.PLANNED


def test_ctl_003_only_one_worker_can_hold_lease(plane) -> None:
    accepted: list[str] = []
    rejected: list[str] = []

    def acquire(holder: str) -> None:
        try:
            plane.leases.acquire("job:one", holder, 10)
            accepted.append(holder)
        except LeaseLost:
            rejected.append(holder)

    threads = [threading.Thread(target=acquire, args=(holder,)) for holder in ("a", "b")]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(accepted) == len(rejected) == 1


def test_ctl_005_idempotency_key_deduplicates_job(plane) -> None:
    first, duplicate = plane.submit_job(valid_job(), idempotency_key="same-request")
    second, duplicate2 = plane.submit_job(valid_job(), idempotency_key="same-request")
    assert (first, duplicate, second, duplicate2) == (first, False, first, True)


def test_ctl_006_budget_exhaustion_blocks_reservation(plane) -> None:
    plane.budgets.configure("agent:wiki", 1)
    plane.budgets.reserve("agent:wiki", 1)
    with pytest.raises(Exception, match="budget exhausted"):
        plane.budgets.reserve("agent:wiki", 1)


def test_ctl_008_status_is_read_only(plane) -> None:
    before = plane.status()
    assert plane.status() == before


def test_art_001_cas_is_deduplicated(plane) -> None:
    one = plane.artifacts.put_bytes(b"same")
    two = plane.artifacts.put_bytes(b"same")
    assert one.digest == two.digest and Path(one.uri).read_bytes() == b"same"


def test_art_002_tampered_cas_fails_closed(plane) -> None:
    artifact = plane.artifacts.put_bytes(b"trusted")
    Path(artifact.uri).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="integrity"):
        plane.artifacts.get_bytes(artifact.digest)
    assert plane.state.get("artifacts", artifact.digest)["metadata"]["integrity_status"] == "corrupt"


def test_art_004_lineage_requires_real_registered_artifacts(plane) -> None:
    with pytest.raises(Exception):
        plane.artifacts.link("missing-parent", "missing-child")


def test_art_007_lineage_is_queryable(plane) -> None:
    parent = plane.artifacts.put_bytes(b"parent")
    child = plane.artifacts.put_bytes(b"child")
    plane.artifacts.link(parent.digest, child.digest, "derived_from")
    assert plane.artifacts.lineage(child.digest)[0]["parent_digest"] == parent.digest


def test_evt_001_rejects_missing_event_envelope_fields(plane) -> None:
    with pytest.raises(ValueError):
        plane.events.append("", "target", "thing")


def test_evt_002_event_id_replay_is_idempotent(plane) -> None:
    one = plane.events.append("job", "j1", "started", event_id="event-1")
    two = plane.events.append("job", "j1", "started", event_id="event-1")
    assert one.sequence == two.sequence
    assert len(list(plane.events.read("job", "j1"))) == 1
    with pytest.raises(ValueError, match="collision"):
        plane.events.append("job", "j1", "different", event_id="event-1")


def test_evt_007_event_stream_is_stably_replayable(plane) -> None:
    plane.events.append("job", "j1", "created", {"state": "planned"})
    plane.events.append("job", "j1", "transitioned", {"state": "ready"})
    replay = [(event.event_type, event.payload["state"]) for event in plane.events.read("job", "j1")]
    assert replay == [("created", "planned"), ("transitioned", "ready")]
