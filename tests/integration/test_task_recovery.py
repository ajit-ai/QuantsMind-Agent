"""Task durability, recovery, verification and bounded repair.

Recovery is the part of v0.4 with the most ways to be quietly wrong: replaying a
side effect, resuming with an approval that no longer matches, or reporting a
check that never ran as though it had. These tests drive the real store, the real
orchestrator and the real recovery analysis against a filesystem, because all
three of those failures are invisible in a mock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from quantsmind_agent.orchestrator import Orchestrator, hash_file
from quantsmind_agent.recovery import analyse_task, apply_decision, approval_is_stale
from quantsmind_agent.task_policy import TaskPolicy, approval_for_plan
from quantsmind_agent.task_store import TaskStore
from quantsmind_agent.tasks import (
    AcceptanceCriterion,
    CheckStatus,
    Plan,
    PlannedStep,
    PlanRejected,
    RiskLevel,
    StepStatus,
    Task,
    TaskBudget,
    TaskStatus,
    ToolExecutionRecord,
)
from quantsmind_agent.verification import Check, discover_checks
from quantsmind_agent.workspace import Workspace


class StubResult:
    def __init__(self, ok: bool = True, summary: str = "", error: str = "") -> None:
        self.ok = ok
        self.summary = summary
        self.error = error


class WritingRegistry:
    names: ClassVar[list[str]] = ["read_file", "write_file", "edit_file"]

    def __init__(self, root: Path, *, failing: set[str] | None = None) -> None:
        self.root = root
        self.failing = failing or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, name: str, arguments: dict[str, Any]) -> StubResult:
        self.calls.append((name, dict(arguments)))
        if name in self.failing:
            return StubResult(False, error="simulated failure")
        target = self.root / str(arguments.get("path", ""))
        if name in {"write_file", "edit_file"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(arguments.get("content", "")), encoding="utf-8")
        return StubResult(True, "ok")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("original\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("other\n", encoding="utf-8")
    return tmp_path


def plan_for(*steps: PlannedStep) -> Plan:
    return Plan(objective="change the modules", steps=steps)


def write_step(step_id: str, path: str, depends: tuple[str, ...] = ()) -> PlannedStep:
    return PlannedStep(
        id=step_id,
        description=f"write {path}",
        tool="write_file",
        arguments={"path": path, "content": "changed\n"},
        depends_on=depends,
        risk=RiskLevel.LOW,
        expected_paths=(path,),
    )


def orchestrator_for(
    project: Path,
    task: Task,
    registry: WritingRegistry | None = None,
    store: TaskStore | None = None,
) -> Orchestrator:
    workspace = Workspace(project)
    return Orchestrator(
        task,
        registry=registry or WritingRegistry(project),
        policy=TaskPolicy(workspace=workspace),
        store=store,
        workspace=workspace,
    )


def approved_orchestrator(
    project: Path,
    task: Task,
    registry: WritingRegistry | None = None,
    store: TaskStore | None = None,
) -> Orchestrator:
    """An orchestrator whose task already has an approved plan.

    Verification runs the project's commands, so it is gated the same way step
    execution is: these tests are about how check outcomes are *recorded*, not
    about the authorisation gate, which has its own tests.
    """

    orchestrator = orchestrator_for(project, task, registry, store)
    plan = plan_for(write_step("noop", "a.py"))
    orchestrator.submit_plan(plan)
    orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
    return orchestrator


# __PART2__


class TestTaskStore:
    def test_a_task_round_trips(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        task.plan = plan_for(write_step("write", "a.py"))

        store.save(task)
        loaded = store.load("t1")

        assert loaded is not None
        assert loaded.objective == "change a.py"
        assert loaded.plan is not None
        assert loaded.plan.steps[0].id == "write"

    def test_a_missing_task_is_absent_not_an_error(self, project: Path) -> None:
        assert TaskStore(project / ".tasks").load("nope") is None

    def test_a_corrupt_record_is_quarantined_not_deleted(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        store.root.mkdir(parents=True)
        (store.root / "broken.json").write_text("{ not json", encoding="utf-8")

        moved = store.verify_integrity()

        assert [path.name for path in moved] == ["broken.json.corrupt"]
        assert (store.root / "broken.json.corrupt").exists()
        assert store.load("broken") is None

    def test_a_record_from_a_newer_schema_is_refused(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        store.root.mkdir(parents=True)
        (store.root / "future.json").write_text(
            '{"schema_version": 99, "id": "future", "objective": "x", "workspace": "y"}',
            encoding="utf-8",
        )

        assert store.load("future") is None
        assert (store.root / "future.json.corrupt").exists()

    def test_budgets_survive_a_restart(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        task = Task(
            id="t1",
            objective="o",
            workspace=str(project),
            budget=TaskBudget(max_steps=7, max_repairs_per_step=1),
        )
        task.steps_executed = 3

        store.save(task)
        loaded = store.load("t1")

        assert loaded is not None
        assert loaded.steps_executed == 3
        assert loaded.budget.max_steps == 7
        assert loaded.budget.max_repairs_per_step == 1

    def test_pruning_leaves_unfinished_tasks_alone(self, project: Path) -> None:
        store = TaskStore(project / ".tasks", max_tasks=1)
        for index, status in enumerate(
            (TaskStatus.COMPLETED, TaskStatus.RUNNING, TaskStatus.FAILED)
        ):
            task = Task(id=f"t{index}", objective=f"o{index}", workspace=str(project))
            task.status = status
            store.save(task)

        removed = store.prune()

        assert store.exists("t1") is True
        assert removed >= 1


class TestRecovery:
    def test_a_task_with_nothing_uncertain_resumes(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task, store=store)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()

        reloaded = store.load("t1")
        assert reloaded is not None
        report = analyse_task(reloaded)

        assert report.decisions == []
        assert report.resumable is True

    def test_an_interrupted_step_becomes_a_decision_not_a_replay(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        task.plan = plan_for(write_step("write", "a.py"))
        task.set_status("write", StepStatus.RUNNING)
        task.status = TaskStatus.RUNNING
        store.save(task)

        reloaded = store.load("t1")
        assert reloaded is not None
        report = analyse_task(reloaded)

        assert any(item.kind == "interrupted_step" for item in report.decisions)
        assert report.resumable is False
        assert reloaded.status is TaskStatus.PAUSED

    def test_a_file_edited_after_the_task_is_a_decision(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        task.plan = plan_for(write_step("write", "a.py"))
        task.status = TaskStatus.RUNNING
        orchestrator = orchestrator_for(project, task, store=store)
        orchestrator.submit_plan(task.plan)
        orchestrator.approve(approval_for_plan(task.plan, str(project), approved=True))
        orchestrator.advance()

        (project / "a.py").write_text("edited by the user\n", encoding="utf-8")
        reloaded = store.load("t1")
        assert reloaded is not None
        report = analyse_task(reloaded)

        assert any(item.kind == "file_changed_since" for item in report.decisions)

    def test_a_missing_file_is_a_decision(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        task.plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task, store=store)
        orchestrator.submit_plan(task.plan)
        orchestrator.approve(approval_for_plan(task.plan, str(project), approved=True))
        orchestrator.advance()

        (project / "a.py").unlink()
        reloaded = store.load("t1")
        assert reloaded is not None
        report = analyse_task(reloaded)

        assert any(item.kind == "file_missing" for item in report.decisions)

    def test_a_changed_plan_invalidates_a_stored_approval(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task, store=store)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.submit_plan(plan_for(write_step("write", "a.py"), write_step("w2", "b.py")))

        reloaded = store.load("t1")
        assert reloaded is not None
        report = analyse_task(reloaded)

        assert approval_is_stale(reloaded) is True
        assert any(item.kind == "approval_invalid" for item in report.decisions)

    def test_a_command_with_an_unknown_outcome_is_flagged(self, project: Path) -> None:
        store = TaskStore(project / ".tasks")
        task = Task(id="t1", objective="run something", workspace=str(project))
        task.plan = plan_for(
            PlannedStep(
                id="run",
                description="run",
                tool="run_command",
                arguments={"command": "python", "arguments": ["-c", "print(1)"]},
                risk=RiskLevel.READ_ONLY,
            )
        )
        task.executions.append(
            ToolExecutionRecord(step_id="run", tool="run_command", ok=False, summary="")
        )
        store.save(task)

        reloaded = store.load("t1")
        assert reloaded is not None
        report = analyse_task(reloaded)

        assert any(item.kind == "uncertain_command" for item in report.decisions)

    def test_choosing_to_stop_ends_the_task(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        task.plan = plan_for(write_step("write", "a.py"))
        task.set_status("write", StepStatus.RUNNING)
        report = analyse_task(task)
        decision = report.decisions[0]

        apply_decision(task, decision, "stop")

        assert task.status is TaskStatus.FAILED
        assert all(item.resolved for item in task.recovery)

    def test_recovery_never_runs_anything_itself(self, project: Path) -> None:
        registry = WritingRegistry(project)
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        task.plan = plan_for(write_step("write", "a.py"))
        task.set_status("write", StepStatus.RUNNING)
        orchestrator = orchestrator_for(project, task, registry=registry)
        orchestrator.submit_plan(task.plan)
        orchestrator.approve(approval_for_plan(task.plan, str(project), approved=True))

        analyse_task(orchestrator.task)

        assert registry.calls == []


# __PART3__


class TestVerificationReporting:
    def test_a_passing_check_is_recorded_as_passed(self, project: Path) -> None:
        task = Task(id="t1", objective="verify", workspace=str(project))
        orchestrator = approved_orchestrator(project, task)

        results = orchestrator.verify([Check(id="ok", argv=("python", "-c", "print('fine')"))])

        assert results[0].status is CheckStatus.PASSED
        assert results[0].exit_code == 0

    def test_a_failing_check_is_not_a_pass(self, project: Path) -> None:
        task = Task(id="t1", objective="verify", workspace=str(project))
        orchestrator = approved_orchestrator(project, task)

        results = orchestrator.verify(
            [Check(id="bad", argv=("python", "-c", "raise SystemExit(3)"))]
        )

        assert results[0].status is CheckStatus.FAILED
        assert results[0].exit_code == 3

    def test_a_check_that_cannot_run_is_skipped_not_passed(self, project: Path) -> None:
        task = Task(id="t1", objective="verify", workspace=str(project))
        orchestrator = approved_orchestrator(project, task)

        results = orchestrator.verify([Check(id="absent", argv=("definitely-not-installed",))])

        assert results[0].status is CheckStatus.SKIPPED
        assert "not installed" in results[0].detail

    def test_a_blocked_command_is_reported_as_blocked(self, project: Path) -> None:
        task = Task(id="t1", objective="verify", workspace=str(project))
        orchestrator = approved_orchestrator(project, task)

        results = orchestrator.verify([Check(id="danger", argv=("format", "C:"))])

        assert results[0].status is CheckStatus.BLOCKED

    def test_a_hanging_check_is_inconclusive_rather_than_failing(self, project: Path) -> None:
        import time as _time

        task = Task(id="t1", objective="verify", workspace=str(project))
        orchestrator = approved_orchestrator(project, task)
        started = _time.monotonic()

        results = orchestrator.verify(
            [Check(id="slow", argv=("python", "-c", "import time; time.sleep(5)"))],
            timeout_seconds=1,
        )

        assert results[0].status is CheckStatus.BLOCKED
        assert _time.monotonic() - started < 4

    def test_the_report_lists_checks_that_could_not_run(self, project: Path) -> None:
        task = Task(id="t1", objective="verify", workspace=str(project))
        orchestrator = approved_orchestrator(project, task)
        orchestrator.verify([Check(id="absent", argv=("definitely-not-installed",))])

        report = orchestrator.report()

        assert report.unverified == ("absent (skipped)",)
        assert report.checks_passed == 0
        assert any("could not be run" in blocker for blocker in report.blockers)

    def test_discovery_returns_only_declared_checks(self, tmp_path: Path) -> None:
        assert discover_checks(tmp_path) == []

    def test_discovery_reads_this_projects_conventions(self) -> None:
        root = Path(__file__).resolve().parents[2]

        assert {"pytest", "ruff", "mypy"} <= {check.id for check in discover_checks(root)}


class TestBoundedRepair:
    def test_a_repair_is_admitted_and_counted(self, project: Path) -> None:
        task = Task(
            id="t1",
            objective="change a.py",
            workspace=str(project),
            budget=TaskBudget(max_repairs_per_step=2),
        )
        plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        task.set_status("write", StepStatus.FAILED)

        step = plan.step("write")
        assert step is not None
        attempt = orchestrator.repair(step, plan, rationale="wrong content")

        assert attempt.attempt == 1
        assert task.status is TaskStatus.REPAIRING
        assert len(task.repairs) == 1

    def test_the_repair_budget_is_enforced(self, project: Path) -> None:
        task = Task(
            id="t1",
            objective="change a.py",
            workspace=str(project),
            budget=TaskBudget(max_repairs_per_step=1),
        )
        plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        task.set_status("write", StepStatus.FAILED)
        step = plan.step("write")
        assert step is not None
        orchestrator.repair(step, plan)
        orchestrator.complete_repair(task.repairs[0], ok=False)

        exhausted = orchestrator.repair(step, plan)

        assert exhausted.status is StepStatus.BLOCKED
        assert task.status is TaskStatus.BLOCKED

    def test_a_repair_may_not_add_steps(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        task.set_status("write", StepStatus.FAILED)
        wider = plan_for(write_step("write", "a.py"), write_step("extra", "b.py"))

        step = plan.step("write")
        assert step is not None
        attempt = orchestrator.repair(step, wider)

        assert attempt.status is StepStatus.BLOCKED
        assert "outside the approved plan" in attempt.rationale

    def test_a_failed_repair_ends_the_task_failed(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        task.set_status("write", StepStatus.FAILED)
        step = plan.step("write")
        assert step is not None
        attempt = orchestrator.repair(step, plan)

        orchestrator.complete_repair(attempt, ok=False)

        assert task.status is TaskStatus.FAILED
        assert orchestrator.report().repairs_attempted == 1


# __PART4__


class TestAcceptanceScenarios:
    def test_a_multi_file_task_reports_only_what_it_verified(self, project: Path) -> None:
        task = Task(id="t1", objective="change both modules", workspace=str(project))
        plan = Plan(
            objective="change both modules",
            steps=(
                write_step("first", "a.py"),
                write_step("second", "b.py", depends=("first",)),
            ),
            acceptance_criteria=(
                AcceptanceCriterion(id="first", description="a.py updated"),
                AcceptanceCriterion(id="second", description="b.py updated", verified_by="pytest"),
            ),
        )
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        for _ in range(4):
            if orchestrator.advance().action == "finished":
                break
        orchestrator.verify([Check(id="pytest", argv=("python", "-c", "print(1)"), source="test")])

        report = orchestrator.report()

        assert set(report.files_changed) == {"a.py", "b.py"}
        statuses = {item.id: item.status for item in report.acceptance}
        assert statuses["first"] == "executed"
        assert statuses["second"] == "verified"
        assert (project / "a.py").read_text(encoding="utf-8") == "changed\n"

    def test_a_failed_verification_ends_the_task_failed(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()
        orchestrator.verify([Check(id="pytest", argv=("python", "-c", "raise SystemExit(1)"))])

        result = orchestrator.advance()

        assert result.action == "finished"
        assert task.status is TaskStatus.FAILED
        assert orchestrator.report().checks_failed == 1

    def test_cancellation_records_completed_and_incomplete_work(self, project: Path) -> None:
        task = Task(id="t1", objective="change both modules", workspace=str(project))
        plan = plan_for(
            write_step("first", "a.py"), write_step("second", "c.py", depends=("first",))
        )
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()

        orchestrator.cancel("user stopped it")

        assert task.status_of("first") is StepStatus.SUCCEEDED
        assert task.status_of("second") is StepStatus.PENDING
        assert (project / "a.py").exists()
        assert not (project / "c.py").exists()
        assert orchestrator.report().files_changed == ("a.py",)

    def test_a_plan_the_tools_cannot_support_is_refused_outright(self, project: Path) -> None:
        task = Task(id="t1", objective="do something impossible", workspace=str(project))
        orchestrator = orchestrator_for(project, task)

        with pytest.raises(PlanRejected):
            orchestrator.submit_plan(
                plan_for(PlannedStep(id="x", description="x", tool="deploy_production"))
            )

        assert task.status is TaskStatus.DRAFT
        assert task.plan is None

    def test_a_failing_tool_call_is_recorded_not_dropped(self, project: Path) -> None:
        registry = WritingRegistry(project, failing={"write_file"})
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task, registry=registry)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))

        result = orchestrator.advance()

        assert result.ok is False
        assert len(task.executions) == 1
        assert task.executions[0].error == "simulated failure"

    def test_file_change_evidence_records_before_and_after(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        plan = plan_for(write_step("write", "a.py"))
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        before = hash_file(project / "a.py")

        orchestrator.advance()

        change = task.file_changes[0]
        assert change.path == "a.py"
        assert change.operation == "modified"
        assert change.before_hash == before
        assert change.after_hash == hash_file(project / "a.py")
        assert change.before_hash != change.after_hash

    def test_a_creation_is_recorded_as_a_creation(self, project: Path) -> None:
        task = Task(id="t1", objective="add a module", workspace=str(project))
        plan = plan_for(write_step("new", "src/new.py"))
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))

        orchestrator.advance()

        change = task.file_changes[0]
        assert change.operation == "created"
        assert change.existed_before is False
        assert (project / "src" / "new.py").exists()

    def test_a_task_survives_a_restart_mid_execution(self, project: Path) -> None:
        from quantsmind_agent.task_store import TaskStore

        store = TaskStore(project / ".tasks")
        task = Task(id="t1", objective="change both modules", workspace=str(project))
        plan = plan_for(
            write_step("first", "a.py"), write_step("second", "b.py", depends=("first",))
        )
        orchestrator = orchestrator_for(project, task, store=store)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()

        stored = store.load("t1")
        assert stored is not None
        resumed = orchestrator_for(project, Task.from_dict(stored.to_dict()), store=store)

        assert resumed.task.status_of("first") is StepStatus.SUCCEEDED
        next_step = resumed.eligible_step()
        assert next_step is not None
        assert next_step.id == "second"

    def test_two_restarts_do_not_run_the_same_step_twice(self, project: Path) -> None:
        from quantsmind_agent.task_store import TaskStore

        store = TaskStore(project / ".tasks")
        registry = WritingRegistry(project)
        task = Task(id="t1", objective="change both modules", workspace=str(project))
        plan = plan_for(
            write_step("first", "a.py"), write_step("second", "b.py", depends=("first",))
        )
        orchestrator = orchestrator_for(project, task, registry=registry, store=store)
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()

        for _ in range(2):
            loaded = store.load("t1")
            assert loaded is not None
            snapshot = Task.from_dict(loaded.to_dict())
            again = orchestrator_for(project, snapshot, registry=registry, store=store)
            again.advance()

        writes = [call for call in registry.calls if call[0] == "write_file"]
        assert len(writes) == 2
        assert [call[1]["path"] for call in writes] == ["a.py", "b.py"]
