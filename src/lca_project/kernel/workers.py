"""Persistent worker registry and lease heartbeat support."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import threading
from typing import Any

from .leases import Lease, LeaseLost, LeaseManager
from .state import StateStore, utcnow


class WorkerRegistry:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def register(self, worker_id: str) -> None:
        now = utcnow()
        with self.state.transaction() as conn:
            conn.execute("""INSERT INTO worker_instances(
              worker_id,hostname,pid,status,current_job_id,current_run_id,current_task_id,
              started_at,heartbeat_at,progress_seq,checkpoint_hash,last_error)
              VALUES(?,?,?,?,NULL,NULL,NULL,?,?,0,NULL,NULL)
              ON CONFLICT(worker_id) DO UPDATE SET
                hostname=excluded.hostname,pid=excluded.pid,status='idle',
                current_job_id=NULL,current_run_id=NULL,current_task_id=NULL,
                started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at,
                progress_seq=0,checkpoint_hash=NULL,last_error=NULL""",
                (worker_id, socket.gethostname(), os.getpid(), "idle", now, now),
            )

    def heartbeat(self, worker_id: str, *, status: str | None = None,
                  job_id: str | None = None, run_id: str | None = None,
                  task_id: str | None = None, checkpoint_hash: str | None = None,
                  progress: bool = False, last_error: str | None = None) -> None:
        assignments: list[str] = ["heartbeat_at=?"]
        params: list[Any] = [utcnow()]
        for column, value in (
            ("status", status), ("current_job_id", job_id), ("current_run_id", run_id),
            ("current_task_id", task_id), ("checkpoint_hash", checkpoint_hash),
            ("last_error", last_error),
        ):
            if value is not None:
                assignments.append(f"{column}=?"); params.append(value)
        if progress:
            assignments.append("progress_seq=progress_seq+1")
        params.append(worker_id)
        with self.state.transaction() as conn:
            changed = conn.execute(
                f"UPDATE worker_instances SET {','.join(assignments)} WHERE worker_id=?", params
            ).rowcount
            if changed != 1:
                raise KeyError(worker_id)

    def idle(self, worker_id: str, *, last_error: str | None = None) -> None:
        with self.state.transaction() as conn:
            changed = conn.execute("""UPDATE worker_instances SET status='idle',
              current_job_id=NULL,current_run_id=NULL,current_task_id=NULL,
              heartbeat_at=?,last_error=? WHERE worker_id=?""",
                (utcnow(), last_error, worker_id),
            ).rowcount
            if changed != 1:
                raise KeyError(worker_id)

    def stop(self, worker_id: str, *, error: str | None = None) -> None:
        with self.state.transaction() as conn:
            conn.execute("""UPDATE worker_instances SET status='stopped',
              current_job_id=NULL,current_run_id=NULL,current_task_id=NULL,
              heartbeat_at=?,last_error=? WHERE worker_id=?""",
                (utcnow(), error, worker_id),
            )


@dataclass(frozen=True)
class WatchdogSweep:
    inspected: int
    recovered: int
    attempt_ids: tuple[str, ...]


class WorkerWatchdog:
    """Recover attempts whose worker heartbeat or fenced lease has expired."""

    def __init__(self, state: StateStore, events: Any) -> None:
        self.state = state
        self.events = events

    def sweep(self, *, stale_after_seconds: float = 30.0) -> WatchdogSweep:
        if stale_after_seconds <= 0:
            raise ValueError("watchdog stale interval must be positive")
        now = utcnow()
        cutoff = (datetime.now(timezone.utc) - timedelta(
            seconds=stale_after_seconds
        )).isoformat(timespec="microseconds")
        rows = list(self.state._connection().execute("""
            SELECT a.attempt_id FROM orchestrator_attempts a
            WHERE a.status='running' AND a.worker_id IS NOT NULL
            ORDER BY a.started_at
        """))
        recovered: list[str] = []
        emitted: list[tuple[str, str, str, str | None, str | None]] = []
        for candidate in rows:
            attempt_id = str(candidate["attempt_id"])
            with self.state.transaction() as conn:
                attempt = conn.execute(
                    "SELECT * FROM orchestrator_attempts WHERE attempt_id=? AND status='running'",
                    (attempt_id,),
                ).fetchone()
                if attempt is None:
                    continue
                lease = conn.execute(
                    "SELECT * FROM leases WHERE resource=?", (attempt["lease_resource"],)
                ).fetchone()
                worker = conn.execute(
                    "SELECT * FROM worker_instances WHERE worker_id=?", (attempt["worker_id"],)
                ).fetchone()
                lease_valid = bool(
                    lease is not None
                    and lease["holder"] == attempt["worker_id"]
                    and lease["fencing_token"] == attempt["fencing_token"]
                    and lease["expires_at"] > now
                )
                worker_fresh = bool(worker is not None and worker["heartbeat_at"] > cutoff)
                if lease_valid and worker_fresh:
                    continue
                changed = conn.execute("""UPDATE orchestrator_attempts
                    SET status='abandoned',failure_code='WORKER_LOST',failure_payload=?,finished_at=?
                    WHERE attempt_id=? AND status='running'""", (
                        json.dumps({"protocol":"failure-envelope-v1","code":"WORKER_LOST",
                                    "category":"infrastructure","scope":f"task:{attempt['task_id']}",
                                    "message":"worker heartbeat or fenced lease was lost",
                                    "evidence_artifacts":[]}, sort_keys=True,
                                   separators=(",", ":")), now, attempt_id,
                    )).rowcount
                if changed != 1:
                    continue
                run = conn.execute(
                    "SELECT job_id FROM orchestrator_runs WHERE run_id=?", (attempt["run_id"],)
                ).fetchone()
                conn.execute("""UPDATE orchestrator_tasks
                    SET status='ready',failure_code='WORKER_LOST',updated_at=?
                    WHERE run_id=? AND task_id=? AND status='running'""",
                    (now, attempt["run_id"], attempt["task_id"]),
                )
                conn.execute("UPDATE orchestrator_runs SET status='ready',updated_at=? WHERE run_id=?",
                             (now, attempt["run_id"]))
                prior_job_state: str | None = None
                job_id = str(run["job_id"]) if run else None
                if job_id:
                    job = conn.execute("SELECT status,payload FROM jobs WHERE id=?", (job_id,)).fetchone()
                    if job is not None and job["status"] in {"ready", "leased", "running"}:
                        prior_job_state = str(job["status"])
                        payload = json.loads(job["payload"])
                        payload.update({
                            "state": "stalled",
                            "transition_reason": f"worker lost during {attempt['task_id']}",
                        })
                        conn.execute("UPDATE jobs SET status='stalled',payload=?,updated_at=? WHERE id=?",
                                     (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":")), now, job_id))
                if worker is not None:
                    conn.execute("""UPDATE worker_instances SET status='lost',last_error='WORKER_LOST',
                        heartbeat_at=? WHERE worker_id=? AND current_run_id=?""",
                        (now, attempt["worker_id"], attempt["run_id"]),
                    )
                # Never delete a successor lease. Only remove this attempt's expired token.
                conn.execute("""DELETE FROM leases WHERE resource=? AND holder=? AND fencing_token=?
                    AND expires_at<=?""", (attempt["lease_resource"], attempt["worker_id"],
                                            attempt["fencing_token"], now))
                recovered.append(attempt_id)
                emitted.append((str(attempt["run_id"]), str(attempt["task_id"]),
                                str(attempt["worker_id"]), job_id, prior_job_state))
        for attempt_id, item in zip(recovered, emitted, strict=True):
            run_id, task_id, worker_id, job_id, prior_job_state = item
            common = {"attempt_id": attempt_id, "task_id": task_id, "worker_id": worker_id,
                      "failure_code": "WORKER_LOST"}
            self.events.append("worker", worker_id, "worker.lost", common,
                               actor="watchdog", event_id=f"watchdog:{attempt_id}:worker-lost")
            self.events.append("workflow_run", run_id, "task.requeued", common,
                               actor="watchdog", event_id=f"watchdog:{attempt_id}:task-requeued")
            if job_id and prior_job_state:
                self.events.append("job", job_id, "job.transitioned", {
                    "from": prior_job_state, "to": "stalled",
                    "reason": f"worker lost during {task_id}",
                }, actor="watchdog", event_id=f"watchdog:{attempt_id}:job-stalled")
        return WatchdogSweep(len(rows), len(recovered), tuple(recovered))


@dataclass
class LeaseHeartbeat:
    """Renew a task lease independently of the child capability process."""

    manager: LeaseManager
    registry: WorkerRegistry
    lease: Lease
    worker_id: str
    lease_seconds: int
    interval_seconds: float

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._lost: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name=f"lease-heartbeat:{self.worker_id}", daemon=True
        )

    def start(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with self._lock:
                    self.lease = self.manager.renew(self.lease, self.lease_seconds)
                self.registry.heartbeat(self.worker_id, status="running", progress=True)
            except BaseException as exc:  # the owner observes and fails closed
                self._lost = exc
                return

    def current(self) -> Lease:
        if self._lost is not None:
            raise LeaseLost(str(self._lost)) from self._lost
        with self._lock:
            lease = self.lease
        self.manager.assert_valid(lease)
        return lease

    def close(self) -> Lease:
        lease = self.stop()
        self.manager.assert_valid(lease)
        if self._lost is not None:
            raise LeaseLost(str(self._lost)) from self._lost
        return lease

    def stop(self) -> Lease:
        """Stop renewal and return the last lease without asserting ownership."""
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        with self._lock:
            return self.lease
