"""Small deterministic scheduler with leases and idempotency keys."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import threading

from .workflow import TaskState, WorkflowRun


@dataclass(frozen=True)
class Lease:
    run_id: str
    task_id: str
    owner: str
    expires_at: datetime


class Scheduler:
    def __init__(self) -> None:
        self._leases: dict[tuple[str, str], Lease] = {}
        self._completed: set[tuple[str, str, str]] = set()
        self._lock = threading.RLock()

    def acquire(self, run: WorkflowRun, run_id: str, task_id: str, owner: str, *, ttl_seconds: int = 60) -> Lease | None:
        with self._lock:
            key = (run_id, task_id)
            now = datetime.now(UTC)
            existing = self._leases.get(key)
            if existing and existing.expires_at > now and existing.owner != owner:
                return None
            if run.states[task_id] == TaskState.READY:
                run.transition(task_id, TaskState.RUNNING)
            elif run.states[task_id] != TaskState.RUNNING:
                return None
            lease = Lease(run_id, task_id, owner, now + timedelta(seconds=ttl_seconds))
            self._leases[key] = lease
            return lease

    def complete(self, lease: Lease, idempotency_key: str) -> bool:
        with self._lock:
            current = self._leases.get((lease.run_id, lease.task_id))
            if current != lease:
                return False
            key = (lease.run_id, lease.task_id, idempotency_key)
            if key in self._completed:
                return False
            self._completed.add(key)
            self._leases.pop((lease.run_id, lease.task_id), None)
            return True

    def expire(self, run: WorkflowRun) -> tuple[str, ...]:
        now = datetime.now(UTC)
        expired: list[str] = []
        with self._lock:
            for key, lease in list(self._leases.items()):
                if lease.expires_at <= now:
                    self._leases.pop(key)
                    if run.states[lease.task_id] == TaskState.RUNNING:
                        run.transition(lease.task_id, TaskState.PENDING)
                    expired.append(lease.task_id)
        return tuple(expired)
