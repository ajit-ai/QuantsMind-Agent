"""History and recovery in the interface.

Two properties matter more than the widgets existing: opening history must not
run anything, and continuing must be a decision the user makes after being told
what it would do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.gui.task_panel import TaskPanel
from quantsmind_agent.sessions import Checkpoint, RecoveryDecision, RecoveryVerdict
from quantsmind_agent.task_store import TaskStore
from quantsmind_agent.tasks import (
    Plan,
    PlannedStep,
    StepStatus,
    Task,
    TaskStatus,
    TraceEvent,
    TraceKind,
)

pytestmark = pytest.mark.gui


def stored_task(store: TaskStore, workspace: Path, task_id: str = "sess1") -> Task:
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


@pytest.fixture
def panel(qapp: object) -> TaskPanel:
    return TaskPanel()


class TestTheTimeline:
    def test_an_empty_history_explains_itself(self, panel: TaskPanel) -> None:
        assert "Select a previous task" in panel.timeline.placeholderText()

    def test_a_stored_task_is_rendered(self, panel: TaskPanel, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        stored_task(store, tmp_path)

        panel.show_timeline(store.load("sess1"))

        text = panel.timeline.toPlainText()
        assert "update the greeting" in text
        assert "write" in text
        assert "Checkpoint" in text

    def test_the_trace_is_shown_as_a_timeline(self, panel: TaskPanel, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        stored_task(store, tmp_path)

        panel.show_timeline(store.load("sess1"))

        assert "Timeline:" in panel.timeline.toPlainText()

    def test_rendering_is_bounded_for_a_long_session(
        self, panel: TaskPanel, tmp_path: Path
    ) -> None:
        store = TaskStore(tmp_path / "tasks")
        task = stored_task(store, tmp_path)
        for index in range(500):
            task.record(TraceEvent(TraceKind.STEP, float(index), f"event {index}"))

        panel.show_timeline(task)

        lines = panel.timeline.toPlainText().splitlines()
        assert len(lines) < 200

    def test_a_task_with_no_trace_still_renders(self, panel: TaskPanel, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        task = stored_task(store, tmp_path)
        task.trace.clear()

        panel.show_timeline(task)

        assert "update the greeting" in panel.timeline.toPlainText()


class TestResumeControlsAreGuarded:
    def test_nothing_is_enabled_before_a_task_is_chosen(self, panel: TaskPanel) -> None:
        panel.set_history_actions()

        assert panel.resume_button.isEnabled() is False

    def test_a_safe_task_can_be_continued(self, panel: TaskPanel) -> None:
        verdict = RecoveryVerdict(RecoveryDecision.SAFE_TO_RESUME)

        panel.set_history_actions(verdict=verdict, inspecting_id="sess1")

        assert panel.resume_button.isEnabled() is True
        assert panel.resume_button.text() == "Continue this task"

    def test_a_task_needing_review_cannot_be_continued(self, panel: TaskPanel) -> None:
        verdict = RecoveryVerdict(
            RecoveryDecision.REQUIRES_REVIEW, ("a step may already have changed something",)
        )

        panel.set_history_actions(verdict=verdict, inspecting_id="sess1")

        assert panel.resume_button.isEnabled() is False
        assert panel.resume_button.text() == "Cannot continue"

    def test_the_reason_is_visible_without_pressing_anything(self, panel: TaskPanel) -> None:
        verdict = RecoveryVerdict(
            RecoveryDecision.NOT_RESUMABLE, ("this task is already complete",)
        )

        panel.set_history_actions(verdict=verdict, inspecting_id="sess1")

        assert "already complete" in panel.resume_button.toolTip()

    def test_a_running_task_cannot_be_continued(self, panel: TaskPanel) -> None:
        verdict = RecoveryVerdict(RecoveryDecision.SAFE_TO_RESUME)

        panel.set_history_actions(verdict=verdict, inspecting_id="sess1", running=True)

        assert panel.resume_button.isEnabled() is False

    def test_the_timeline_is_read_only(self, panel: TaskPanel) -> None:
        assert panel.timeline.isReadOnly() is True


class TestHistoryRowsCarryTheirId:
    def test_selecting_a_row_reports_the_session_id(self, panel: TaskPanel, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        stored_task(store, tmp_path)
        seen: list[str] = []
        panel.history_selected.connect(seen.append)

        panel.set_history(store.list())
        panel.history.setCurrentRow(0)

        assert seen == ["sess1"]

    def test_an_interrupted_session_is_marked_in_the_list(
        self, panel: TaskPanel, tmp_path: Path
    ) -> None:
        store = TaskStore(tmp_path / "tasks")
        stored_task(store, tmp_path)

        panel.set_history(store.list(), interrupted_ids=frozenset({"sess1"}))

        assert panel.history.item(0).text().startswith("! ")

    def test_a_corrupt_record_is_skipped_by_the_list(
        self, panel: TaskPanel, tmp_path: Path
    ) -> None:

        store = TaskStore(tmp_path / "tasks")
        stored_task(store, tmp_path)
        store.path_for("broken").write_text("{not json", encoding="utf-8")

        panel.set_history(store.list())

        assert panel.history.count() >= 1
