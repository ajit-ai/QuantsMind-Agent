"""Recovery safety: whether interrupted work may continue.

Most of these tests assert a *refusal*. That is the point: the question worth
asking is not "can we resume?" but "what would we be repeating if we did?", and
where the answer is unknown the correct outcome is to stop and say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.services.tasks.models import (
    Plan,
    PlannedStep,
    StepStatus,
    Task,
    TaskStatus,
    TraceKind,
)
from quantsmind_agent.services.tasks.service import RecoveryRefused, TaskService
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.sessions import (
    CHECKPOINT_SCHEMA_VERSION,
    Checkpoint,
    RecoveryDecision,
    RecoveryService,
    checkpoint_for,
    latest_checkpoint,
)
from quantsmind_agent.task_store import TaskStore

pytestmark = pytest.mark.security

TOOLS = frozenset({"read_file", "write_file", "edit_file"})


@dataclass
class Registry:
    calls: list[str] = field(default_factory=list)
    names: tuple[str, ...] = ("read_file", "write_file", "edit_file")

    def execute(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(tool)
        return _Result(ok=True, summary="done")

    def __contains__(self, tool: str) -> bool:
        return tool in self.names


@dataclass
class _Result:
    ok: bool
    summary: str = ""
    error: str = ""


def plan_with(*paths: str) -> Plan:
    names = paths or ("a.py",)
    return Plan(
        objective="change the files",
        steps=tuple(
            PlannedStep(
                id=f"s{index}",
                description=f"write {path}",
                tool="write_file",
                arguments={"path": path, "content": "changed\n"},
                depends_on=() if index == 0 else (f"s{index - 1}",),
            )
            for index, path in enumerate(names)
        ),
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("original\n", encoding="utf-8")
    return tmp_path


def service_for(project: Path) -> TaskService:
    return TaskService(
        data_dir=project / "data",
        registry_factory=lambda _w: Registry(),
        store=TaskStore(project / "tasks"),
    )


def session(project: Path, plan: Plan | None = None) -> TaskService:
    """A service holding one approved session, as if it had been left running."""

    service = service_for(project)
    proposed = plan or plan_with()
    service.propose(proposed, Workspace(project))
    service.decide(True)
    return service


def decide_for(project: Path, task: Task, tools: frozenset[str] = TOOLS) -> Any:
    return RecoveryService(known_tools=tools, current_workspace=str(project)).decide(task)


def task_of(service: TaskService) -> Task:
    orchestrator = service.orchestrator
    assert orchestrator is not None
    return orchestrator.task


def runner_of(service: TaskService) -> Any:
    """The current orchestrator, for tests that drive it directly."""

    orchestrator = service.orchestrator
    assert orchestrator is not None
    return orchestrator


class TestTheDecisionIsFailClosed:
    def test_a_clean_interruption_before_any_step_is_safe(self, project: Path) -> None:
        service = session(project)
        task = task_of(service)

        verdict = decide_for(project, task)

        assert verdict.decision is RecoveryDecision.SAFE_TO_RESUME
        assert verdict.resumable is True

    def test_a_step_in_flight_requires_review(self, project: Path) -> None:
        """The step may already have changed something; nobody can prove it did not."""

        task = task_of(session(project))
        task.set_status("s0", StepStatus.RUNNING)

        verdict = decide_for(project, task)

        assert verdict.decision is RecoveryDecision.REQUIRES_REVIEW
        assert any("may already have changed" in reason for reason in verdict.reasons)

    def test_a_completed_task_is_not_resumed(self, project: Path) -> None:
        task = task_of(session(project))
        task.move_to(TaskStatus.RUNNING)
        task.move_to(TaskStatus.COMPLETED)

        verdict = decide_for(project, task)

        assert verdict.decision is RecoveryDecision.NOT_RESUMABLE
        assert "already complete" in " ".join(verdict.reasons)

    def test_a_changed_plan_is_not_resumed(self, project: Path) -> None:
        task = task_of(session(project))
        task.checkpoints.append(checkpoint_for(task).to_dict())
        task.plan = plan_with("a.py", "c.py")

        verdict = decide_for(project, task)

        assert verdict.decision is RecoveryDecision.REQUIRES_REVIEW
        assert any("plan changed" in reason for reason in verdict.reasons)

    def test_a_missing_tool_is_refused(self, project: Path) -> None:
        task = task_of(session(project))

        verdict = decide_for(project, task, tools=frozenset({"read_file"}))

        assert verdict.decision is RecoveryDecision.NOT_RESUMABLE
        assert any("no longer available" in reason for reason in verdict.reasons)

    def test_a_different_project_is_refused(self, project: Path, tmp_path: Path) -> None:
        task = task_of(session(project))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        verdict = RecoveryService(known_tools=TOOLS, current_workspace=str(elsewhere)).decide(task)

        assert verdict.decision is RecoveryDecision.REQUIRES_REVIEW

    def test_a_session_with_no_plan_is_refused(self, project: Path) -> None:
        task = Task(id="sess1", objective="nothing", workspace=str(project))

        assert decide_for(project, task).decision is RecoveryDecision.NOT_RESUMABLE

    def test_a_checkpoint_from_another_session_is_refused(self, project: Path) -> None:
        task = task_of(session(project))
        plan = task.plan
        assert plan is not None
        task.checkpoints.append(Checkpoint(session_id="other", plan_digest=plan.digest).to_dict())

        verdict = decide_for(project, task)

        assert verdict.decision is RecoveryDecision.NOT_RESUMABLE
        assert any("different session" in reason for reason in verdict.reasons)


class TestCheckpointsAreWrittenAtRealBoundaries:
    def test_a_completed_step_produces_a_checkpoint(self, project: Path) -> None:
        service = session(project)
        task = task_of(service)

        runner_of(service).advance()

        assert task.checkpoints
        assert "s0" in task.checkpoints[-1]["completed_steps"]

    def test_a_checkpoint_is_written_after_the_outcome(self, project: Path) -> None:
        """A checkpoint claiming a step ran before it did loses work on resume."""

        service = session(project, plan_with("a.py", "b.py"))
        task = task_of(service)

        runner_of(service).advance()

        assert task.checkpoints[-1]["completed_steps"] == ["s0"]
        assert task.status_of("s0") is StepStatus.SUCCEEDED

    def test_a_failed_step_is_not_recorded_as_complete(self, project: Path) -> None:
        class Failing(Registry):
            def execute(self, tool: str, arguments: dict[str, Any]) -> Any:
                self.calls.append(tool)
                return _Result(ok=False, summary="no", error="it failed")

        service = session(project)
        task = task_of(service)
        runner_of(service)._registry = Failing()

        runner_of(service).advance()

        completed = task.checkpoints[-1]["completed_steps"] if task.checkpoints else ()
        assert "s0" not in completed

    def test_the_checkpoint_is_a_trace_event(self, project: Path) -> None:
        service = session(project)
        task = task_of(service)

        runner_of(service).advance()

        assert any(event.kind is TraceKind.CHECKPOINT for event in task.trace)

    def test_a_checkpoint_round_trips(self, project: Path) -> None:
        service = session(project)
        task = task_of(service)
        runner_of(service).advance()

        mark = latest_checkpoint(task)

        assert mark is not None
        assert mark.session_id == task.id
        assert mark.schema_version == CHECKPOINT_SCHEMA_VERSION

    def test_a_corrupt_checkpoint_reads_as_no_boundary(self, project: Path) -> None:
        task = task_of(session(project))
        task.checkpoints.append({"schema_version": 9999})

        assert latest_checkpoint(task) is None

    def test_an_unreadable_entry_is_skipped_not_fatal(self, project: Path) -> None:
        task = task_of(session(project))
        task.checkpoints.append("not a checkpoint")

        assert latest_checkpoint(task) is None


class TestCompletedWorkIsNotRepeated:
    def test_a_finished_step_is_not_eligible_again(self, project: Path) -> None:
        service = session(project, plan_with("a.py", "b.py"))

        runner_of(service).advance()
        following = runner_of(service).eligible_step()

        assert following is not None
        assert following.id == "s1"

    def test_resuming_does_not_re_run_the_finished_step(self, project: Path) -> None:
        registry = Registry()
        service = session(project, plan_with("a.py", "b.py"))
        runner_of(service)._registry = registry
        runner_of(service).advance()
        assert registry.calls == ["write_file"]

        task = task_of(service)
        # The process went away without cancelling: the record survives, the
        # manager that owned it does not.
        service._orchestrator = None
        service.resume(task, Workspace(project))
        runner_of(service)._registry = registry
        runner_of(service).advance()

        assert task.status_of("s1") is StepStatus.SUCCEEDED
        assert registry.calls == ["write_file", "write_file"], "a step ran more than once"


class TestResumeGoesThroughTheService:
    def test_a_refused_resume_raises_with_the_reason(self, project: Path) -> None:
        service = session(project)
        task = task_of(service)
        task.set_status("s0", StepStatus.RUNNING)

        with pytest.raises(RecoveryRefused) as caught:
            service.resume(task, Workspace(project))

        assert caught.value.verdict.decision is RecoveryDecision.REQUIRES_REVIEW
        assert caught.value.verdict.reasons

    def test_a_safe_resume_is_allowed_and_recorded(self, project: Path) -> None:
        service = session(project)
        task = task_of(service)

        resumed = service.resume(task, Workspace(project))

        assert resumed is service.orchestrator
        assert any(event.kind is TraceKind.RECOVERY for event in task.trace)

    def test_two_resumes_cannot_leave_two_managers(self, project: Path) -> None:
        """Adopting again releases the previous one rather than orphaning it."""

        service = session(project)
        task = task_of(service)
        first = service.resume(task, Workspace(project))

        second = service.resume(task, Workspace(project))

        assert first is not None
        assert second is service.orchestrator
        assert service.orchestrator is first
