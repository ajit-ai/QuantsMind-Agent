"""Orchestrator + TaskStateStore integration (Correction B)."""

from __future__ import annotations

from pathlib import Path

from quantsmind_agent.services.tasks.dependencies import TaskStateStore
from quantsmind_agent.services.tasks.models import (
    Plan,
    PlannedStep,
    RiskLevel,
    StepStatus,
    Task,
    TaskBudget,
    TaskStatus,
)
from quantsmind_agent.services.tasks.orchestrator import Orchestrator
from quantsmind_agent.services.tasks.policy import TaskPolicy, approval_for_plan
from quantsmind_agent.services.workspace.containment import Workspace


class _Result:
    def __init__(self, ok=True, summary="", error=""):
        self.ok, self.summary, self.error = ok, summary, error


class _Registry:
    from typing import ClassVar

    names: ClassVar[list[str]] = ["write_file"]

    def __init__(self, root: Path, failing=()):
        self.root = root
        self.failing = set(failing)
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name in self.failing:
            return _Result(False, error="simulated failure")
        target = self.root / str(arguments.get("path", ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(arguments.get("content", "")), encoding="utf-8")
        return _Result(True, f"wrote {arguments.get('path')}")


def _approved(tmp_path: Path, steps, registry, policy, workspace, state_store=None):
    task = Task(id="t1", objective="do objective", workspace=str(tmp_path), budget=TaskBudget())
    orch = Orchestrator(
        task, registry=registry, policy=policy, workspace=workspace, state_store=state_store
    )
    plan = Plan(objective="do objective", steps=steps)
    orch.submit_plan(plan)
    orch.approve(approval_for_plan(plan, str(tmp_path), approved=True))
    return task, orch


def _step(sid, depends=()):
    return PlannedStep(
        id=sid,
        description=sid,
        tool="write_file",
        arguments={"path": f"{sid}.txt", "content": sid},
        depends_on=depends,
        risk=RiskLevel.LOW,
        expected_paths=(f"{sid}.txt",),
    )


def test_topological_execution(tmp_path: Path) -> None:
    (tmp_path / "seed").write_text("x")
    ws = Workspace(tmp_path)
    reg = _Registry(tmp_path)
    policy = TaskPolicy(workspace=ws)
    store = TaskStateStore.open(tmp_path)
    steps = [_step("a"), _step("b", ("a",)), _step("c", ("b",))]
    _t, orch = _approved(tmp_path, steps, reg, policy, ws, state_store=store)
    while True:
        res = orch.advance()
        if res.action in ("finished", "blocked"):
            break
    assert [c[1]["path"] for c in reg.calls] == ["a.txt", "b.txt", "c.txt"]
    assert orch.task.status is TaskStatus.COMPLETED
    assert store.snapshot("t1")["steps"]["c"]["status"] == StepStatus.SUCCEEDED.value


def test_failed_prerequisite_blocks_dependent(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    reg = _Registry(tmp_path, failing={"write_file"})
    policy = TaskPolicy(workspace=ws)
    steps = [_step("a"), _step("b", ("a",))]
    _t, orch = _approved(
        tmp_path, steps, reg, policy, ws, state_store=TaskStateStore.open(tmp_path)
    )
    for _ in range(5):
        orch.advance()
    statuses = orch.task.step_status
    assert statuses["a"] is StepStatus.FAILED
    assert statuses["b"] is StepStatus.SKIPPED
    assert orch.task.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)


def test_restart_resume_no_repeat(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    reg = _Registry(tmp_path)
    policy = TaskPolicy(workspace=ws)
    store = TaskStateStore.open(tmp_path)
    steps = [_step("a"), _step("b", ("a",))]
    task, orch = _approved(tmp_path, steps, reg, policy, ws, state_store=store)
    orch.advance()  # runs a
    assert [c[1]["path"] for c in reg.calls] == ["a.txt"]
    # Simulate restart: new orchestrator over same persisted task + state store.
    store2 = TaskStateStore.open(tmp_path)
    orch2 = Orchestrator(task, registry=reg, policy=policy, workspace=ws, state_store=store2)
    restored = orch2.restore_from_state_store()
    assert "a" in restored
    orch2.advance()  # runs b only
    assert [c[1]["path"] for c in reg.calls] == ["a.txt", "b.txt"]
    assert orch2.task.step_status["a"] is StepStatus.SUCCEEDED


def test_retry_cap(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    reg = _Registry(tmp_path, failing={"write_file"})
    policy = TaskPolicy(workspace=ws)
    store = TaskStateStore.open(tmp_path)
    steps = [_step("a")]
    _t, orch = _approved(tmp_path, steps, reg, policy, ws, state_store=store)
    for _ in range(6):
        orch.advance()
    assert store.attempts("t1", "a") <= 4
    assert orch.task.step_status["a"] is StepStatus.FAILED


def test_cycle_rejected(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    reg = _Registry(tmp_path)
    policy = TaskPolicy(workspace=ws)
    task = Task(id="t1", objective="do objective", workspace=str(tmp_path), budget=TaskBudget())
    orch = Orchestrator(task, registry=reg, policy=policy, workspace=ws)
    import pytest

    from quantsmind_agent.services.tasks.models import PlanRejected

    with pytest.raises(PlanRejected):
        orch.submit_plan(
            Plan(objective="do objective", steps=[_step("a", ("b",)), _step("b", ("a",))])
        )


def test_permission_enforced_on_resume(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    reg = _Registry(tmp_path)
    policy = TaskPolicy(workspace=ws)
    steps = [_step("a")]
    _task, orch = _approved(
        tmp_path, steps, reg, policy, ws, state_store=TaskStateStore.open(tmp_path)
    )
    orch.advance()
    assert orch.task.step_status["a"] is StepStatus.SUCCEEDED
    # Withdraw approval representation: a fresh orchestrator with unapproved plan
    # must not execute (advance returns waiting/blocked, no extra registry call).
    task2 = Task(id="t2", objective="do objective", workspace=str(tmp_path), budget=TaskBudget())
    orch2 = Orchestrator(task2, registry=reg, policy=policy, workspace=ws)
    assert orch2.eligible_step() is None
