"""Task orchestration upgrades (WP4 of v0.13.0).

Dependency helpers on top of the existing :mod:`services.tasks.models` plan
validation (which already rejects self/cyclic/missing dependencies). Adds
topological sequencing, retry policy, resume-safe state persistence and
failure propagation. No second execution loop: the orchestrator still runs.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from quantsmind_agent.services.tasks.models import (
    Plan,
    StepStatus,
    TaskStatus,
    validate_plan,
)

__all__ = [
    "RetryPolicy",
    "TaskStateStore",
    "dependency_order",
    "propagate_failure",
    "resume_candidates",
]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not (1 <= self.max_attempts <= 10):
            raise ValueError("max_attempts must be 1..10")


def dependency_order(plan: Plan) -> list[str]:
    """Topological order of step ids (dependencies first)."""
    validate_plan(plan)
    order: list[str] = []
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visited:
            return
        visited.add(step_id)
        step = next(s for s in plan.steps if s.id == step_id)
        for dep in step.depends_on:
            visit(str(dep))
        order.append(step_id)

    for step in plan.steps:
        visit(step.id)
    return order


def propagate_failure(
    plan: Plan, failed_step: str, completed: set[str]
) -> tuple[set[str], set[str]]:
    """Return (blocked, runnable): dependents of a failed step are blocked."""
    failed = {failed_step}
    changed = True
    blocked: set[str] = set()
    while changed:
        changed = False
        for step in plan.steps:
            if step.id in blocked or step.id in failed or step.id in completed:
                continue
            if any(dep in failed or dep in blocked for dep in step.depends_on):
                blocked.add(step.id)
                changed = True
    runnable = {s.id for s in plan.steps} - failed - blocked - completed
    return blocked, runnable


def resume_candidates(step_statuses: dict[str, str], plan: Plan) -> list[str]:
    """Steps safe to run on resume: never re-run completed mutations."""
    runnable: list[str] = []
    for step_id in dependency_order(plan):
        status = step_statuses.get(step_id, StepStatus.PENDING.value)
        if status in (StepStatus.SUCCEEDED.value,):
            continue
        step = next(s for s in plan.steps if s.id == step_id)
        if all(
            step_statuses.get(str(d), "") == StepStatus.SUCCEEDED.value for d in step.depends_on
        ):
            runnable.append(step_id)
    return runnable


@dataclass
class TaskStateStore:
    """Resume-safe orchestration state at ``<root>/.quantsmind/task_state.json``."""

    path: Path
    _data: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def open(cls, root: str | Path) -> TaskStateStore:
        path = Path(root) / ".quantsmind" / "task_state.json"
        store = cls(path=path)
        store.load()
        return store

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def record(self, task_id: str, step_id: str, status: str, attempts: int = 1) -> None:
        entry = self._data.setdefault(task_id, {"steps": {}, "updated_at": 0.0})
        entry["steps"][step_id] = {"status": status, "attempts": attempts, "at": time.time()}
        entry["updated_at"] = time.time()
        self.save()

    def attempts(self, task_id: str, step_id: str) -> int:
        return int(self._data.get(task_id, {}).get("steps", {}).get(step_id, {}).get("attempts", 0))

    def can_retry(self, task_id: str, step_id: str, policy: RetryPolicy) -> bool:
        return self.attempts(task_id, step_id) < policy.max_attempts

    def snapshot(self, task_id: str) -> dict[str, Any]:
        return dict(self._data.get(task_id, {}))

    def mark(self, task_id: str, status: TaskStatus) -> None:
        entry = self._data.setdefault(task_id, {"steps": {}, "updated_at": 0.0})
        entry["task_status"] = status.value
        entry["updated_at"] = time.time()
        self.save()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) if False else dict(self._data)
