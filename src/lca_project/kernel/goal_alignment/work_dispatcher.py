"""Bounded process-local dispatch for durable goal-alignment work."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import threading
from typing import Any, Callable


class GoalWorkDispatcher:
    """Run blocking Agents outside Supervisor reconciliation threads.

    The database remains the queue and source of truth.  This dispatcher only
    owns process-local execution slots; restart recovery can safely resubmit
    any nonterminal row whose fenced execution ownership is no longer fresh.
    """

    def __init__(self, root: str | Path, *, max_workers: int = 4,
                 thread_prefix: str = "goal-work") -> None:
        self.root = Path(root).resolve()
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_prefix
        )
        self._lock = threading.Lock()
        self._futures: dict[str, Future[Any]] = {}

    def submit(self, key: str, operation: Callable[[], Any]) -> bool:
        with self._lock:
            current = self._futures.get(key)
            if current is not None and not current.done():
                return False
            future = self._pool.submit(operation)
            self._futures[key] = future
            future.add_done_callback(lambda completed, item=key: self._finished(item, completed))
            return True

    def _finished(self, key: str, future: Future[Any]) -> None:
        # Reading the exception prevents silent Future warnings. Durable Agent
        # rows and events carry the user-facing failure evidence.
        try:
            future.exception()
        except BaseException:
            pass
        with self._lock:
            if self._futures.get(key) is future:
                self._futures.pop(key, None)

    def active(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(key for key, future in self._futures.items() if not future.done())
            )

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=False)


_DISPATCHERS: dict[Path, GoalWorkDispatcher] = {}
_DISPATCHERS_LOCK = threading.Lock()
_LOGIC_DISPATCHERS: dict[Path, GoalWorkDispatcher] = {}


def dispatcher_for(root: str | Path) -> GoalWorkDispatcher:
    resolved = Path(root).resolve()
    with _DISPATCHERS_LOCK:
        dispatcher = _DISPATCHERS.get(resolved)
        if dispatcher is None:
            dispatcher = GoalWorkDispatcher(resolved)
            _DISPATCHERS[resolved] = dispatcher
        return dispatcher


def logic_dispatcher_for(root: str | Path) -> GoalWorkDispatcher:
    """Use isolated capacity so advisory reviews cannot delay repairs."""
    resolved = Path(root).resolve()
    with _DISPATCHERS_LOCK:
        dispatcher = _LOGIC_DISPATCHERS.get(resolved)
        if dispatcher is None:
            dispatcher = GoalWorkDispatcher(
                resolved, max_workers=2, thread_prefix="logic-audit"
            )
            _LOGIC_DISPATCHERS[resolved] = dispatcher
        return dispatcher


def dispatch_failure_triage(root: str | Path, triage_run_id: str) -> bool:
    resolved = Path(root).resolve()

    def execute() -> Any:
        from .failure_triage_agent import FailureTriageAgent
        return FailureTriageAgent(resolved).execute(triage_run_id)

    return dispatcher_for(resolved).submit(f"triage:{triage_run_id}", execute)


def dispatch_system_repair(root: str | Path, repair_run_id: str) -> bool:
    resolved = Path(root).resolve()

    def execute() -> Any:
        from .system_repair_agent import SystemRepairAgent
        return SystemRepairAgent(resolved).execute(repair_run_id)

    return dispatcher_for(resolved).submit(f"repair:{repair_run_id}", execute)


def dispatch_scm_publication(root: str | Path, repair_run_id: str) -> bool:
    resolved = Path(root).resolve()

    def execute() -> Any:
        from .system_repair_agent import SystemRepairAgent
        return SystemRepairAgent(resolved).publish_scm(repair_run_id)

    return dispatcher_for(resolved).submit(f"scm:{repair_run_id}", execute)


def dispatch_system_meta(root: str | Path) -> bool:
    resolved = Path(root).resolve()

    def execute() -> Any:
        from .meta_supervisor import SystemMetaSupervisor
        return SystemMetaSupervisor(
            resolved, supervisor_id="dashboard-system-meta"
        ).reconcile()

    return dispatcher_for(resolved).submit("system-meta", execute)


def dispatch_logic_audit(root: str | Path, audit_run_id: str) -> bool:
    """Execute one advisory logic review outside Worker/Supervisor critical paths."""
    resolved = Path(root).resolve()

    def execute() -> Any:
        from ..logic_audit import LogicAuditAgent
        return LogicAuditAgent(resolved).execute(audit_run_id)

    return logic_dispatcher_for(resolved).submit(f"logic-audit:{audit_run_id}", execute)
