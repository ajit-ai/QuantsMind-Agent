"""Workspace facts as evidence: stored, traced, checkpointed, never authoritative.

The snapshot answers "what did the plan think it was working on?" months later.
These tests pin the boundary: it travels with the task record and the trace, but
it is not an approval, and a missing snapshot is not an empty workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.orchestrator import Orchestrator
from quantsmind_agent.services import TaskService
from quantsmind_agent.task_policy import TaskPolicy
from quantsmind_agent.task_store import TaskStore
from quantsmind_agent.tasks import (
    SCHEMA_VERSION,
    Plan,
    PlannedStep,
    Task,
    TaskStatus,
    TraceKind,
    migrate,
)
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.workspace_intel import discover_workspace

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
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "acme"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
        encoding="utf-8",
    )
    return tmp_path


def service_for(project: Path) -> TaskService:
    return TaskService(
        data_dir=project / "data",
        registry_factory=lambda _workspace: RecordingRegistry(),
        store=TaskStore(project / "tasks"),
    )


def orchestrator_for(project: Path, task: Task) -> Orchestrator:
    workspace = Workspace(project)
    return Orchestrator(
        task,
        registry=RecordingRegistry(),
        policy=TaskPolicy(workspace=workspace),
        workspace=workspace,
    )


class TestTheTaskRecordKeepsTheSnapshot:
    def test_propose_stores_the_snapshot_as_evidence(self, project: Path) -> None:
        snapshot = discover_workspace(project)
        service = service_for(project)

        orchestrator = service.propose(plan_for(), Workspace(project), snapshot=snapshot)

        assert orchestrator.task.workspace_snapshot == snapshot.to_dict()

    def test_a_snapshot_does_not_approve_the_plan(self, project: Path) -> None:
        service = service_for(project)

        orchestrator = service.propose(
            plan_for(), Workspace(project), snapshot=discover_workspace(project)
        )

        assert orchestrator.task.status is TaskStatus.AWAITING_APPROVAL
        assert orchestrator.task.approved_plan is None

    def test_the_snapshot_survives_a_round_trip(self, project: Path) -> None:
        snapshot = discover_workspace(project)
        task = Task(
            id="t1",
            objective="change a.py",
            workspace=str(project),
            workspace_snapshot=snapshot.to_dict(),
        )

        loaded = Task.from_dict(task.to_dict())

        assert loaded.workspace_snapshot == snapshot.to_dict()
        assert loaded.workspace_snapshot["digest"] == snapshot.digest

    def test_a_v4_record_migrates_to_no_snapshot(self) -> None:
        migrated = migrate(
            {
                "schema_version": 4,
                "id": "old",
                "objective": "change it",
                "workspace": "C:/project",
            }
        )

        assert migrated["schema_version"] == SCHEMA_VERSION
        assert migrated["workspace_snapshot"] is None


class TestTheTraceAndCheckpointsCarryIt:
    def test_submitting_a_plan_records_the_workspace_event(self, project: Path) -> None:
        snapshot = discover_workspace(project)
        task = Task(
            id="t1",
            objective="change a.py",
            workspace=str(project),
            workspace_snapshot=snapshot.to_dict(),
        )
        orchestrator = orchestrator_for(project, task)

        orchestrator.submit_plan(plan_for())

        workspace_events = [event for event in task.trace if event.kind is TraceKind.WORKSPACE]
        assert len(workspace_events) == 1
        assert workspace_events[0].fields["workspace_digest"] == snapshot.digest
        assert workspace_events[0].fields["project_kind"] == snapshot.project_kind
        assert workspace_events[0].fields["files"] == snapshot.file_count
        assert workspace_events[0].fields["truncated"] is snapshot.truncated

    def test_a_checkpoint_repeats_the_digest_as_scalars(self, project: Path) -> None:
        snapshot = discover_workspace(project)
        task = Task(
            id="t1",
            objective="change a.py",
            workspace=str(project),
            workspace_snapshot=snapshot.to_dict(),
        )
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan_for())

        orchestrator.record_checkpoint()

        notes = task.checkpoints[-1]["notes"]
        assert notes["workspace_digest"] == snapshot.digest
        assert notes["workspace_kind"] == snapshot.project_kind
        assert notes["workspace_files"] == snapshot.file_count

        checkpoint_events = [event for event in task.trace if event.kind is TraceKind.CHECKPOINT]
        assert checkpoint_events[-1].fields["workspace_digest"] == snapshot.digest

    def test_no_snapshot_means_no_invented_event(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task)

        orchestrator.submit_plan(plan_for())
        orchestrator.record_checkpoint()

        assert not any(event.kind is TraceKind.WORKSPACE for event in task.trace)
        assert "workspace_digest" not in task.checkpoints[-1]["notes"]
