"""The task panel, driven through a real window.

The panel is where a user finds out what the agent is about to do, approves or
refuses it, stops it, and reads what actually happened. These tests exercise it
through the window so the wiring -- buttons, worker thread, rendering -- is
covered rather than just the panel object.

Two things are asserted throughout: that controls reflect the orchestrator's real
state, and that no worker thread is left running when the test ends. A panel that
leaves a QThread alive would take the whole application down at shutdown.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from quantsmind_agent.gui.task_panel import TaskPanel
from quantsmind_agent.orchestrator import Orchestrator
from quantsmind_agent.task_policy import TaskPolicy, approval_for_plan
from quantsmind_agent.tasks import Plan, PlannedStep, RiskLevel, Task, TaskStatus
from quantsmind_agent.workspace import Workspace


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


@pytest.fixture
def panel(qapp: object, tmp_path: Path) -> TaskPanel:
    (tmp_path / "a.py").write_text("original\n", encoding="utf-8")
    return TaskPanel()


@pytest.fixture
def orchestrator_for(tmp_path: Path) -> Callable[[], tuple[Orchestrator, PanelRegistry, Task]]:
    def build() -> tuple[Orchestrator, PanelRegistry, Task]:
        workspace = Workspace(tmp_path)
        registry = PanelRegistry(tmp_path)
        task = Task(id="t1", objective="change a.py", workspace=str(tmp_path))
        orchestrator = Orchestrator(
            task, registry=registry, policy=TaskPolicy(workspace=workspace), workspace=workspace
        )
        return orchestrator, registry, task

    return build


def _wait_for_terminal(qapp: object, task: Task, limit_ms: int = 5000) -> None:
    """Drain events until the worker reaches a terminal state, or time runs out.

    The worker runs on a QThread, so a single ``processEvents`` can return before
    it has done anything. Polling is what makes this test deterministic rather
    than usually-passing.
    """

    deadline = time.monotonic() + limit_ms / 1000
    while not task.terminal and time.monotonic() < deadline:
        _wait_for_terminal(qapp, task)
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


class TestPanelControlsReflectState:
    def test_run_is_disabled_until_a_plan_is_approved(
        self, panel: TaskPanel, orchestrator_for: Any
    ) -> None:
        orchestrator, _registry, _task = orchestrator_for()
        panel.attach(orchestrator)

        assert panel.run_button.isEnabled() is False

        orchestrator.submit_plan(simple_plan())
        panel.show_plan(simple_plan())
        assert panel.run_button.isEnabled() is False
        assert panel.approve_button.isEnabled() is True

        orchestrator.approve(
            approval_for_plan(simple_plan(), orchestrator.task.workspace, approved=True)
        )
        panel.refresh()
        assert panel.run_button.isEnabled() is True

        panel.shutdown()

    def test_the_plan_is_shown_for_review(self, panel: TaskPanel, orchestrator_for: Any) -> None:
        orchestrator, _registry, _task = orchestrator_for()
        panel.attach(orchestrator)
        plan = orchestrator.submit_plan(simple_plan())

        panel.show_plan(plan)
        text = panel.plan_view.toPlainText()

        assert "change a.py" in text
        assert "write_file" in text
        assert "will touch: a.py" in text
        panel.shutdown()

    def test_rejecting_is_offered_alongside_approving(
        self, panel: TaskPanel, orchestrator_for: Any
    ) -> None:
        orchestrator, _registry, _task = orchestrator_for()
        panel.attach(orchestrator)
        panel.show_plan(orchestrator.submit_plan(simple_plan()))

        assert panel.approve_button.isEnabled() is True
        assert panel.reject_button.isEnabled() is True
        panel.shutdown()

    def test_controls_are_disabled_for_a_finished_task(
        self, panel: TaskPanel, orchestrator_for: Any
    ) -> None:
        orchestrator, _registry, task = orchestrator_for()
        plan = orchestrator.submit_plan(simple_plan())
        orchestrator.approve(approval_for_plan(plan, task.workspace, approved=True))
        task.status = TaskStatus.COMPLETED
        panel.attach(orchestrator)

        assert panel.run_button.isEnabled() is False
        assert panel.cancel_button.isEnabled() is False
        panel.shutdown()


class TestPanelRunsATask:
    def test_running_executes_the_plan_and_shows_the_result(
        self, qapp: object, panel: TaskPanel, orchestrator_for: Any, tmp_path: Path
    ) -> None:
        orchestrator, registry, task = orchestrator_for()
        plan = orchestrator.submit_plan(simple_plan())
        orchestrator.approve(approval_for_plan(plan, task.workspace, approved=True))
        panel.attach(orchestrator)
        panel.refresh()

        panel._on_run()
        _wait_for_terminal(qapp, task)

        assert [name for name, _ in registry.calls] == ["write_file"]
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "changed\n"
        assert task.status is TaskStatus.COMPLETED
        assert panel.changes.count() == 1
        panel.shutdown()

    def test_cancelling_stops_the_task(self, panel: TaskPanel, orchestrator_for: Any) -> None:
        orchestrator, _registry, task = orchestrator_for()
        plan = orchestrator.submit_plan(simple_plan())
        orchestrator.approve(approval_for_plan(plan, task.workspace, approved=True))
        panel.attach(orchestrator)

        panel._on_cancel()

        assert task.status is TaskStatus.CANCELLED
        panel.shutdown()

    def test_shutdown_is_safe_without_a_task(self, panel: TaskPanel) -> None:
        panel.attach(None)
        panel.shutdown()
        panel.shutdown()

    def test_the_worker_leaves_no_running_thread(
        self, qapp: object, panel: TaskPanel, orchestrator_for: Any
    ) -> None:
        orchestrator, _registry, task = orchestrator_for()
        plan = orchestrator.submit_plan(simple_plan())
        orchestrator.approve(approval_for_plan(plan, task.workspace, approved=True))
        panel.attach(orchestrator)
        panel._on_run()
        _wait_for_terminal(qapp, task)

        panel.shutdown()

        assert panel._bridge is None
