"""Fenced leases prevent a stale worker from committing after a takeover."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .state import StateStore, utcnow


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    resource: str
    holder: str
    fencing_token: int
    expires_at: str


class LeaseManager:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _expiry(seconds: int) -> str:
        if seconds <= 0:
            raise ValueError("lease duration must be positive")
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")

    def acquire(self, resource: str, holder: str, seconds: int = 60) -> Lease:
        expiry = self._expiry(seconds)
        now = utcnow()
        with self.state.transaction() as conn:
            row = conn.execute("SELECT * FROM leases WHERE resource=?", (resource,)).fetchone()
            if row is not None and row["expires_at"] > now and row["holder"] != holder:
                raise LeaseLost(f"resource is leased by {row['holder']}")
            token = (row["fencing_token"] if row else 0) + (0 if row and row["holder"] == holder and row["expires_at"] > now else 1)
            conn.execute("INSERT INTO leases(resource,holder,fencing_token,expires_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET holder=excluded.holder,fencing_token=excluded.fencing_token,expires_at=excluded.expires_at,updated_at=excluded.updated_at", (resource, holder, token, expiry, now))
        return Lease(resource, holder, token, expiry)

    def renew(self, lease: Lease, seconds: int = 60) -> Lease:
        expiry = self._expiry(seconds)
        with self.state.transaction() as conn:
            changed = conn.execute("UPDATE leases SET expires_at=?,updated_at=? WHERE resource=? AND holder=? AND fencing_token=? AND expires_at>?", (expiry, utcnow(), lease.resource, lease.holder, lease.fencing_token, utcnow())).rowcount
            if changed != 1:
                raise LeaseLost(f"lease lost: {lease.resource}")
        return Lease(lease.resource, lease.holder, lease.fencing_token, expiry)

    def assert_valid(self, lease: Lease) -> None:
        row = self.state._connection().execute("SELECT * FROM leases WHERE resource=?", (lease.resource,)).fetchone()
        if row is None or row["holder"] != lease.holder or row["fencing_token"] != lease.fencing_token or row["expires_at"] <= utcnow():
            raise LeaseLost(f"lease lost: {lease.resource}")
