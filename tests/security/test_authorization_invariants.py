"""Security: the authorization invariants, tested as negative cases.

The specification for v0.5 asks for regression coverage of specific ways an
operation might reach execution when it should not. These tests are the answer,
and they are deliberately written against *services and models* rather than the
widgets that trigger them: a boundary enforced only in the GUI is not a boundary.

Each test drives :meth:`Orchestrator.advance` and then asserts on what actually
reached the registry, because "the policy said no" is a claim about the policy,
while "no tool ran" is a fact about the system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.services.tasks.models import (
    ALLOWED_TRANSITIONS,
    CheckStatus,
    Plan,
    PlannedStep,
    PlanRejected,
    Task,
    TaskStatus,
    can_transition,
)
from quantsmind_agent.services.tasks.orchestrator import Orchestrator
from quantsmind_agent.services.tasks.policy import TaskPolicy, approval_for_plan
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.verification import Check

pytestmark = pytest.mark.security


@dataclass
class RecordingRegistry:
    """A registry that records every call instead of doing anything."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    names: tuple[str, ...] = ("write_file",)

    def execute(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, dict(arguments)))
        return _Result(ok=True, summary=f"{tool} ran")

    def __contains__(self, tool: str) -> bool:
        return tool in self.names


@dataclass
class _Result:
    ok: bool
    summary: str = ""
    error: str = ""


def write_step(step_id: str = "write", path: str = "a.py", **kwargs: Any) -> PlannedStep:
    return PlannedStep(
        id=step_id,
        description=f"write {path}",
        tool="write_file",
        arguments={"path": path, "content": "changed\n"},
        **kwargs,
    )


def plan_for(*steps: PlannedStep) -> Plan:
    return Plan(objective="change a.py", steps=tuple(steps))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("original\n", encoding="utf-8")
    return tmp_path


def orchestrator_for(project: Path, task: Task, registry: RecordingRegistry) -> Orchestrator:
    workspace = Workspace(project)
    return Orchestrator(
        task,
        registry=registry,
        policy=TaskPolicy(workspace=workspace),
        workspace=workspace,
    )


def task_for(project: Path, **kwargs: Any) -> Task:
    return Task(id="t1", objective="change a.py", workspace=str(project), **kwargs)


class TestRunningWithoutApproval:
    def test_a_submitted_plan_executes_nothing(self, project: Path) -> None:
        """A plan awaiting approval must not reach a tool, whatever the caller wants."""

        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        orchestrator.submit_plan(plan_for(write_step()))

        for _ in range(5):
            orchestrator.advance()

        assert registry.calls == []
        assert task.status is TaskStatus.AWAITING_APPROVAL

    def test_a_draft_task_executes_nothing(self, project: Path) -> None:
        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)

        for _ in range(5):
            orchestrator.advance()

        assert registry.calls == []

    def test_verification_runs_nothing_without_an_approved_plan(self, project: Path) -> None:
        """Verification executes commands, so it needs the same authorisation."""

        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)

        results = orchestrator.verify([Check(id="pytest", argv=("python", "-c", "pass"))])

        assert results == []
        assert registry.calls == []
        assert task.status is TaskStatus.DRAFT


class TestRunningAfterRejection:
    def test_a_rejected_plan_never_executes(self, project: Path) -> None:
        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = plan_for(write_step())
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=False))

        for _ in range(5):
            orchestrator.advance()

        assert registry.calls == []
        assert task.status is TaskStatus.CANCELLED
        assert task.approved_plan is None


class TestApprovalAfterPlanModification:
    def test_editing_the_plan_invalidates_the_approval(self, project: Path) -> None:
        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = plan_for(write_step())
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))

        # The model, a repair, or a user edits the approved plan in place.
        edited = Plan(
            objective=plan.objective,
            steps=(write_step(path="secret.py"),),
            scope_paths=plan.scope_paths,
        )
        task.plan = edited

        for _ in range(5):
            orchestrator.advance()

        assert registry.calls == []
        assert task.approved_plan is None

    def test_approval_for_another_workspace_is_refused_at_approval_time(
        self, project: Path
    ) -> None:
        """Refused where it is offered, not silently unusable later."""

        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = plan_for(write_step())
        orchestrator.submit_plan(plan)
        foreign = approval_for_plan(plan, str(project / "elsewhere"), approved=True)

        with pytest.raises(Exception) as caught:
            orchestrator.approve(foreign)

        assert "workspace" in str(caught.value)
        assert registry.calls == []
        assert task.approved_plan is None

    def test_an_approval_for_another_plan_is_refused(self, project: Path) -> None:
        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = plan_for(write_step())
        orchestrator.submit_plan(plan)
        wrong = approval_for_plan(plan_for(write_step(path="b.py")), str(project), approved=True)

        with pytest.raises(Exception) as caught:
            orchestrator.approve(wrong)

        assert "different plan" in str(caught.value)
        assert registry.calls == []


class TestUnregisteredAndUnauthorizedCapabilities:
    def test_a_plan_naming_an_unregistered_tool_is_rejected(self, project: Path) -> None:
        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = Plan(
            objective="escape",
            steps=(
                PlannedStep(
                    id="s1",
                    description="run something unregistered",
                    tool="definitely_not_registered",
                    arguments={},
                ),
            ),
        )

        with pytest.raises(PlanRejected):
            orchestrator.submit_plan(plan)

        assert task.plan is None

    def test_a_step_outside_the_workspace_is_refused_at_validation(self, project: Path) -> None:
        """Refused before approval is offered: an out-of-scope plan is never approvable."""

        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)

        with pytest.raises(PlanRejected) as caught:
            orchestrator.submit_plan(plan_for(write_step(path="../outside.py")))

        assert "outside the workspace" in str(caught.value)
        assert registry.calls == []
        assert task.plan is None

    def test_a_path_traversal_argument_is_refused(self, project: Path) -> None:
        """`a.py/../../escaped.py` is outside the workspace, however it is spelled."""

        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)

        with pytest.raises(PlanRejected) as caught:
            orchestrator.submit_plan(plan_for(write_step(path="a.py/../../escaped.py")))

        assert "outside the workspace" in str(caught.value)
        assert registry.calls == []

    def test_a_step_outside_an_explicit_scope_is_blocked_at_execution(self, project: Path) -> None:
        """An approved plan whose step escapes its scope is stopped, not executed."""

        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = Plan(objective="change two files", steps=(write_step(path="b.py"),))
        scoped = Plan(
            objective=plan.objective,
            steps=plan.steps,
            scope_paths=("src",),
        )
        orchestrator.submit_plan(scoped)
        orchestrator.approve(approval_for_plan(scoped, str(project), approved=True))

        result = orchestrator.advance()

        assert result.action == "blocked"
        assert registry.calls == []
        assert task.status is TaskStatus.BLOCKED


class TestBypassingThePermissionBroker:
    def test_cancelling_does_not_become_completion(self, project: Path) -> None:
        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = plan_for(write_step())
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()  # runs the step
        orchestrator.advance()  # settles, having nothing left to do

        assert task.status is TaskStatus.COMPLETED
        orchestrator.cancel("too late")

        assert task.status is TaskStatus.COMPLETED
        assert len(registry.calls) == 1

    def test_a_duplicate_run_request_does_not_execute_twice(self, project: Path) -> None:
        """Two approvals, one execution: a stale approval cannot re-run a task."""

        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = plan_for(write_step())
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()
        calls_after_first = len(registry.calls)

        for _ in range(5):
            orchestrator.advance()

        assert len(registry.calls) == calls_after_first

    def test_a_terminal_task_cannot_be_restarted_by_reapproval(self, project: Path) -> None:
        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = plan_for(write_step())
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()
        task.status = TaskStatus.CANCELLED  # simulate the user stopping it

        for _ in range(5):
            orchestrator.advance()

        assert len(registry.calls) == 1


class TestLifecycleInvariants:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (TaskStatus.COMPLETED, TaskStatus.RUNNING),
            (TaskStatus.COMPLETED, TaskStatus.CANCELLED),
            (TaskStatus.COMPLETED, TaskStatus.APPROVED),
            (TaskStatus.CANCELLED, TaskStatus.RUNNING),
            (TaskStatus.CANCELLED, TaskStatus.APPROVED),
            (TaskStatus.FAILED, TaskStatus.COMPLETED),
            (TaskStatus.FAILED, TaskStatus.RUNNING),
            (TaskStatus.BLOCKED, TaskStatus.RUNNING),
            (TaskStatus.APPROVED, TaskStatus.COMPLETED),
        ],
    )
    def test_illegal_moves_are_refused(self, current: TaskStatus, target: TaskStatus) -> None:
        assert not can_transition(current, target)
        assert target not in ALLOWED_TRANSITIONS[current]

    def test_every_status_has_an_entry(self) -> None:
        """A status missing from the table would be an ungoverned status."""

        assert set(ALLOWED_TRANSITIONS) == set(TaskStatus)

    def test_terminal_states_are_governed_too(self) -> None:
        """Completion may still be verified; cancellation may never be undone."""

        assert ALLOWED_TRANSITIONS[TaskStatus.COMPLETED] == frozenset({TaskStatus.VERIFYING})
        assert ALLOWED_TRANSITIONS[TaskStatus.CANCELLED] == frozenset()


class TestFailureHonesty:
    def test_a_failed_check_is_never_reported_as_a_pass(self, project: Path) -> None:
        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = plan_for(write_step())
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()

        orchestrator.verify([Check(id="pytest", argv=("python", "-c", "raise SystemExit(1)"))])
        orchestrator.advance()

        report = orchestrator.report()
        assert task.status is TaskStatus.FAILED
        assert report.checks_failed == 1
        assert all(check.status is not CheckStatus.PASSED for check in task.checks)

    def test_stopping_a_task_is_never_reported_as_completion(self, project: Path) -> None:
        """Stopping is not succeeding: a stopped worker proves nothing."""

        registry = RecordingRegistry()
        task = task_for(project)
        orchestrator = orchestrator_for(project, task, registry)
        plan = plan_for(write_step())
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))

        orchestrator.cancel("the user stopped it")

        assert task.status is TaskStatus.CANCELLED
        assert registry.calls == []
