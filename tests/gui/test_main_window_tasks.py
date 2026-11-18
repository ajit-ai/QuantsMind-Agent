"""The v0.4 workflow through the real application window.

A component with its own passing tests is not a feature. These tests drive
:class:`MainWindow` -- the object the user actually launches -- from opening a
project to cancelling a task, and assert the two things the milestone promised:
the task workflow is reachable, and approval remains a hard gate.

Two integration facts these tests pin down, both found the hard way:

* A task's steps go through the same permission broker as the chat, so a
  mutating step *asks*. Headless, that ask reaches the modal dialog and blocks
  the worker until somebody clicks it -- correct for a user, a deadlock in a
  test. The fixture below installs an approver, which is what clicking Allow does.
* Closing the window has to stop the worker. A leaked QThread keeps the process
  alive after the window is gone, and the application never exits.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.security.permissions import ApprovalOutcome
from quantsmind_agent.services.tasks.models import (
    ApprovalRecord,
    Plan,
    PlannedStep,
    RiskLevel,
    StepStatus,
    Task,
    TaskStatus,
    TraceEvent,
    TraceKind,
)
from quantsmind_agent.services.tasks.orchestrator import Orchestrator
from quantsmind_agent.sessions import Checkpoint
from quantsmind_agent.task_store import TaskStore
from tests.fakes import FakeModelProvider, text_response
from tests.gui.harness import GuiHarness


def _wait_for_terminal(qapp: object, orchestrator: Orchestrator, limit_ms: int = 5000) -> None:
    """Drain events until the task worker reaches a terminal state, or time runs out."""

    deadline = time.monotonic() + limit_ms / 1000
    while not orchestrator.task.terminal and time.monotonic() < deadline:
        qapp.processEvents()  # type: ignore[attr-defined]
        time.sleep(0.01)
    qapp.processEvents()  # type: ignore[attr-defined]


def read_plan(path: str = "greeting.py") -> Plan:
    """A one-step plan naming a registered tool and a file inside the project."""

    return Plan(
        objective="rewrite the greeting module",
        steps=(
            PlannedStep(
                id="write",
                description="write the new greeting",
                tool="write_file",
                arguments={"path": path, "content": "print('hello')\n"},
                risk=RiskLevel.LOW,
                expected_paths=(path,),
            ),
        ),
    )


@pytest.fixture
def opened(gui_harness: GuiHarness, sample_project: Path) -> GuiHarness:
    """A window with a project open, with an approver standing in for the user.

    The order matters: opening a project *rebuilds* the tool registry, so an
    approver installed before the rebuild is thrown away with the old registry
    and the next permission request reaches the modal dialog -- which blocks
    the worker thread until somebody clicks it.
    """

    gui_harness.open_project(sample_project)
    gui_harness.context.tools.permissions.set_approver(lambda request: ApprovalOutcome.ALLOW_ONCE)
    return gui_harness


def approve_and_attach(window: Any, plan: Plan | None = None) -> Orchestrator:
    """Drive the window's own entry points to a task that is approved and ready."""

    window.submit_task_plan(plan or read_plan())
    window._on_task_approval(True)
    orchestrator: Orchestrator = window.task_orchestrator
    assert orchestrator is not None
    return orchestrator


def workspace_root(gui_harness: GuiHarness) -> str:
    """The open project's root, for the tests that need to name it."""

    workspace = gui_harness.context.workspace
    assert workspace is not None
    return str(workspace.root)


def approved_plan_of(task: Any) -> Any:
    """Read the approved plan through a call, so the check is not narrowed away."""

    return task.approved_plan


class TestTheTaskWorkflowIsReachable:
    def test_the_task_view_is_reachable_from_the_window(self, opened: GuiHarness) -> None:
        window = opened.window

        window.show_task_view()

        assert window._views.currentIndex() == 1
        assert window._views.indexOf(window.task_panel) == 1

    def test_the_switcher_offers_chat_and_task(self, opened: GuiHarness) -> None:
        labels = [button.text() for button in opened.window._view_switch.buttons()]

        assert labels == ["Chat", "Task"]

    def test_switching_back_to_chat_works(self, opened: GuiHarness) -> None:
        window = opened.window
        window.show_task_view()
        assert window._views.currentIndex() == 1

        window.show_chat_view()

        assert window._views.currentIndex() == 0

    def test_a_task_is_created_against_the_open_project(self, opened: GuiHarness) -> None:
        window = opened.window

        orchestrator = approve_and_attach(window)

        assert orchestrator.task.workspace == workspace_root(opened)
        assert window.task_panel.run_button.isEnabled() is True


class TestProjectEvidenceReachesTheTask:
    def test_the_snapshot_the_plan_was_offered_with_is_recorded(self, opened: GuiHarness) -> None:
        from quantsmind_agent.workspace_intel import WorkspaceSnapshot

        window = opened.window
        snapshot = WorkspaceSnapshot(
            workspace=workspace_root(opened),
            project_kind="python-package",
            file_count=7,
            digest="digest-from-the-plan",
        )

        window.submit_task_plan(read_plan(), snapshot=snapshot)
        window._on_task_approval(True)

        orchestrator = window.task_orchestrator
        assert orchestrator is not None
        task = orchestrator.task
        assert task.workspace_snapshot is not None
        assert task.workspace_snapshot["digest"] == "digest-from-the-plan"
        assert task.workspace_snapshot["project_kind"] == "python-package"
        assert window._pending_snapshot is None

    def test_a_scan_from_the_previous_project_is_ignored(self, opened: GuiHarness) -> None:
        from quantsmind_agent.workspace_intel import WorkspaceSnapshot

        window = opened.window
        window.task_panel.show_workspace(None)
        window._pending_snapshot = None

        window._on_workspace_scan_finished(
            WorkspaceSnapshot(workspace="C:/somewhere/else", file_count=99)
        )

        assert window.task_panel.workspace_snapshot is None
        assert window._pending_snapshot is None

    def test_a_late_scan_does_not_replace_the_plans_snapshot(self, opened: GuiHarness) -> None:
        from quantsmind_agent.workspace_intel import WorkspaceSnapshot

        window = opened.window
        planned = WorkspaceSnapshot(
            workspace=workspace_root(opened), project_kind="python-package", digest="planned"
        )
        later = WorkspaceSnapshot(
            workspace=workspace_root(opened), project_kind="python-package", digest="later"
        )

        window.submit_task_plan(read_plan(), snapshot=planned)
        window._on_workspace_scan_finished(later)
        window._on_task_approval(True)

        orchestrator = window.task_orchestrator
        assert orchestrator is not None
        assert orchestrator.task.workspace_snapshot["digest"] == "planned"


class TestApprovalIsAHardGate:
    def test_nothing_runs_when_the_approval_is_missing(
        self, qapp: object, opened: GuiHarness
    ) -> None:
        window = opened.window
        orchestrator = approve_and_attach(window)
        orchestrator.task.approval = None

        window._on_task_run()
        _wait_for_terminal(qapp, orchestrator)

        assert orchestrator.task.executions == []
        assert "Approve the plan" in window.task_panel.status.text()

    def test_a_rejected_plan_is_not_runnable(self, opened: GuiHarness) -> None:
        window = opened.window
        window.submit_task_plan(read_plan())

        window._on_task_approval(False)

        orchestrator = window.task_orchestrator
        assert orchestrator is not None
        assert orchestrator.task.status is TaskStatus.CANCELLED

    def test_changing_the_plan_invalidates_the_approval(
        self, qapp: object, opened: GuiHarness
    ) -> None:
        window = opened.window
        orchestrator = approve_and_attach(window)
        assert orchestrator.task.approved_plan is not None

        orchestrator.submit_plan(read_plan("other.py"))
        window._on_task_run()
        _wait_for_terminal(qapp, orchestrator)

        assert approved_plan_of(orchestrator.task) is None
        assert orchestrator.task.executions == []

    def test_a_plan_reaching_outside_the_workspace_is_refused(self, opened: GuiHarness) -> None:
        from quantsmind_agent.services.tasks.models import PlanRejected

        window = opened.window
        escaping = Plan(
            objective="write outside the project",
            steps=(
                PlannedStep(
                    id="escape",
                    description="escape",
                    tool="write_file",
                    arguments={"path": "../escaped.py", "content": "x"},
                ),
            ),
        )

        workspace = opened.context.workspace
        assert workspace is not None

        with pytest.raises(PlanRejected):
            window._task_service.propose(escaping, workspace)


# __PART2__


class TestExecutionThroughTheWindow:
    def test_an_approved_task_runs_and_reports(self, qapp: object, opened: GuiHarness) -> None:
        window = opened.window
        orchestrator = approve_and_attach(window)

        window._on_task_run()
        _wait_for_terminal(qapp, orchestrator)

        assert orchestrator.task.status is TaskStatus.COMPLETED
        assert orchestrator.task.report is not None
        assert "greeting.py" in orchestrator.task.report.files_changed
        assert "completed" in window.task_panel.report.toPlainText().lower()

    def test_cancelling_stops_further_operations(self, qapp: object, opened: GuiHarness) -> None:
        window = opened.window
        orchestrator = approve_and_attach(window)

        window._on_task_cancel()
        window._on_task_run()
        _wait_for_terminal(qapp, orchestrator)

        assert orchestrator.task.status is TaskStatus.CANCELLED
        assert orchestrator.task.executions == []

    def test_pausing_a_task_stops_it(self, opened: GuiHarness) -> None:
        window = opened.window
        orchestrator = approve_and_attach(window)

        window._on_task_pause()

        assert orchestrator.task.status is TaskStatus.PAUSED

    def test_the_changed_file_is_shown_with_its_evidence(
        self, qapp: object, opened: GuiHarness
    ) -> None:
        window = opened.window
        orchestrator = approve_and_attach(window)
        window._on_task_run()
        _wait_for_terminal(qapp, orchestrator)

        change = orchestrator.task.file_changes[0]
        assert change.path == "greeting.py"
        assert change.operation == "created"
        assert change.before_hash == ""


class TestWindowLifecycle:
    def test_closing_the_window_stops_the_task_worker(
        self, qapp: object, opened: GuiHarness
    ) -> None:
        window = opened.window
        orchestrator = approve_and_attach(window)
        window._on_task_run()

        window.close()

        assert orchestrator.task.status is TaskStatus.CANCELLED
        assert window.task_panel._bridge is None

    def test_a_new_conversation_releases_the_task(self, opened: GuiHarness) -> None:
        window = opened.window
        approve_and_attach(window)

        window._on_new_conversation()

        assert window.task_orchestrator is None

    def test_a_task_does_not_disturb_the_chat(
        self, qapp: object, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        """A task is not a conversation; using one must not break the other."""

        gui_harness.open_project(sample_project)
        gui_harness.context.tools.permissions.set_approver(
            lambda request: ApprovalOutcome.ALLOW_ONCE
        )
        gui_harness.use_provider(FakeModelProvider([text_response("a normal answer")]))
        gui_harness.ask("what does this project do?")
        window = gui_harness.window

        orchestrator = approve_and_attach(window)
        window._on_task_run()
        _wait_for_terminal(qapp, orchestrator)

        assert "a normal answer" in gui_harness.transcript
        assert orchestrator.task.report is not None


class TestPlanGenerationUsesStructuredOutput:
    def test_a_too_short_objective_is_refused_without_calling_the_model(
        self, opened: GuiHarness
    ) -> None:
        """Propose now goes through the structured-output path, not the composer."""

        window = opened.window
        window.show_task_view()

        window.task_panel.objective.setText("hi")
        window.task_panel._on_propose()

        # The refusal is stated rather than silently ignored, and nothing was put
        # in the composer for a human to send by hand.
        assert window.input.toPlainText() == ""
        assert "more fully" in window.task_panel.status.text()
        assert window._plan_thread is None

    def test_an_unusable_model_answer_is_refused_with_reasons(self, opened: GuiHarness) -> None:
        from quantsmind_agent.services.tasks.planner import PlanProposal

        window = opened.window
        window.show_task_view()

        window._proposal_ready(PlanProposal(problems=("the model returned prose",)))

        assert "No usable plan" in window.task_panel.status.text()
        assert window._pending_plan is None

    def test_a_valid_proposal_is_offered_for_review(self, opened: GuiHarness) -> None:
        from quantsmind_agent.services.tasks.models import Plan, PlannedStep
        from quantsmind_agent.services.tasks.planner import PlanProposal

        window = opened.window
        window.show_task_view()
        proposed = Plan(
            objective="rewrite the greeting module",
            steps=(
                PlannedStep(
                    id="write",
                    description="write it",
                    tool="write_file",
                    arguments={"path": "a.py", "content": "hi\n"},
                ),
            ),
        )

        window._proposal_ready(PlanProposal(plan=proposed))

        assert window._pending_plan is proposed
        assert window._views.currentIndex() == 1

    def test_the_panel_never_approves_its_own_plan(self, opened: GuiHarness) -> None:
        """Pressing Propose must not create a runnable task."""

        window = opened.window
        window.show_task_view()
        window.task_panel.objective.setText("rewrite the greeting module")

        window.task_panel._on_propose()

        assert window.task_orchestrator is None
        assert window.task_panel.run_button.isEnabled() is False


def stored_session(store: TaskStore, workspace: Path, task_id: str = "sess1") -> Task:
    """A stored, interrupted session as a crash would leave it."""

    plan = Plan(
        objective="update the greeting",
        steps=(
            PlannedStep(
                id="write",
                description="write it",
                tool="write_file",
                arguments={"path": "a.py", "content": "hi\n"},
            ),
        ),
    )
    task = Task(id=task_id, objective=plan.objective, workspace=str(workspace))
    task.plan = plan
    task.move_to(TaskStatus.AWAITING_APPROVAL)
    task.approval = ApprovalRecord(
        plan_digest=plan.digest, workspace=str(workspace), approved=True, decided_at=1.0
    )
    task.move_to(TaskStatus.APPROVED)
    task.set_status("write", StepStatus.SUCCEEDED)
    task.move_to(TaskStatus.RUNNING)
    task.move_to(TaskStatus.PAUSED)
    task.record(TraceEvent(TraceKind.STEP, 1.0, "wrote a.py", step_id="write"))
    task.checkpoints.append(
        Checkpoint(
            session_id=task_id, plan_digest=plan.digest, completed_steps=("write",)
        ).to_dict()
    )
    store.save(task)
    return task


class TestWindowIntegration:
    def test_selecting_history_does_not_start_execution(self, opened: Any) -> None:
        window = opened.window
        workspace = opened.context.workspace
        assert workspace is not None
        store = TaskStore(window._task_service.store.root)
        stored_session(store, workspace.root)

        window._on_task_history_selected("sess1")

        assert window._inspecting is not None
        assert window.task_panel.timeline.toPlainText()

    def test_an_unreadable_record_says_so(self, opened: Any) -> None:
        window = opened.window

        window._on_task_history_selected("does-not-exist")

        assert "could not be read" in window.task_panel.status.text()
        assert window._inspecting is None

    def test_a_refused_resume_explains_itself(self, opened: Any) -> None:
        window = opened.window
        workspace = opened.context.workspace
        assert workspace is not None
        store = TaskStore(window._task_service.store.root)
        task = stored_session(store, workspace.root)
        # A step was in flight when the process died: its outcome is unknown, so
        # continuing is not the application's decision to make.
        task.set_status("write", StepStatus.RUNNING)
        assert task.plan is not None
        task.checkpoints[-1] = Checkpoint(
            session_id=task.id, plan_digest=task.plan.digest, in_flight_step="write"
        ).to_dict()
        store.save(task)
        window._inspecting = task

        window._on_task_resume()

        assert "cannot continue" in window.task_panel.status.text().lower()

    def test_a_completed_session_is_never_offered_a_resume(self, opened: Any) -> None:
        window = opened.window
        workspace = opened.context.workspace
        assert workspace is not None
        store = TaskStore(window._task_service.store.root)
        task = stored_session(store, workspace.root)
        task.move_to(TaskStatus.APPROVED)
        task.move_to(TaskStatus.RUNNING)
        task.move_to(TaskStatus.COMPLETED)
        store.save(task)

        verdict = window._verdict_for(task)

        assert verdict.resumable is False
        assert "complete" in verdict.describe()
