"""The task panel, driven through a real window.

The panel is where a user finds out what the agent is about to do, approves or
refuses it, stops it, and reads what actually happened. These tests exercise it
through the panel and its worker thread, so the wiring -- buttons, threading,
rendering -- is covered rather than just the panel object.

Two things are asserted throughout: that controls reflect the orchestrator's real
state, and that no worker thread is left running when the test ends. A panel that
leaves a QThread alive would take the whole application down at shutdown, and a
worker that ran on the GUI thread would freeze it -- both of which happened.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
from PySide6.QtWidgets import QPushButton

from quantsmind_agent.gui.task.panel import TaskPanel, WorkspaceScanBridge
from quantsmind_agent.services.tasks.models import Plan, PlannedStep, RiskLevel, Task, TaskStatus
from quantsmind_agent.services.tasks.orchestrator import Orchestrator
from quantsmind_agent.services.tasks.policy import TaskPolicy, approval_for_plan
from quantsmind_agent.services.workspace.containment import Workspace


class StubResult:
    def __init__(self, ok: bool = True, summary: str = "", error: str = "") -> None:
        self.ok = ok
        self.summary = summary
        self.error = error


class PanelRegistry:
    names: ClassVar[list[str]] = ["read_file", "write_file"]

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, name: str, arguments: dict[str, Any]) -> StubResult:
        self.calls.append((name, dict(arguments)))
        target = self.root / str(arguments.get("path", ""))
        if name == "write_file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(arguments.get("content", "")), encoding="utf-8")
        return StubResult(True, "ok")


def _wait_for_terminal(qapp: object, task: Task, limit_ms: int = 5000) -> None:
    """Drain events until the worker reaches a terminal state, or time runs out.

    The worker runs on a QThread, so a single ``processEvents`` can return before
    it has done anything. Polling is what makes this deterministic rather than
    usually-passing.
    """

    deadline = time.monotonic() + limit_ms / 1000
    while not task.terminal and time.monotonic() < deadline:
        qapp.processEvents()  # type: ignore[attr-defined]
        time.sleep(0.01)
    qapp.processEvents()  # type: ignore[attr-defined]


def simple_plan() -> Plan:
    return Plan(
        objective="change a.py",
        steps=(
            PlannedStep(
                id="write",
                description="write the new content",
                tool="write_file",
                arguments={"path": "a.py", "content": "changed\n"},
                risk=RiskLevel.LOW,
                expected_paths=("a.py",),
            ),
        ),
    )


def approved_orchestrator(
    tmp_path: Path, plan: Plan | None = None
) -> tuple[Orchestrator, PanelRegistry, Task]:
    workspace = Workspace(tmp_path)
    registry = PanelRegistry(tmp_path)
    task = Task(id="t1", objective="change a.py", workspace=str(tmp_path))
    orchestrator = Orchestrator(
        task, registry=registry, policy=TaskPolicy(workspace=workspace), workspace=workspace
    )
    chosen = plan or simple_plan()
    orchestrator.submit_plan(chosen)
    orchestrator.approve(approval_for_plan(chosen, str(tmp_path), approved=True))
    return orchestrator, registry, task


# __PART2__


class TestPanelControlsReflectState:
    def test_run_is_disabled_until_a_plan_is_approved(self, qapp: object, tmp_path: Path) -> None:
        panel = TaskPanel()
        workspace = Workspace(tmp_path)
        task = Task(id="t", objective="change a.py", workspace=str(tmp_path))
        orchestrator = Orchestrator(
            task, registry=PanelRegistry(tmp_path), policy=TaskPolicy(workspace=workspace)
        )
        orchestrator.submit_plan(simple_plan())
        panel.attach(orchestrator)

        assert panel.run_button.isEnabled() is False

        plan = orchestrator.task.plan
        assert plan is not None
        orchestrator.approve(approval_for_plan(plan, str(tmp_path), approved=True))
        panel.refresh()
        assert panel.run_button.isEnabled() is True

        panel.shutdown()

    def test_the_plan_is_shown_for_review(self, qapp: object, tmp_path: Path) -> None:
        panel = TaskPanel()
        orchestrator, _registry, _task = approved_orchestrator(tmp_path)
        panel.attach(orchestrator)

        panel.show_plan(simple_plan())
        text = panel.plan_view.toPlainText()

        assert "change a.py" in text
        assert "write_file" in text
        assert "will touch: a.py" in text
        assert panel.approve_button.isEnabled() is True
        assert panel.reject_button.isEnabled() is True
        panel.shutdown()

    def test_controls_are_disabled_for_a_finished_task(self, qapp: object, tmp_path: Path) -> None:
        panel = TaskPanel()
        orchestrator, _registry, task = approved_orchestrator(tmp_path)
        task.status = TaskStatus.COMPLETED
        panel.attach(orchestrator)

        assert panel.run_button.isEnabled() is False
        assert panel.cancel_button.isEnabled() is False
        panel.shutdown()

    def test_the_controls_exist_and_start_disabled(self, qapp: object) -> None:
        panel = TaskPanel()

        labels = {button.text() for button in panel.findChildren(QPushButton)}
        assert {"Propose plan", "Approve", "Reject", "Run", "Pause", "Cancel"} <= labels
        assert panel.approve_button.isEnabled() is False
        assert panel.reject_button.isEnabled() is False
        assert panel.run_button.isEnabled() is False
        assert panel.pause_button.isEnabled() is False
        assert panel.cancel_button.isEnabled() is False
        panel.shutdown()


class TestProjectSummary:
    def test_the_panel_shows_the_bounded_project_facts(self, qapp: object) -> None:
        from quantsmind_agent.verification import Check
        from quantsmind_agent.workspace_intel import WorkspaceSnapshot

        panel = TaskPanel()
        snapshot = WorkspaceSnapshot(
            workspace="C:/projects/acme",
            project_kind="python-package",
            file_count=12,
            ecosystems=("python",),
            manifests=("pyproject.toml",),
            checks=(Check(id="pytest", argv=("python", "-m", "pytest")),),
        )

        panel.show_workspace(snapshot)

        summary = panel.project_summary.text()
        assert "acme" in summary
        assert "python-package" in summary
        assert "12 file(s)" in summary
        assert "pytest" in summary
        assert panel.workspace_snapshot is snapshot
        panel.shutdown()

    def test_closing_the_project_clears_the_summary(self, qapp: object) -> None:
        from quantsmind_agent.workspace_intel import WorkspaceSnapshot

        panel = TaskPanel()
        panel.show_workspace(WorkspaceSnapshot(workspace="C:/projects/acme"))

        panel.show_workspace(None)

        assert panel.project_summary.text() == "No project open."
        assert panel.workspace_snapshot is None
        panel.shutdown()

    def test_refresh_is_requested_rather_than_run_in_the_widget(self, qapp: object) -> None:
        panel = TaskPanel()
        requested: list[bool] = []
        panel.workspace_refresh_requested.connect(lambda: requested.append(True))

        panel.project_refresh.click()

        assert requested == [True]
        panel.shutdown()

    def test_a_refresh_runs_on_the_scan_thread(self, qapp: object) -> None:
        from quantsmind_agent.workspace_intel import WorkspaceSnapshot

        class StubIntelligence:
            def __init__(self) -> None:
                self.calls = 0

            def refresh_snapshot(self) -> WorkspaceSnapshot:
                self.calls += 1
                return WorkspaceSnapshot(workspace="C:/projects/acme", file_count=1)

        intelligence = StubIntelligence()
        bridge = WorkspaceScanBridge(intelligence)
        results: list[WorkspaceSnapshot] = []
        bridge.finished.connect(results.append)
        bridge.start()

        deadline = time.monotonic() + 5
        while not results and time.monotonic() < deadline:
            qapp.processEvents()  # type: ignore[attr-defined]
            time.sleep(0.01)
        qapp.processEvents()  # type: ignore[attr-defined]

        assert intelligence.calls == 1
        assert results and results[0].file_count == 1
        bridge.shutdown()


def test_placeholder_removed() -> None:
    """Anchor kept so the file ends on a test; the helpers above it are gone."""


class TestPanelRunsATask:
    def test_running_executes_the_plan_and_shows_the_result(
        self, qapp: object, tmp_path: Path
    ) -> None:
        panel = TaskPanel()
        orchestrator, registry, task = approved_orchestrator(tmp_path)
        panel.attach(orchestrator)
        panel.refresh()

        panel.start_worker()
        _wait_for_terminal(qapp, task)

        assert [name for name, _ in registry.calls] == ["write_file"]
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "changed\n"
        assert task.status is TaskStatus.COMPLETED
        assert panel.changes.count() == 1
        panel.shutdown()

    def test_the_worker_leaves_no_running_thread(self, qapp: object, tmp_path: Path) -> None:
        panel = TaskPanel()
        orchestrator, _registry, task = approved_orchestrator(tmp_path)
        panel.attach(orchestrator)

        panel.start_worker()
        _wait_for_terminal(qapp, task)
        panel.shutdown()

        assert panel._bridge is None

    def test_repeated_use_leaves_no_stale_workers(self, qapp: object, tmp_path: Path) -> None:
        """Cancel, then run another task, then close: nothing may be left behind."""

        panel = TaskPanel()
        first, _registry, first_task = approved_orchestrator(tmp_path)
        panel.attach(first)
        panel.request_cancel()
        assert first_task.status is TaskStatus.CANCELLED

        second, registry, second_task = approved_orchestrator(tmp_path)
        panel.attach(second)
        panel.refresh()
        panel.start_worker()
        _wait_for_terminal(qapp, second_task)

        assert second_task.status is TaskStatus.COMPLETED
        assert len(registry.calls) == 1
        panel.shutdown()
        assert panel._bridge is None
        panel.shutdown()

    def test_cancelling_stops_the_task(self, qapp: object, tmp_path: Path) -> None:
        panel = TaskPanel()
        orchestrator, registry, task = approved_orchestrator(tmp_path)
        panel.attach(orchestrator)

        panel.request_cancel()

        assert task.status is TaskStatus.CANCELLED
        assert registry.calls == []
        panel.shutdown()

    def test_shutdown_is_safe_without_a_task(self, qapp: object) -> None:
        panel = TaskPanel()
        panel.attach(None)
        panel.shutdown()
        panel.shutdown()

    def test_a_second_run_button_press_does_not_start_a_second_worker(
        self, qapp: object, tmp_path: Path
    ) -> None:
        panel = TaskPanel()
        orchestrator, registry, task = approved_orchestrator(tmp_path)
        panel.attach(orchestrator)
        panel.refresh()

        panel.start_worker()
        panel.start_worker()  # a second press, as a double click would produce
        _wait_for_terminal(qapp, task)

        assert len(registry.calls) == 1
        panel.shutdown()


class TestPanelRejectsWorkWithoutApproval:
    def test_a_rejected_plan_leaves_nothing_runnable(self, qapp: object, tmp_path: Path) -> None:
        from quantsmind_agent.services.tasks.models import PlanRejected

        panel = TaskPanel()
        workspace = Workspace(tmp_path)
        task = Task(id="t", objective="escape", workspace=str(tmp_path))
        orchestrator = Orchestrator(
            task, registry=PanelRegistry(tmp_path), policy=TaskPolicy(workspace=workspace)
        )
        bad = Plan(
            objective="write outside",
            steps=(
                PlannedStep(
                    id="x",
                    description="x",
                    tool="write_file",
                    arguments={"path": "../escaped.py"},
                ),
            ),
        )

        with pytest.raises(PlanRejected):
            orchestrator.submit_plan(bad)

        panel.attach(orchestrator)
        assert panel.run_button.isEnabled() is False
        panel.shutdown()


def _unused() -> Callable[[], None]:  # pragma: no cover - keeps the import honest
    return lambda: None
