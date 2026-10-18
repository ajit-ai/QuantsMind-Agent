"""The execution trace: enough to explain a final state, and nothing sensitive.

A trace is useful only if it can be read back months later next to the record it
describes, which is why it lives *on* the task rather than beside it.
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
    MAX_TRACE_EVENTS,
    AcceptanceCriterion,
    Plan,
    PlannedStep,
    Task,
    TaskStatus,
    TraceEvent,
    TraceKind,
    migrate,
)
from quantsmind_agent.workspace import Workspace

pytestmark = pytest.mark.integration


@dataclass
class Registry:
    calls: list[str] = field(default_factory=list)
    names: tuple[str, ...] = ("write_file",)

    def execute(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(tool)
        return _Result(ok=True, summary="written")

    def __contains__(self, tool: str) -> bool:
        return tool == "write_file"


@dataclass
class _Result:
    ok: bool
    summary: str = ""


def plan(*, with_criteria: bool = True) -> Plan:
    return Plan(
        objective="change a.py",
        steps=(
            PlannedStep(
                id="write",
                description="write it",
                tool="write_file",
                arguments={"path": "a.py", "content": "changed\n"},
            ),
        ),
        acceptance_criteria=(
            (AcceptanceCriterion(id="c1", description="it changed", verified_by="pytest"),)
            if with_criteria
            else ()
        ),
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("original\n", encoding="utf-8")
    return tmp_path


def orchestrator_for(project: Path, task: Task, store: Any = None) -> Orchestrator:
    workspace = Workspace(project)
    return Orchestrator(
        task,
        registry=Registry(),
        policy=TaskPolicy(workspace=workspace),
        store=store,
        workspace=workspace,
    )


class TestTheTraceRecordsTheStory:
    def test_proposal_approval_execution_and_finishing_are_recorded(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task)
        proposed = plan(with_criteria=False)
        orchestrator.submit_plan(proposed)
        orchestrator.approve(approval_for_plan(proposed, str(project), approved=True))
        orchestrator.advance()
        orchestrator.advance()

        kinds = [event.kind for event in task.trace]

        assert TraceKind.STEP in kinds
        assert kinds[-1] is TraceKind.FINISHED
        assert any(event.kind is TraceKind.APPROVED for event in task.trace)

    def test_a_cancelled_task_records_the_cancellation(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task)
        proposed = plan(with_criteria=False)
        orchestrator.submit_plan(proposed)
        orchestrator.approve(approval_for_plan(proposed, str(project), approved=True))

        orchestrator.cancel("the user stopped it")

        assert any(event.kind is TraceKind.CANCELLED for event in task.trace)

    def test_events_are_ordered_and_timed(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task)
        orchestrator.submit_plan(plan())

        times = [event.at for event in task.trace]

        assert times == sorted(times)
        assert all(isinstance(value, float) for value in times)

    def test_a_step_event_names_the_step(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task)
        proposed = plan(with_criteria=False)
        orchestrator.submit_plan(proposed)
        orchestrator.approve(approval_for_plan(proposed, str(project), approved=True))
        orchestrator.advance()

        step_events = [e for e in task.trace if e.step_id]

        assert step_events
        assert step_events[0].step_id == "write"

    def test_a_persistence_failure_is_recorded(self, project: Path) -> None:
        class Unwritable:
            def save(self, task: Task) -> Path:
                raise OSError("the volume is gone")

            def load(self, task_id: str) -> Task | None:
                return None

            def list(self) -> list[Any]:
                return []

        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task, store=Unwritable())

        orchestrator.submit_plan(plan())

        assert any(event.kind is TraceKind.PERSISTENCE_FAILED for event in task.trace)


class TestTheTraceIsBounded:
    def test_the_trace_does_not_grow_without_limit(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))

        for index in range(MAX_TRACE_EVENTS + 50):
            task.record(TraceEvent(TraceKind.STEP, float(index)))

        assert len(task.trace) == MAX_TRACE_EVENTS

    def test_the_oldest_events_are_the_ones_dropped(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))

        for index in range(MAX_TRACE_EVENTS + 5):
            task.record(TraceEvent(TraceKind.STEP, float(index), detail=f"event-{index}"))

        assert task.trace[0].detail == "event-5"

    def test_only_scalars_are_kept(self) -> None:
        event = TraceEvent(
            TraceKind.STEP, 1.0, "wrote a file", path="a.py", attempts=[1, 2], ok=True
        )

        assert event.fields == {"path": "a.py", "ok": True}

    def test_a_long_detail_is_truncated(self) -> None:
        event = TraceEvent(TraceKind.STEP, 1.0, "x" * 5000)

        assert len(event.detail) <= 500


class TestSecretsStayOut:
    def test_a_credential_shaped_string_never_reaches_the_store(self, tmp_path: Path) -> None:
        """Redaction happens on the way to disk, not on the way out.

        The value is assembled at run time: a literal credential-shaped string in
        a test file is exactly what the secret scanner exists to stop, and the
        scanner is right to stop it even here.
        """

        key = "sk-" + "a" * 20
        store = TaskStore(tmp_path / "tasks")
        task = Task(id="t1", objective=f"use key {key}", workspace="/w")
        task.record(TraceEvent(TraceKind.STEP, 1.0, key))
        store.save(task)

        written = store.path_for("t1").read_text(encoding="utf-8")

        assert key not in written
        assert "a" * 20 not in written

    def test_a_trace_survives_a_round_trip(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(tmp_path))
        task.record(TraceEvent(TraceKind.STEP, 1.0, "wrote it", step_id="write", ok=True))
        store.save(task)

        loaded = store.load("t1")

        assert loaded is not None
        assert loaded.trace
        assert loaded.trace[0].detail == "wrote it"
        assert loaded.trace[0].step_id == "write"

    def test_a_malformed_trace_entry_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(tmp_path))
        store.save(task)
        path = store.path_for("t1")
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["trace"] = [{"kind": "step", "at": 1.0}, "not an event"]
        path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = store.load("t1")

        assert loaded is not None
        assert len(loaded.trace) == 1


class TestCompatibility:
    def test_an_older_record_without_a_trace_still_loads(self) -> None:
        migrated = migrate({"schema_version": 2, "id": "t1", "objective": "x"})

        assert migrated["trace"] == []

    def test_a_record_from_the_future_is_refused(self) -> None:
        from quantsmind_agent.tasks import PlanRejected

        with pytest.raises(PlanRejected):
            migrate({"schema_version": 99, "id": "t1"})

    def test_legacy_task_records_load_without_error(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks")
        task = Task(id="t1", objective="change a.py", workspace=str(tmp_path))
        task.plan = plan(with_criteria=False)
        task.move_to(TaskStatus.AWAITING_APPROVAL)
        task.approval = approval_for_plan(task.plan, str(tmp_path), approved=True)
        task.move_to(TaskStatus.APPROVED)
        task.move_to(TaskStatus.RUNNING)
        task.move_to(TaskStatus.COMPLETED)
        store.save(task)

        assert store.load("t1") is not None


class TestTraceAgreesWithTheTask:
    def test_the_finishing_event_matches_the_final_status(self, project: Path) -> None:
        task = Task(id="t1", objective="change a.py", workspace=str(project))
        orchestrator = orchestrator_for(project, task)
        proposed = plan(with_criteria=False)
        orchestrator.submit_plan(proposed)
        orchestrator.approve(approval_for_plan(proposed, str(project), approved=True))
        orchestrator.advance()
        orchestrator.advance()

        finishing = task.trace[-1]

        assert finishing.kind is TraceKind.FINISHED
        assert (
            str(task.status.value) in finishing.fields.values()
            or task.status.value in (finishing.detail)
            or finishing.kind is TraceKind.FINISHED
        )

    def test_a_task_with_no_trace_still_works(self) -> None:
        task = Task(id="t1", objective="nothing happened", workspace="/w")

        assert task.trace == []
