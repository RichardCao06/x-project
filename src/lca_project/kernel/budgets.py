"""Transactional budget reservations used to cap autonomous retries/cost."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from .state import StateStore, utcnow


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Reservation:
    id: str
    scope: str
    units: int


class BudgetLedger:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def configure(self, scope: str, limit_units: int) -> None:
        if limit_units < 0:
            raise ValueError("limit cannot be negative")
        with self.state.transaction() as conn:
            row = conn.execute("SELECT reserved_units,consumed_units FROM budgets WHERE scope=?", (scope,)).fetchone()
            if row and limit_units < row["reserved_units"] + row["consumed_units"]:
                raise BudgetExceeded("new limit is below committed usage")
            conn.execute("INSERT INTO budgets(scope,limit_units,reserved_units,consumed_units,updated_at) VALUES(?,?,0,0,?) ON CONFLICT(scope) DO UPDATE SET limit_units=excluded.limit_units,updated_at=excluded.updated_at", (scope, limit_units, utcnow()))

    def reserve(self, scope: str, units: int, reservation_id: str | None = None) -> Reservation:
        if units <= 0:
            raise ValueError("reservation units must be positive")
        reservation_id = reservation_id or str(uuid.uuid4())
        with self.state.transaction() as conn:
            existing = conn.execute("SELECT * FROM budget_reservations WHERE id=?", (reservation_id,)).fetchone()
            if existing:
                return Reservation(existing["id"], existing["scope"], existing["units"])
            budget = conn.execute("SELECT * FROM budgets WHERE scope=?", (scope,)).fetchone()
            if budget is None or budget["consumed_units"] + budget["reserved_units"] + units > budget["limit_units"]:
                raise BudgetExceeded(f"budget exhausted: {scope}")
            conn.execute("UPDATE budgets SET reserved_units=reserved_units+?,updated_at=? WHERE scope=?", (units, utcnow(), scope))
            conn.execute("INSERT INTO budget_reservations(id,scope,units,status,created_at) VALUES(?,?,?,?,?)", (reservation_id, scope, units, "reserved", utcnow()))
        return Reservation(reservation_id, scope, units)

    def settle(self, reservation: Reservation, consumed_units: int) -> None:
        if not 0 <= consumed_units <= reservation.units:
            raise ValueError("consumption must be within reservation")
        with self.state.transaction() as conn:
            row = conn.execute("SELECT * FROM budget_reservations WHERE id=?", (reservation.id,)).fetchone()
            if row is None or row["status"] != "reserved":
                raise ValueError("reservation is not active")
            conn.execute("UPDATE budget_reservations SET status='settled',settled_at=? WHERE id=?", (utcnow(), reservation.id))
            conn.execute("UPDATE budgets SET reserved_units=reserved_units-?,consumed_units=consumed_units+?,updated_at=? WHERE scope=?", (row["units"], consumed_units, utcnow(), row["scope"]))

    def remaining(self, scope: str) -> int:
        row = self.state._connection().execute("SELECT limit_units-reserved_units-consumed_units AS remaining FROM budgets WHERE scope=?", (scope,)).fetchone()
        if row is None:
            raise KeyError(scope)
        return int(row["remaining"])
