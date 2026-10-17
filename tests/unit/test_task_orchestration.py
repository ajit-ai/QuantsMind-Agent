"""Plan validation and the orchestrator's state machine.

These cover the part of v0.4 that decides: what a plan may contain, when a step
is eligible, what an approval does and does not authorise, and how budgets
behave. They drive the real orchestrator against a stub registry, because the
point of these tests is the decisions, not the tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from quantsmind_agent.orchestrator import Orchestrator
from quantsmind_agent.task_policy import TaskPolicy, approval_for_plan
from quantsmind_agent.tasks import (
    ApprovalRecord,
    Plan,
    PlannedStep,
    PlanRejected,
    RiskLevel,
    StepStatus,
    Task,
    TaskBudget,
    TaskStatus,
    validate_plan,
)
from quantsmind_agent.workspace import Workspace


class StubResult:
    def __init__(self, ok: bool = True, summary: str = "", error: str = "") -> None:
        self.ok = ok
        self.summary = summary
        self.error = error


class StubRegistry:
    """A registry that records calls and writes real files."""

    names: ClassVar[list[str]] = [
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "run_command",
    ]

    def __init__(self, root: Path, *, failing: set[str] | None = None) -> None:
        self.root = root
        self.failing = failing or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, name: str, arguments: dict[str, Any]) -> StubResult:
        self.calls.append((name, dict(arguments)))
        # Injected failures win over the scripted behaviour, or a test asking for
        # a failing read would silently succeed.
        if name in self.failing:
            return StubResult(False, error="simulated failure")
        target = self.root / str(arguments.get("path", ""))
        if name == "write_file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(arguments.get("content", "")), encoding="utf-8")
            return StubResult(True, f"wrote {arguments.get('path')}")
        if name == "edit_file":
            target.write_text(str(arguments.get("new_text", "")), encoding="utf-8")
            return StubResult(True, f"edited {arguments.get('path')}")
        if name == "delete_file":
            target.unlink(missing_ok=True)
            return StubResult(True, "deleted")
        if name == "read_file":
            return StubResult(True, target.read_text(encoding="utf-8"))
        return StubResult(True, "ok")


def read_step(path: str = "a.py") -> PlannedStep:
    return PlannedStep(
        id="read",
        description="read the file",
        tool="read_file",
        arguments={"path": path},
        risk=RiskLevel.READ_ONLY,
    )


def write_step(path: str = "a.py", depends: tuple[str, ...] = ("read",)) -> PlannedStep:
    return PlannedStep(
        id="write",
        description="write the file",
        tool="write_file",
        arguments={"path": path, "content": "print('hi')\n"},
        depends_on=depends,
        risk=RiskLevel.LOW,
        expected_paths=(path,),
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("print('original')\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def parts(project: Path) -> tuple[Workspace, StubRegistry, TaskPolicy]:
    workspace = Workspace(project)
    registry = StubRegistry(project)
    return workspace, registry, TaskPolicy(workspace=workspace)


def make_task(project: Path, **budget: int) -> Task:
    return Task(
        id="task-1",
        objective="change the greeting",
        workspace=str(project),
        budget=TaskBudget(**budget) if budget else TaskBudget(),
    )


def approved_plan_of(task: Task) -> Plan | None:
    """The approved plan, read through a call so the check is not narrowed away.

    mypy narrows a property after an ``assert`` on it, which would make a later
    ``assert ... is None`` on the same property look unreachable -- and a type
    checker that refuses to look at the line where an approval is *withdrawn* is
    no use.
    """

    return task.approved_plan


# __PART2__


class TestPlanValidation:
    def test_a_good_plan_is_accepted(self) -> None:
        assert Plan(objective="change the greeting", steps=(read_step(), write_step())).digest

    def test_an_unknown_tool_is_rejected(
        self, parts: tuple[Workspace, StubRegistry, TaskPolicy]
    ) -> None:
        _, registry, _ = parts
        plan = Plan(
            objective="do something dangerous",
            steps=(PlannedStep(id="x", description="x", tool="rm_rf", arguments={}),),
        )
        with pytest.raises(PlanRejected) as caught:
            validate_plan(plan, known_tools=set(registry.names))
        assert any("unknown tool" in reason for reason in caught.value.reasons)

    def test_every_reason_is_reported_at_once(self) -> None:
        plan = Plan(
            objective="",
            steps=(PlannedStep(id="a b", description="x", tool="nope", depends_on=("z",)),),
        )
        with pytest.raises(PlanRejected) as caught:
            validate_plan(plan, known_tools={"read_file"})
        assert len(caught.value.reasons) >= 3

    def test_a_dependency_cycle_is_rejected(self) -> None:
        plan = Plan(
            objective="cycle between two steps",
            steps=(
                PlannedStep(id="a", description="x", tool="read_file", depends_on=("b",)),
                PlannedStep(id="b", description="x", tool="read_file", depends_on=("a",)),
            ),
        )
        with pytest.raises(PlanRejected) as caught:
            validate_plan(plan)
        assert any("cycle" in reason for reason in caught.value.reasons)

    def test_a_dependency_on_a_missing_step_is_rejected(self) -> None:
        plan = Plan(
            objective="depends on nothing that exists",
            steps=(PlannedStep(id="a", description="x", tool="read_file", depends_on=("ghost",)),),
        )
        with pytest.raises(PlanRejected) as caught:
            validate_plan(plan)
        assert any("not a step" in reason for reason in caught.value.reasons)

    def test_duplicate_step_ids_are_rejected(self) -> None:
        with pytest.raises(PlanRejected):
            validate_plan(
                Plan(objective="two steps with one name", steps=(read_step(), read_step()))
            )

    def test_a_traversing_path_is_rejected(self, project: Path) -> None:
        plan = Plan(
            objective="write outside the project",
            steps=(
                PlannedStep(
                    id="x", description="x", tool="write_file", arguments={"path": "../escaped.py"}
                ),
            ),
        )
        with pytest.raises(PlanRejected) as caught:
            validate_plan(plan, workspace=str(project))
        assert any("outside the workspace" in reason for reason in caught.value.reasons)

    def test_an_empty_plan_is_rejected(self) -> None:
        with pytest.raises(PlanRejected):
            validate_plan(Plan(objective="do nothing at all", steps=()))

    def test_a_plan_larger_than_the_budget_is_rejected(self) -> None:
        plan = Plan(
            objective="a very large plan",
            steps=tuple(read_step(f"f{index}.py") for index in range(5)),
        )
        with pytest.raises(PlanRejected):
            validate_plan(plan, max_steps=3)

    def test_malformed_arguments_deserialise_without_raising(self) -> None:
        """A plan from a model can carry anything; reading it must not explode."""

        plan = Plan.from_dict(
            {
                "objective": "do the thing",
                "steps": [
                    {"id": "a", "description": "x", "tool": "read_file", "arguments": "oops"}
                ],
            }
        )
        assert plan.steps[0].arguments == {}


class TestApprovalBinding:
    def test_nothing_runs_before_approval(self, project: Path, parts: tuple[Any, Any, Any]) -> None:
        workspace, registry, policy = parts
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        orch.submit_plan(Plan(objective="change the greeting", steps=(read_step(),)))

        result = orch.advance()

        assert result.action == "waiting"
        assert registry.calls == []
        assert task.status is TaskStatus.AWAITING_APPROVAL

    def test_an_approval_for_another_plan_is_refused(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, registry, policy = parts
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        orch.submit_plan(Plan(objective="change the greeting", steps=(read_step(),)))

        with pytest.raises(PlanRejected):
            orch.approve(
                ApprovalRecord(plan_digest="deadbeef", workspace=str(project), approved=True)
            )
        assert task.approved_plan is None

    def test_changing_the_plan_invalidates_the_approval(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, registry, policy = parts
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        first = orch.submit_plan(Plan(objective="change the greeting", steps=(read_step(),)))
        orch.approve(approval_for_plan(first, str(project), approved=True))
        assert approved_plan_of(task) is not None

        orch.submit_plan(
            Plan(objective="change the greeting", steps=(read_step(), write_step("b.py")))
        )

        assert approved_plan_of(task) is None
        result = orch.advance()
        assert result.action in {"waiting", "blocked"}
        assert registry.calls == []

    def test_a_rejected_plan_never_replaces_the_current_one(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, registry, policy = parts
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        good = orch.submit_plan(Plan(objective="change the greeting", steps=(read_step(),)))
        orch.approve(approval_for_plan(good, str(project), approved=True))

        with pytest.raises(PlanRejected):
            orch.submit_plan(
                Plan(objective="", steps=(PlannedStep(id="x", description="x", tool="ghost"),))
            )

        assert task.plan is good


# __PART3__


class TestScheduling:
    def test_steps_run_in_dependency_order(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, registry, policy = parts
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        plan = Plan(objective="change the greeting", steps=(read_step(), write_step()))
        orch.submit_plan(plan)
        orch.approve(approval_for_plan(plan, str(project), approved=True))

        first = orch.advance()
        second = orch.advance()

        assert [name for name, _ in registry.calls] == ["read_file", "write_file"]
        assert first.step_id == "read"
        assert second.step_id == "write"

    def test_a_step_whose_dependency_failed_is_skipped(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, _, policy = parts
        registry = StubRegistry(project, failing={"read_file"})
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        plan = Plan(objective="change the greeting", steps=(read_step(), write_step()))
        orch.submit_plan(plan)
        orch.approve(approval_for_plan(plan, str(project), approved=True))

        orch.advance()
        orch.advance()

        assert task.status_of("read") is StepStatus.FAILED
        assert task.status_of("write") is StepStatus.SKIPPED
        assert [name for name, _ in registry.calls] == ["read_file"]

    def test_a_plan_larger_than_the_step_budget_is_refused_at_submission(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        """Budget beats plan size: a plan bigger than the budget is never approved."""

        workspace, registry, policy = parts
        task = make_task(project, max_steps=1)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        plan = Plan(
            objective="change the greeting twice",
            steps=(
                PlannedStep(
                    id="one", description="a", tool="read_file", arguments={"path": "a.py"}
                ),
                PlannedStep(
                    id="two", description="b", tool="read_file", arguments={"path": "a.py"}
                ),
            ),
        )

        with pytest.raises(PlanRejected) as caught:
            orch.submit_plan(plan)

        assert any("limit of 1" in reason for reason in caught.value.reasons)
        assert registry.calls == []

    def test_a_budget_cannot_be_configured_past_its_ceiling(self) -> None:
        budget = TaskBudget.from_dict(
            {"max_steps": 10_000, "max_repairs_per_step": 999, "max_active_seconds": 10**9}
        )
        assert budget.max_steps <= TaskBudget.HARD_MAX_STEPS
        assert budget.max_repairs_per_step <= TaskBudget.HARD_MAX_REPAIRS
        assert budget.max_active_seconds <= TaskBudget.HARD_MAX_SECONDS

    def test_counters_survive_a_round_trip(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        from quantsmind_agent.tasks import Task as TaskModel

        workspace, registry, policy = parts
        task = make_task(project, max_steps=5)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        plan = Plan(objective="change the greeting", steps=(read_step(),))
        orch.submit_plan(plan)
        orch.approve(approval_for_plan(plan, str(project), approved=True))
        orch.advance()

        restored = TaskModel.from_dict(task.to_dict())

        assert restored.steps_executed == 1
        assert restored.budget.max_steps == 5
        assert restored.status_of("read") is StepStatus.SUCCEEDED


class TestPauseResumeCancel:
    def test_a_paused_task_does_nothing_until_resumed(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, registry, policy = parts
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        plan = Plan(objective="change the greeting", steps=(read_step(),))
        orch.submit_plan(plan)
        orch.approve(approval_for_plan(plan, str(project), approved=True))

        orch.pause()
        paused = orch.advance()
        assert paused.action == "paused"
        assert registry.calls == []

        orch.resume()
        resumed = orch.advance()

        assert resumed.action == "step"
        assert [name for name, _ in registry.calls] == ["read_file"]

    def test_cancellation_stops_further_operations(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, registry, policy = parts
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        plan = Plan(objective="change the greeting", steps=(read_step(), write_step()))
        orch.submit_plan(plan)
        orch.approve(approval_for_plan(plan, str(project), approved=True))
        orch.advance()

        orch.cancel("the user changed their mind")
        after = orch.advance()

        assert after.action == "cancelled"
        assert [name for name, _ in registry.calls] == ["read_file"]
        assert task.status is TaskStatus.CANCELLED
        assert task.status_of("write") is StepStatus.PENDING

    def test_resuming_after_a_plan_change_requires_new_approval(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, registry, policy = parts
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        plan = Plan(objective="change the greeting", steps=(read_step(),))
        orch.submit_plan(plan)
        orch.approve(approval_for_plan(plan, str(project), approved=True))
        orch.pause()

        orch.submit_plan(
            Plan(objective="change the greeting", steps=(read_step(), write_step("b.py")))
        )
        orch.resume()

        assert task.status is TaskStatus.AWAITING_APPROVAL
        assert registry.calls == []


class TestCompletion:
    def test_a_finished_task_reports_what_actually_ran(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, registry, policy = parts
        task = make_task(project)
        orch = Orchestrator(task, registry=registry, policy=policy, workspace=workspace)
        plan = Plan(objective="change the greeting", steps=(read_step(), write_step()))
        orch.submit_plan(plan)
        orch.approve(approval_for_plan(plan, str(project), approved=True))

        for _ in range(4):
            if orch.advance().action == "finished":
                break

        assert task.status is TaskStatus.COMPLETED
        report = task.report
        assert report is not None
        assert report.steps_completed == 2
        assert report.files_changed == ("a.py",)
        assert report.checks_passed == 0
        assert report.unverified == ()

    def test_an_unexpected_exception_becomes_a_failed_step(
        self, project: Path, parts: tuple[Any, Any, Any]
    ) -> None:
        workspace, _, policy = parts

        class Exploding(StubRegistry):
            def execute(self, name: str, arguments: dict[str, Any]) -> StubResult:
                raise RuntimeError("the tool exploded")

        task = make_task(project)
        orch = Orchestrator(task, registry=Exploding(project), policy=policy, workspace=workspace)
        plan = Plan(objective="change the greeting", steps=(read_step(),))
        orch.submit_plan(plan)
        orch.approve(approval_for_plan(plan, str(project), approved=True))

        result = orch.advance()

        assert result.ok is False
        assert task.status_of("read") is StepStatus.FAILED
        assert "exploded" in task.executions[-1].error
