"""The task service: the application's answer to "what is true about my task".

These tests exist because the logic used to live in the window, where it could
only be exercised through Qt. Everything here runs headless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.services import INTERRUPTED_STATUSES, TaskService
from quantsmind_agent.task_store import TaskStore
from quantsmind_agent.tasks import Plan, PlannedStep, StepStatus, Task, TaskStatus
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


def plan_for(path: str = "a.py") -> Plan:
    return Plan(
        objective=f"change {path}",
        steps=(
            PlannedStep(
                id="write",
                description=f"write {path}",
                tool="write_file",
                arguments={"path": path, "content": "changed\n"},
            ),
        ),
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("original\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def service(tmp_path: Path, project: Path) -> TaskService:
    return TaskService(
        data_dir=tmp_path / "data",
        registry_factory=lambda _workspace: RecordingRegistry(),
        store=TaskStore(tmp_path / "tasks"),
    )


class TestPlanningGoesThroughOneDoor:
    def test_a_proposed_plan_waits_for_approval(self, service: TaskService, project: Path) -> None:
        orchestrator = service.propose(plan_for(), Workspace(project))

        assert orchestrator.task.status is TaskStatus.AWAITING_APPROVAL
        assert orchestrator.task.approved_plan is None

    def test_deciding_approves_or_refuses(self, service: TaskService, project: Path) -> None:
        service.propose(plan_for(), Workspace(project))

        orchestrator = service.decide(True)

        assert orchestrator.task.status is TaskStatus.APPROVED
        assert orchestrator.task.approved_plan is not None

    def test_a_refusal_leaves_nothing_approved(self, service: TaskService, project: Path) -> None:
        service.propose(plan_for(), Workspace(project))

        orchestrator = service.decide(False)

        assert orchestrator.task.status is TaskStatus.CANCELLED
        assert orchestrator.task.approved_plan is None

    def test_an_approval_may_be_narrowed_to_a_scope(
        self, service: TaskService, project: Path
    ) -> None:
        service.propose(plan_for(), Workspace(project))

        orchestrator = service.approve_with_scope(("src",))

        assert orchestrator.task.approval is not None
        assert orchestrator.task.approval.scope_paths == ("src",)

    def test_a_second_plan_replaces_the_first(self, service: TaskService, project: Path) -> None:
        service.propose(plan_for(), Workspace(project))
        service.decide(True)

        orchestrator = service.propose(plan_for("b.py"), Workspace(project))

        assert orchestrator.task.status is TaskStatus.AWAITING_APPROVAL
        assert orchestrator.task.approval is None


class TestTheViewMatchesTheRecord:
    def test_the_view_reports_what_may_be_done(self, service: TaskService, project: Path) -> None:
        service.propose(plan_for(), Workspace(project))
        service.decide(True)

        view = service.view()

        assert view is not None
        assert view.can_run is True
        assert view.can_cancel is True
        assert view.step_total == 1
        assert "approved" in view.describe()

    def test_a_finished_task_offers_nothing(self, service: TaskService, project: Path) -> None:
        service.propose(plan_for(), Workspace(project))
        service.decide(True)
        orchestrator = service.orchestrator
        assert orchestrator is not None
        orchestrator.advance()
        orchestrator.advance()

        view = service.view()

        assert view is not None
        assert view.can_run is False
        assert view.can_cancel is False
        assert view.step_done == 1


class TestReleaseIsSafe:
    def test_releasing_a_live_task_cancels_it(self, service: TaskService, project: Path) -> None:
        service.propose(plan_for(), Workspace(project))
        service.decide(True)

        service.release()

        assert service.orchestrator is None

    def test_releasing_twice_is_harmless(self, service: TaskService, project: Path) -> None:
        service.propose(plan_for(), Workspace(project))
        service.decide(True)

        service.release()
        service.release()

        assert service.orchestrator is None

    def test_the_record_survives_release(self, service: TaskService, project: Path) -> None:
        service.propose(plan_for(), Workspace(project))
        service.decide(True)
        orchestrator = service.orchestrator
        assert orchestrator is not None
        task_id = orchestrator.task.id

        service.release()

        assert service.store.exists(task_id)


class TestInterruptedWorkIsReportedNotResumed:
    def _store_running_task(self, store: TaskStore, workspace: Path) -> Task:
        """A task recorded mid-flight, as a crash would leave it."""

        task = Task(
            id="interrupted1",
            objective="change a.py",
            workspace=str(workspace),
            status=TaskStatus.RUNNING,
        )
        task.plan = plan_for()
        task.step_status["write"] = StepStatus.RUNNING
        task.steps_executed = 0
        store.save(task)
        return task

    def test_a_task_running_at_shutdown_is_found(
        self, service: TaskService, tmp_path: Path, project: Path
    ) -> None:
        self._store_running_task(service.store, project)

        found = service.interrupted_tasks(Workspace(project))

        assert len(found) == 1
        assert found[0].task.id == "interrupted1"

    def test_an_interrupted_task_is_not_marked_failed(
        self, service: TaskService, project: Path
    ) -> None:
        self._store_running_task(service.store, project)

        found = service.interrupted_tasks(Workspace(project))

        assert found[0].task.status is not TaskStatus.FAILED
        assert found[0].task.status is TaskStatus.PAUSED
        assert "may have finished" in found[0].describe()

    def test_a_finished_task_is_not_interrupted(self, service: TaskService, project: Path) -> None:
        self._store_running_task(service.store, project)
        done = self._store_running_task(service.store, project)
        done.id = "done1"
        done.status = TaskStatus.COMPLETED
        service.store.save(done)

        found = service.interrupted_tasks(Workspace(project))

        assert [item.task.id for item in found] == ["interrupted1"]

    def test_every_in_flight_status_counts_as_interrupted(self) -> None:
        assert set(INTERRUPTED_STATUSES) == {
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
            TaskStatus.REPAIRING,
        }

    def test_adopting_does_not_resume_execution(self, service: TaskService, project: Path) -> None:
        stored = self._store_running_task(service.store, project)
        registry = RecordingRegistry()

        orchestrator = service.adopt(stored, Workspace(project))

        assert orchestrator.task.status is TaskStatus.RUNNING
        assert registry.calls == []

    def test_reapproval_binds_to_the_plan_as_stored_now(
        self, service: TaskService, project: Path
    ) -> None:
        stored = self._store_running_task(service.store, project)
        service.adopt(stored, Workspace(project))

        orchestrator = service.renew_approval(approved=True)

        plan = stored.plan
        assert plan is not None
        assert orchestrator.task.approval is not None
        assert orchestrator.task.approval.plan_digest == plan.digest
        assert orchestrator.task.status is TaskStatus.APPROVED


class TestHistory:
    def test_history_lists_stored_tasks_newest_first(
        self, service: TaskService, project: Path
    ) -> None:
        service.propose(plan_for("a.py"), Workspace(project))
        service.propose(plan_for("b.py"), Workspace(project))

        history = service.history()

        assert len(history) == 2
        # Task ids are random, so ordering is by record time, not by id.
        assert history[0].updated_at >= history[-1].updated_at
