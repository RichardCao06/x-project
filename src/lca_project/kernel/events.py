"""Append-only event ledger; state projections must be reconstructible from it."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from .state import StateStore, utcnow


@dataclass(frozen=True)
class Event:
    sequence: int
    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    trace_id: str | None
    actor: str | None
    occurred_at: str


class EventLedger:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def append(self, aggregate_type: str, aggregate_id: str, event_type: str, payload: dict[str, Any] | None = None, *, trace_id: str | None = None, actor: str | None = None, event_id: str | None = None) -> Event:
        if not all((aggregate_type, aggregate_id, event_type)):
            raise ValueError("aggregate type/id and event type are required")
        event_id = event_id or str(uuid.uuid4())
        occurred_at = utcnow()
        with self.state.transaction() as conn:
            try:
                cursor = conn.execute("INSERT INTO events(event_id,aggregate_type,aggregate_id,event_type,payload,trace_id,actor,occurred_at) VALUES(?,?,?,?,?,?,?,?)", (event_id, aggregate_type, aggregate_id, event_type, json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), trace_id, actor, occurred_at))
            except Exception as exc:
                if "UNIQUE constraint failed: events.event_id" not in str(exc):
                    raise
                row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
                expected_payload = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
                if (row["aggregate_type"], row["aggregate_id"], row["event_type"], row["payload"]) != (
                    aggregate_type, aggregate_id, event_type, expected_payload
                ):
                    raise ValueError(f"event_id collision with different envelope: {event_id}") from exc
                return self._row(row)
            row = conn.execute("SELECT * FROM events WHERE sequence=?", (cursor.lastrowid,)).fetchone()
        return self._row(row)

    def read(self, aggregate_type: str, aggregate_id: str, *, after: int = 0) -> Iterable[Event]:
        rows = self.state._connection().execute("SELECT * FROM events WHERE aggregate_type=? AND aggregate_id=? AND sequence>? ORDER BY sequence", (aggregate_type, aggregate_id, after))
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: Any) -> Event:
        return Event(row["sequence"], row["event_id"], row["aggregate_type"], row["aggregate_id"], row["event_type"], json.loads(row["payload"]), row["trace_id"], row["actor"], row["occurred_at"])
