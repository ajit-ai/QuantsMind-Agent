"""The task panel's history: evidence, including work that never finished.

An interrupted task is shown as interrupted rather than as failed, because whether
its last step took effect is exactly what the user cannot determine from the
interface -- and telling them it "failed" would be a claim nobody can support.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.gui.task.panel import TaskPanel
from quantsmind_agent.services.tasks.models import Plan, PlannedStep, StepStatus, Task, TaskStatus
from quantsmind_agent.task_store import TaskStore

pytestmark = pytest.mark.gui


@pytest.fixture
def panel(qapp: object) -> TaskPanel:
    return TaskPanel()


def store_running_task(store: TaskStore, workspace: Path, task_id: str = "run1") -> Task:
    """A task recorded mid-flight, as a crash would leave it."""

    task = Task(id=task_id, objective=f"change {task_id}.py", workspace=str(workspace))
    task.plan = Plan(
        objective=task.objective,
        steps=(
            PlannedStep(
                id="write",
                description="write",
                tool="write_file",
                arguments={"path": f"{task_id}.py", "content": "x\n"},
            ),
        ),
    )
    task.status = TaskStatus.RUNNING
    task.step_status["write"] = StepStatus.RUNNING
    store.save(task)
    return task


def done_task(store: TaskStore, workspace: Path, task_id: str = "done1") -> Task:
    task = Task(id=task_id, objective=f"finish {task_id}.py", workspace=str(workspace))
    task.status = TaskStatus.COMPLETED
    store.save(task)
    return task


class TestHistoryIsShown:
    def test_stored_tasks_appear_in_the_list(self, panel: TaskPanel, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        done_task(store, tmp_path)

        panel.set_history(store.list())

        assert panel.history.count() == 1
        assert "finish done1.py" in panel.history.item(0).text()

    def test_a_task_id_is_carried_with_its_row(self, panel: TaskPanel, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        done_task(store, tmp_path)

        panel.set_history(store.list())
        panel.history.setCurrentRow(0)

        assert panel.selected_task_id() == "done1"
        assert panel.summary_for("done1") is not None

    def test_nothing_selected_means_nothing_selected(
        self, panel: TaskPanel, tmp_path: Path
    ) -> None:
        store = TaskStore(tmp_path / "tasks")
        done_task(store, tmp_path)
        panel.set_history(store.list())

        assert panel.selected_task_id() is None

    def test_an_interrupted_task_is_marked_not_hidden(
        self, panel: TaskPanel, tmp_path: Path
    ) -> None:
        store = TaskStore(tmp_path / "tasks")
        store_running_task(store, tmp_path, "run1")
        done_task(store, tmp_path, "done1")

        panel.set_history(store.list(), interrupted_ids=frozenset({"run1"}))

        labels = [panel.history.item(row).text() for row in range(panel.history.count())]
        assert any(text.startswith("! ") for text in labels)
        assert sum(1 for text in labels if text.startswith("! ")) == 1

    def test_an_ordinary_task_is_not_marked(self, panel: TaskPanel, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        done_task(store, tmp_path)

        panel.set_history(store.list(), interrupted_ids=frozenset())

        assert not panel.history.item(0).text().startswith("! ")

    def test_an_empty_history_is_empty_not_broken(self, panel: TaskPanel, tmp_path: Path) -> None:
        panel.set_history(TaskStore(tmp_path / "tasks").list())

        assert panel.history.count() == 0


class TestSelectionIsAnnounced:
    def test_choosing_a_row_emits_its_id(
        self, qapp: object, panel: TaskPanel, tmp_path: Path
    ) -> None:
        store = TaskStore(tmp_path / "tasks")
        done_task(store, tmp_path)
        seen: list[str] = []
        panel.history_selected.connect(seen.append)
        panel.set_history(store.list())

        panel.history.setCurrentRow(0)

        assert seen == ["done1"]
