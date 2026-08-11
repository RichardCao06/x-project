"""Workflow contracts, compilation and explicit run-state transitions."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable


class WorkflowError(ValueError):
    pass


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    SKIPPED = "skipped"


TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.QUARANTINED, TaskState.SKIPPED}


@dataclass(frozen=True)
class TaskSpec:
    id: str
    capability: str
    depends_on: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 2


@dataclass(frozen=True)
class WorkflowSpec:
    id: str
    version: str
    tasks: tuple[TaskSpec, ...]

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "WorkflowSpec":
        # Public Workflow.v1 uses workflow_id/steps/needs.  Normalise it here
        # so the execution state machine has one internal representation.
        workflow_id = raw.get("workflow_id", raw.get("id"))
        source_tasks = raw.get("steps", raw.get("tasks", ()))
        tasks = tuple(TaskSpec(
            id=str(item["id"]), capability=str(item["capability"]),
            depends_on=tuple(item.get("needs", item.get("depends_on", ()))), inputs=dict(item.get("inputs", {})),
            max_attempts=int(item.get("max_attempts", 2)),
        ) for item in source_tasks)
        if not workflow_id or not raw.get("version") or not tasks:
            raise WorkflowError("workflow needs id, version and one or more tasks")
        return cls(str(workflow_id), str(raw["version"]), tasks)


@dataclass(frozen=True)
class CompiledWorkflow:
    spec: WorkflowSpec
    order: tuple[str, ...]
    dependencies: dict[str, frozenset[str]]
    children: dict[str, frozenset[str]]

    def ready(self, states: dict[str, TaskState]) -> tuple[str, ...]:
        return tuple(task_id for task_id in self.order if states[task_id] == TaskState.PENDING
                     and all(states[parent] == TaskState.SUCCEEDED for parent in self.dependencies[task_id]))


def compile_workflow(spec: WorkflowSpec, known_capabilities: Iterable[str] = ()) -> CompiledWorkflow:
    by_id = {task.id: task for task in spec.tasks}
    if len(by_id) != len(spec.tasks) or any(not key for key in by_id):
        raise WorkflowError("task ids must be unique and non-empty")
    known = set(known_capabilities)
    if known:
        unknown = sorted({task.capability for task in spec.tasks} - known)
        if unknown:
            raise WorkflowError(f"unknown capabilities: {', '.join(unknown)}")
    deps = {task.id: frozenset(task.depends_on) for task in spec.tasks}
    unknown_deps = sorted({dep for values in deps.values() for dep in values if dep not in by_id})
    if unknown_deps:
        raise WorkflowError(f"unknown task dependencies: {', '.join(unknown_deps)}")
    if any(task.max_attempts < 1 for task in spec.tasks):
        raise WorkflowError("max_attempts must be positive")
    children: dict[str, set[str]] = {key: set() for key in by_id}
    indegree = {key: len(value) for key, value in deps.items()}
    for child, parents in deps.items():
        for parent in parents:
            children[parent].add(child)
    ready = sorted(key for key, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()
    if len(order) != len(by_id):
        cyclic = sorted(key for key, degree in indegree.items() if degree)
        raise WorkflowError(f"workflow cycle detected: {', '.join(cyclic)}")
    return CompiledWorkflow(spec, tuple(order), deps, {key: frozenset(value) for key, value in children.items()})


@dataclass
class WorkflowRun:
    compiled: CompiledWorkflow
    states: dict[str, TaskState] = field(init=False)
    attempts: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.states = {task.id: TaskState.PENDING for task in self.compiled.spec.tasks}
        self.attempts = {task.id: 0 for task in self.compiled.spec.tasks}

    def claim_ready(self) -> tuple[str, ...]:
        claimed = self.compiled.ready(self.states)
        for task_id in claimed:
            self.states[task_id] = TaskState.READY
        return claimed

    def transition(self, task_id: str, to: TaskState) -> None:
        current = self.states[task_id]
        legal = {
            TaskState.PENDING: {TaskState.READY, TaskState.SKIPPED},
            TaskState.READY: {TaskState.RUNNING, TaskState.SKIPPED},
            TaskState.RUNNING: {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.QUARANTINED, TaskState.PENDING},
        }
        if to not in legal.get(current, set()):
            raise WorkflowError(f"illegal transition {task_id}: {current} -> {to}")
        if to == TaskState.RUNNING:
            self.attempts[task_id] += 1
        self.states[task_id] = to

    @property
    def terminal(self) -> bool:
        return all(state in TERMINAL for state in self.states.values())
