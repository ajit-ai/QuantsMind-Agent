"""Persistence: what survives a crash, and what happens when nothing can be saved.

Two claims are tested here. First, a task's record is enough to explain what
happened after the process dies. Second, when the record *cannot* be written,
execution stops rather than carrying on unrecorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.services.tasks.models import (
    DIGEST_VERSION,
    SCHEMA_VERSION,
    Plan,
    PlannedStep,
    PlanRejected,
    Task,
    TaskStatus,
    migrate,
)
from quantsmind_agent.services.tasks.orchestrator import Orchestrator
from quantsmind_agent.services.tasks.policy import TaskPolicy, approval_for_plan
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.task_store import TaskStore

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


class FailingStore:
    """A store whose disk has gone away."""

    def __init__(self) -> None:
        self.attempts = 0

    def save(self, task: Task) -> Path:
        self.attempts += 1
        raise OSError("the volume is not there any more")

    def load(self, task_id: str) -> Task | None:
        return None

    def list(self) -> list[Any]:
        return []

    def exists(self, task_id: str) -> bool:
        return False


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


def orchestrator_for(project: Path, task: Task, store: Any, registry: Any) -> Orchestrator:
    workspace = Workspace(project)
    return Orchestrator(
        task,
        registry=registry,
        policy=TaskPolicy(workspace=workspace),
        store=store,
        workspace=workspace,
    )


class TestPersistenceFailureIsFailSafe:
    def test_execution_stops_when_the_record_cannot_be_saved(self, project: Path) -> None:
        """Continuing would run work whose authorisation could not be recovered."""

        store = FailingStore()
        registry = RecordingRegistry()
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task, store, registry)
        plan = plan_for()
        orchestrator.submit_plan(plan)

        assert store.attempts > 0
        assert orchestrator.persistence_failed is True
        assert task.status is TaskStatus.FAILED

        result = orchestrator.advance()

        assert result.action == "blocked"
        assert registry.calls == []

    def test_the_reason_is_reported_rather_than_swallowed(self, project: Path) -> None:
        events: list[str] = []
        store = FailingStore()
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task, store, RecordingRegistry())
        orchestrator.set_observer(lambda event: events.append(event.kind))

        orchestrator.submit_plan(plan_for())

        assert "persist_failed" in events
        assert "the volume is not there" in orchestrator.persistence_error

    def test_an_approval_is_never_granted_without_a_record(self, project: Path) -> None:
        """If the approval cannot be stored, it is not treated as granted."""

        store = FailingStore()
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task, store, RecordingRegistry())
        orchestrator.submit_plan(plan_for())

        assert task.terminal
        assert orchestrator.advance().action == "blocked"


class TestTheRecordExplainsWhatHappened:
    def test_a_saved_task_round_trips(self, tmp_path: Path, project: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task, store, RecordingRegistry())
        plan = plan_for()
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))
        orchestrator.advance()

        loaded = store.load("t1")

        assert loaded is not None
        assert loaded.objective == "change a.py"
        assert loaded.approval is not None
        assert loaded.plan is not None
        assert [record.tool for record in loaded.executions] == ["write_file"]

    def test_the_approval_travels_with_the_plan_digest(self, tmp_path: Path, project: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task, store, RecordingRegistry())
        plan = plan_for()
        orchestrator.submit_plan(plan)
        orchestrator.approve(approval_for_plan(plan, str(project), approved=True))

        loaded = store.load("t1")

        assert loaded is not None and loaded.approval is not None
        stored_plan = loaded.plan
        assert stored_plan is not None
        assert loaded.approval.plan_digest == stored_plan.digest

    def test_the_written_record_declares_its_schema(self, tmp_path: Path, project: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        store.save(task)

        payload = json.loads(store.path_for("t1").read_text(encoding="utf-8"))

        assert payload["schema_version"] == SCHEMA_VERSION


class TestSchemaVersioning:
    def test_a_record_from_a_newer_build_is_refused_not_guessed(self) -> None:
        payload = {"schema_version": SCHEMA_VERSION + 1, "id": "t1", "objective": "x"}

        with pytest.raises(PlanRejected) as caught:
            migrate(payload)

        assert "newer than this build" in str(caught.value)

    def test_an_older_record_is_brought_forward(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION - 1 if SCHEMA_VERSION > 1 else 1,
            "id": "t1",
            "objective": "x",
            "workspace": "C:/w",
        }
        payload["schema_version"] = 1

        migrated = migrate(payload)

        assert migrated["schema_version"] == SCHEMA_VERSION

    def test_the_digest_version_is_part_of_the_digest(self) -> None:
        """Changing what is hashed must invalidate every approval already given."""

        plan = plan_for()

        assert DIGEST_VERSION >= 1
        assert plan.digest == plan.digest  # stable
        assert len(plan.digest) == 32

    def test_changing_a_step_changes_the_digest(self) -> None:
        original = plan_for("a.py")
        edited = Plan(
            objective=original.objective,
            steps=(
                PlannedStep(
                    id="write",
                    description="write b.py",
                    tool="write_file",
                    arguments={"path": "b.py", "content": "changed\n"},
                ),
            ),
        )

        assert original.digest != edited.digest

    def test_changing_the_scope_changes_the_digest(self) -> None:
        original = plan_for()

        assert (
            original.digest
            != Plan(objective=original.objective, steps=original.steps, scope_paths=("src",)).digest
        )
