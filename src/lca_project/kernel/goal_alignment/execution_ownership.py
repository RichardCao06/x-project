"""Fenced, renewable ownership for long-running goal-alignment actions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import socket
import threading
from typing import Any

from ...control import ControlPlane
from ..leases import Lease, LeaseLost
from ..state import utcnow


def _cutoff(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    )


@dataclass
class ExecutionOwnership:
    """Renew a control-plane lease while a blocking Agent call is in flight."""

    control: ControlPlane
    execution_type: str
    execution_id: str
    owner_id: str
    attempt: int
    lease_seconds: int = 60
    heartbeat_seconds: float = 10.0

    def __post_init__(self) -> None:
        self.resource = f"{self.execution_type}:{self.execution_id}"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._lost: BaseException | None = None
        self.lease: Lease | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"goal-heartbeat:{self.execution_type}:{self.execution_id[-10:]}",
            daemon=True,
        )

    @classmethod
    def create(
        cls, control: ControlPlane, execution_type: str, execution_id: str,
        *, attempt: int, lease_seconds: int = 60, heartbeat_seconds: float = 10.0,
    ) -> "ExecutionOwnership":
        owner = (
            f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}:"
            f"{execution_type}"
        )
        return cls(
            control, execution_type, execution_id, owner, attempt,
            lease_seconds, heartbeat_seconds,
        )

    def _persist(self, status: str) -> None:
        if self.lease is None:
            raise LeaseLost(f"execution has no lease: {self.resource}")
        now = utcnow()
        with self.control.state.transaction() as conn:
            existing = conn.execute(
                "SELECT started_at FROM goal_execution_owners "
                "WHERE execution_type=? AND execution_id=?",
                (self.execution_type, self.execution_id),
            ).fetchone()
            started_at = str(existing["started_at"]) if existing else now
            conn.execute(
                "INSERT INTO goal_execution_owners(" 
                "execution_type,execution_id,resource,owner_id,owner_pid,fencing_token,"
                "attempt,status,heartbeat_at,lease_expires_at,started_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(execution_type,execution_id) DO UPDATE SET "
                "resource=excluded.resource,owner_id=excluded.owner_id,"
                "owner_pid=excluded.owner_pid,fencing_token=excluded.fencing_token,"
                "attempt=excluded.attempt,status=excluded.status,"
                "heartbeat_at=excluded.heartbeat_at,"
                "lease_expires_at=excluded.lease_expires_at,updated_at=excluded.updated_at "
                "WHERE goal_execution_owners.fencing_token<=excluded.fencing_token",
                (
                    self.execution_type, self.execution_id, self.resource,
                    self.owner_id, os.getpid(), self.lease.fencing_token,
                    self.attempt, status, now, self.lease.expires_at, started_at, now,
                ),
            )

    def start(self) -> "ExecutionOwnership":
        self.lease = self.control.leases.acquire(
            self.resource, self.owner_id, seconds=self.lease_seconds
        )
        self._persist("running")
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                with self._lock:
                    if self.lease is None:
                        raise LeaseLost(f"execution has no lease: {self.resource}")
                    self.lease = self.control.leases.renew(
                        self.lease, seconds=self.lease_seconds
                    )
                self._persist("running")
            except BaseException as exc:  # owner fails closed at its next write
                self._lost = exc
                return

    def current(self) -> Lease:
        if self._lost is not None:
            raise LeaseLost(str(self._lost)) from self._lost
        with self._lock:
            if self.lease is None:
                raise LeaseLost(f"execution has no lease: {self.resource}")
            lease = self.lease
        self.control.leases.assert_valid(lease)
        return lease

    def close(self, status: str = "released") -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))
        lease = self.lease
        if lease is not None:
            try:
                self.current()
                self._persist(status)
            except LeaseLost:
                # A successor owns the durable row now.  A stale owner must
                # never rewrite its projection while unwinding.
                pass
            finally:
                self.control.leases.release(lease)


def execution_is_fresh(
    control: ControlPlane, execution_type: str, execution_id: str,
    *, heartbeat_grace_seconds: float = 90.0,
) -> bool:
    """Return true only when owner projection and fenced lease are both live."""
    row = control.state._connection().execute(
        "SELECT o.*,l.holder,l.fencing_token AS lease_token,l.expires_at "
        "FROM goal_execution_owners o LEFT JOIN leases l ON l.resource=o.resource "
        "WHERE o.execution_type=? AND o.execution_id=?",
        (execution_type, execution_id),
    ).fetchone()
    now = utcnow()
    return bool(
        row is not None
        and row["status"] == "running"
        and str(row["heartbeat_at"]) > _cutoff(heartbeat_grace_seconds)
        and row["holder"] == row["owner_id"]
        and row["lease_token"] == row["fencing_token"]
        and str(row["expires_at"] or "") > now
    )


def ownership_snapshot(
    control: ControlPlane, execution_type: str, execution_id: str,
) -> dict[str, Any] | None:
    row = control.state._connection().execute(
        "SELECT * FROM goal_execution_owners WHERE execution_type=? AND execution_id=?",
        (execution_type, execution_id),
    ).fetchone()
    return dict(row) if row else None
