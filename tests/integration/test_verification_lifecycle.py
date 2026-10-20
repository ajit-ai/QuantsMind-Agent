"""Verification in the real lifecycle: what ran, and what it was allowed to say.

The three outcomes exist because two of them are easy to confuse. Each case here
asserts both the recorded outcome *and* the resulting task status -- a correct
verdict on a task that still reads "completed" would be a lie the user sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.orchestrator import Orchestrator
from quantsmind_agent.task_policy import TaskPolicy, approval_for_plan
from quantsmind_agent.task_store import TaskStore
from quantsmind_agent.tasks import (
    AcceptanceCriterion,
    CheckResult,
    CheckStatus,
    Plan,
    PlannedStep,
    Task,
    TaskBudget,
    TaskStatus,
    VerificationOutcome,
    migrate,
)
from quantsmind_agent.workspace import Workspace

pytestmark = pytest.mark.integration


@dataclass
class RecordingRegistry:
    calls: list[str] = field(default_factory=list)
    names: tuple[str, ...] = ("write_file",)

    def execute(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(tool)
        return _Result(ok=True, summary="written")

    def __contains__(self, tool: str) -> bool:
        return tool in self.names


@dataclass
class _Result:
    ok: bool
    summary: str = ""


def step(path: str = "a.py") -> PlannedStep:
    return PlannedStep(
        id="write",
        description=f"write {path}",
        tool="write_file",
        arguments={"path": path, "content": "changed\n"},
    )


def criterion(identifier: str = "c1", verified_by: str = "pytest") -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=identifier, description="the suite passes", verified_by=verified_by
    )


def plan_with_criteria(*criteria: AcceptanceCriterion) -> Plan:
    """A plan that promised evidence. Criteria are the application's, not the model's."""

    return Plan(objective="change a.py", steps=(step(),), acceptance_criteria=criteria)


def plain_plan() -> Plan:
    return Plan(objective="change a.py", steps=(step(),))


def orchestrator_for(project: Path, task: Task, store: Any = None) -> Orchestrator:
    workspace = Workspace(project)
    return Orchestrator(
        task,
        registry=RecordingRegistry(),
        policy=TaskPolicy(workspace=workspace),
        store=store,
        workspace=workspace,
    )


def approved(project: Path, plan: Plan, store: Any = None, **kwargs: Any) -> Orchestrator:
    task = Task(id="t1", objective=plan.objective, workspace=str(project), **kwargs)
    orchestrator = orchestrator_for(project, task, store)
    orchestrator.submit_plan(plan)
    orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
    return orchestrator


def run_steps(orchestrator: Orchestrator) -> None:
    orchestrator.advance()  # executes the step
    orchestrator.advance()  # settles


def with_check(
    orchestrator: Orchestrator,
    check_id: str = "pytest",
    status: CheckStatus = CheckStatus.PASSED,
    *,
    step_id: str = "",
    detail: str = "",
) -> None:
    orchestrator.task.checks.append(
        CheckResult(check_id=check_id, status=status, step_id=step_id, detail=detail)
    )
    orchestrator.task.checks_run += 1


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("original\n", encoding="utf-8")
    return tmp_path


class TestWhatVerificationIsRequired:
    def test_a_plan_without_criteria_does_not_require_verification(self, project: Path) -> None:
        """Unchanged behaviour: no promise of evidence, no verification step."""

        orchestrator = approved(project, plain_plan())

        assert orchestrator.verification_required is False

        run_steps(orchestrator)

        assert orchestrator.task.status is TaskStatus.COMPLETED
        assert orchestrator.task.verification is None

    def test_a_plan_with_criteria_requires_verification(self, project: Path) -> None:
        assert approved(project, plan_with_criteria(criterion())).verification_required is True

    def test_steps_alone_never_complete_a_task_that_promised_evidence(self, project: Path) -> None:
        orchestrator = approved(project, plan_with_criteria(criterion()))

        run_steps(orchestrator)

        assert orchestrator.task.status is TaskStatus.VERIFYING
        assert orchestrator.task.verification is None


class TestVerificationGatesCompletion:
    def test_a_passing_check_completes_and_records_the_verdict(self, project: Path) -> None:
        """The whole path: execute, park, verify, complete."""

        orchestrator = approved(project, plan_with_criteria(criterion()))
        run_steps(orchestrator)
        with_check(orchestrator, step_id="write")

        outcome = orchestrator.verify_current()
        orchestrator.advance()

        assert outcome is VerificationOutcome.PASS
        assert orchestrator.task.status is TaskStatus.COMPLETED
        assert orchestrator.verified is True
        record = orchestrator.task.verification
        assert record is not None
        assert record.criteria_verified == 1

    def test_a_failing_check_fails_the_task(self, project: Path) -> None:
        orchestrator = approved(project, plan_with_criteria(criterion()))
        run_steps(orchestrator)
        with_check(orchestrator, status=CheckStatus.FAILED)

        outcome = orchestrator.verify_current()
        orchestrator.advance()

        assert outcome is VerificationOutcome.FAIL
        assert orchestrator.task.status is TaskStatus.FAILED
        assert orchestrator.verified is False

    def test_a_check_that_could_not_run_is_inconclusive_never_a_pass(self, project: Path) -> None:
        """The failure mode this whole feature exists to prevent."""

        orchestrator = approved(project, plan_with_criteria(criterion()))
        run_steps(orchestrator)
        with_check(orchestrator, status=CheckStatus.SKIPPED, detail="pytest is not installed")

        outcome = orchestrator.verify_current()
        orchestrator.advance()

        assert outcome is VerificationOutcome.INCONCLUSIVE
        assert orchestrator.task.status is TaskStatus.BLOCKED
        assert orchestrator.verified is False

    def test_a_check_refused_by_policy_is_inconclusive_not_a_pass(self, project: Path) -> None:
        orchestrator = approved(project, plan_with_criteria(criterion()))
        run_steps(orchestrator)
        with_check(orchestrator, "danger", CheckStatus.BLOCKED, detail="refused by policy")

        assert orchestrator.verify_current() is VerificationOutcome.INCONCLUSIVE

    def test_a_passing_check_naming_no_criterion_is_inconclusive(self, project: Path) -> None:
        """Green tests that do not speak to the criteria prove nothing about them."""

        orchestrator = approved(project, plan_with_criteria(criterion("c1", "other")))
        run_steps(orchestrator)
        with_check(orchestrator)

        outcome = orchestrator.verify_current()

        assert outcome is VerificationOutcome.INCONCLUSIVE
        record = orchestrator.task.verification
        assert record is not None
        assert any("criteria" in reason for reason in record.reasons)

    def test_a_project_with_no_declared_checks_is_inconclusive(self, project: Path) -> None:
        orchestrator = approved(project, plan_with_criteria(criterion()))
        run_steps(orchestrator)

        outcome = orchestrator.verify_current()

        assert outcome is VerificationOutcome.INCONCLUSIVE
        record = orchestrator.task.verification
        assert record is not None
        assert "declares no checks" in record.reasons[0]
        assert orchestrator.task.checks[-1].status is CheckStatus.NOT_CONFIGURED


class TestVerificationIsGatedLikeExecution:
    def test_a_task_with_no_approved_plan_cannot_verify(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task)

        assert orchestrator.verify_current() is VerificationOutcome.INCONCLUSIVE
        assert task.verification is None

    def test_a_rejected_plan_cannot_verify(self, project: Path) -> None:
        plan = plan_with_criteria(criterion())
        task = Task(id="t1", objective=plan.objective, workspace=str(project))
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=False))

        assert orchestrator.verify_current() is VerificationOutcome.INCONCLUSIVE

    def test_editing_the_plan_after_verification_invalidates_the_evidence(
        self, project: Path
    ) -> None:
        """The recorded verdict belongs to the plan it verified, not to the task name."""

        orchestrator = approved(project, plan_with_criteria(criterion()))
        run_steps(orchestrator)
        with_check(orchestrator, step_id="write")
        orchestrator.verify_current()
        assert orchestrator.task.verification is not None

        orchestrator.task.plan = plan_with_criteria(criterion("c1", "other"))

        assert orchestrator.task.approved_plan is None


class TestBudgetsAndCancellation:
    def test_a_spent_time_budget_stops_verification_inconclusive(self, project: Path) -> None:
        orchestrator = approved(
            project, plan_with_criteria(criterion()), budget=TaskBudget(max_active_seconds=0)
        )
        run_steps(orchestrator)

        outcome = orchestrator.verify_current()

        assert outcome is VerificationOutcome.INCONCLUSIVE
        record = orchestrator.task.verification
        assert record is not None
        assert "time budget" in record.reasons[0]

    def test_a_recorded_verdict_is_not_overwritten_by_a_second_run(self, project: Path) -> None:
        orchestrator = approved(project, plan_with_criteria(criterion()))
        run_steps(orchestrator)
        with_check(orchestrator, step_id="write")
        assert orchestrator.verify_current() is VerificationOutcome.PASS

        with_check(orchestrator, status=CheckStatus.FAILED)

        assert orchestrator.verify_current() is VerificationOutcome.PASS

    def test_a_cancelled_task_does_not_advance_into_verification(self, project: Path) -> None:
        orchestrator = approved(project, plan_with_criteria(criterion()))
        run_steps(orchestrator)
        orchestrator.cancel("the user stopped it")

        result = orchestrator.advance()

        assert result.action == "cancelled"
        assert orchestrator.task.status is TaskStatus.CANCELLED


class TestPersistence:
    def test_the_verdict_survives_a_reload(self, tmp_path: Path, project: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        orchestrator = approved(project, plan_with_criteria(criterion()), store=store)
        run_steps(orchestrator)
        orchestrator.verify_current()

        loaded = store.load("t1")

        assert loaded is not None
        assert loaded.verification is not None
        assert loaded.verification.outcome is VerificationOutcome.INCONCLUSIVE

    def test_a_v1_record_migrates_to_never_verified(self, project: Path) -> None:
        """An older record has no verdict. That means unverified, not verified."""

        migrated = migrate(
            {
                "schema_version": 1,
                "id": "t1",
                "objective": "change a.py",
                "workspace": str(project),
                "status": "completed",
            }
        )

        assert migrated["verification"] is None
        assert migrated["schema_version"] >= 2
