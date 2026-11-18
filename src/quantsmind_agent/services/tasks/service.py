"""Application services: the layer the presentation talks to.

This module exists because the window was doing the application's work. It built a
:class:`~quantsmind_agent.task_store.TaskStore`, an
:class:`~quantsmind_agent.services.tasks.orchestrator.Orchestrator`, a
:class:`~quantsmind_agent.services.tasks.policy.TaskPolicy` and an approval record itself,
then decided what was authorised. Every one of those is a security decision, and
written inside a widget they could not be tested without a running Qt
application, nor reviewed without reading the whole UI.

The services here own those decisions. They hold no widgets, emit no signals and
import nothing from :mod:`quantsmind_agent.gui`, so a test, a script or a future
CLI drives them identically.

Nothing here duplicates the domain. The services *arrange* the domain objects --
which task is in play, which stored task was interrupted, what the user may do
next -- and delegate the lifecycle itself to
:mod:`~quantsmind_agent.services.tasks.orchestrator`. That is why there is still one planning
path and one state machine.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quantsmind_agent.recovery import RecoveryReport, analyse_task, renewed_approval
from quantsmind_agent.services.tasks.models import (
    Plan,
    StepStatus,
    Task,
    TaskStatus,
    TraceEvent,
    TraceKind,
    can_transition,
)
from quantsmind_agent.services.tasks.orchestrator import Orchestrator, TaskEvent
from quantsmind_agent.services.tasks.policy import TaskPolicy, approval_for_plan
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.sessions import (
    RecoveryService,
    RecoveryVerdict,
    latest_checkpoint,
)
from quantsmind_agent.task_store import TaskStore, TaskSummary

__all__ = [
    "INTERRUPTED_STATUSES",
    "InterruptedTask",
    "RecoveryRefused",
    "TaskService",
    "TaskView",
]

logger = logging.getLogger(__name__)


class RecoveryRefused(RuntimeError):
    """Raised when a resume is asked for and the decision is not "safe".

    Carries the verdict so the interface can explain itself rather than printing
    a bare refusal: the user needs to know whether to fix something, look at the
    history, or accept that the work cannot continue.
    """

    def __init__(self, verdict: RecoveryVerdict) -> None:
        super().__init__(verdict.describe())
        self.verdict = verdict


#: Statuses meaning "this task was mid-flight when the process went away". A task
#: in one of these at startup did not finish, and the honest description of its
#: outcome is unknown -- not failed, and certainly not completed.
INTERRUPTED_STATUSES = (TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.REPAIRING)


@dataclass(frozen=True, slots=True)
class TaskView:
    """What the interface needs to know about the current task.

    Derived from the task record rather than from widget state, so the panel and
    the service cannot disagree about what is true. The ``can_*`` flags are
    advisory for the interface only: the policy re-checks authorisation when a
    step runs, so a stale button cannot execute anything.
    """

    task_id: str
    objective: str
    status: TaskStatus
    step_total: int
    step_done: int
    steps_executed: int
    checks_passed: int
    checks_failed: int
    active_seconds: float
    files_changed: tuple[str, ...]
    can_run: bool
    can_pause: bool
    can_cancel: bool
    interrupted: bool = False
    verification: str = ""
    criteria_verified: int = 0
    criteria_total: int = 0

    @classmethod
    def of(cls, task: Task, *, interrupted: bool = False) -> TaskView:
        """Build a view from a task record."""

        plan = task.plan
        total = len(plan.steps) if plan is not None else 0
        done = sum(
            1
            for status in task.step_status.values()
            if status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED)
        )
        return cls(
            task_id=task.id,
            objective=task.objective,
            status=task.status,
            step_total=total,
            step_done=done,
            steps_executed=task.steps_executed,
            checks_passed=sum(1 for c in task.checks if c.status.value == "passed"),
            checks_failed=sum(1 for c in task.checks if c.status.value == "failed"),
            active_seconds=task.active_seconds,
            files_changed=tuple(change.path for change in task.file_changes),
            can_run=task.status is TaskStatus.APPROVED,
            can_pause=not task.terminal and task.status is not TaskStatus.PAUSED,
            can_cancel=not task.terminal,
            interrupted=interrupted,
            verification=(task.verification.outcome.value if task.verification is not None else ""),
            criteria_verified=(
                task.verification.criteria_verified if task.verification is not None else 0
            ),
            criteria_total=(
                task.verification.criteria_total if task.verification is not None else 0
            ),
        )

    def describe(self) -> str:
        """One honest line, for the status bar and for logs.

        Execution and verification are reported separately on purpose: "steps
        done" and "verified" are different claims, and joining them into one
        status is how an unverified task ends up described as a success.
        """

        if self.interrupted:
            return f"{self.objective} -- interrupted, outcome unknown"
        parts = [self.objective, self.status.value]
        if self.step_total:
            parts.append(f"{self.step_done}/{self.step_total} steps")
        if self.criteria_total:
            verdict = self.verification or "not verified"
            parts.append(f"verification: {verdict}")
        elif self.checks_failed:
            parts.append(f"{self.checks_failed} check(s) failed")
        return " -- ".join(parts)


@dataclass(frozen=True, slots=True)
class InterruptedTask:
    """A task that was in flight when the application last closed."""

    task: Task
    report: RecoveryReport

    @property
    def needs_decision(self) -> bool:
        """Whether the user must answer something before anything continues."""

        return self.report.pending or not self.report.approval_ok

    def describe(self) -> str:
        if self.needs_decision:
            reasons = "; ".join(decision.detail for decision in self.report.decisions)
            return f"{self.task.objective} -- interrupted: {reasons or 'needs re-approval'}"
        return f"{self.task.objective} -- interrupted, nothing uncertain"


class TaskService:
    """The task lifecycle, from the user's point of view.

    One instance per application. It owns the store and builds orchestrators, so
    "which task is in play", "was that task interrupted" and "may this run" are
    answered in one place instead of five.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        registry_factory: Callable[[Workspace], Any],
        observer: Callable[[TaskEvent], None] | None = None,
        clock: Callable[[], float] | None = None,
        store: TaskStore | None = None,
    ) -> None:
        self._store = store if store is not None else TaskStore(Path(data_dir) / "tasks")
        self._registry_factory = registry_factory
        self._observer = observer
        self._clock = clock
        self._orchestrator: Orchestrator | None = None

    # -- current task ------------------------------------------------------
    @property
    def orchestrator(self) -> Orchestrator | None:
        """The orchestrator for the task in play, if there is one."""

        return self._orchestrator

    @property
    def store(self) -> TaskStore:
        return self._store

    def view(self, *, interrupted: bool = False) -> TaskView | None:
        """What the interface should display for the current task."""

        if self._orchestrator is None:
            return None
        return TaskView.of(self._orchestrator.task, interrupted=interrupted)

    # -- planning ----------------------------------------------------------
    def propose(
        self, plan: Plan, workspace: Workspace, *, snapshot: Any | None = None
    ) -> Orchestrator:
        """Validate a plan, attach it to a task, and wait for approval.

        Planning happens in the conversation; this is the single door a validated
        plan comes through, so there is one path from "the model proposed this" to
        "the user may approve this".

        ``snapshot`` is the bounded workspace description the plan was proposed
        against, when one is available. It becomes part of the task's evidence;
        it never becomes part of its authorisation.
        """

        task = Task(
            id=uuid.uuid4().hex[:12],
            objective=plan.objective,
            workspace=str(workspace.root),
        )
        if snapshot is not None:
            if hasattr(snapshot, "to_dict"):
                task.workspace_snapshot = dict(snapshot.to_dict())
            elif isinstance(snapshot, dict):
                task.workspace_snapshot = dict(snapshot)
        orchestrator = self._build(task, workspace)
        orchestrator.submit_plan(plan)
        self._orchestrator = orchestrator
        return orchestrator

    def decide(self, approved: bool, *, reason: str = "") -> Orchestrator:
        """Record the user's decision about the plan awaiting approval."""

        orchestrator = self._require()
        task = orchestrator.task
        plan = task.plan
        if plan is None:
            raise RuntimeError("there is no plan to decide about")
        orchestrator.approve(
            approval_for_plan(
                plan,
                task.workspace,
                approved=approved,
                reason=reason or ("approved by the user" if approved else "rejected by the user"),
            )
        )
        return orchestrator

    def approve_with_scope(self, scope: tuple[str, ...]) -> Orchestrator:
        """Approve the current plan, narrowed to ``scope``.

        The scope travels with the approval, so the policy refuses any later step
        reaching outside it without consulting the plan again.
        """

        orchestrator = self._require()
        task = orchestrator.task
        plan = task.plan
        if plan is None:
            raise RuntimeError("there is no plan to approve")
        orchestrator.approve(approval_for_plan(plan, task.workspace, approved=True, scope=scope))
        return orchestrator

    # -- execution ---------------------------------------------------------
    def release(self) -> None:
        """Stop and forget the current task, leaving its record on disk."""

        if self._orchestrator is not None:
            if not self._orchestrator.task.terminal:
                self._orchestrator.cancel("the task was released")
            self._orchestrator = None

    def attach_observer(self, observer: Callable[[TaskEvent], None] | None) -> None:
        """Route task events to ``observer``, whoever owns the worker thread."""

        if self._orchestrator is not None:
            self._orchestrator.set_observer(observer)

    # -- history and recovery ---------------------------------------------
    def history(self, limit: int = 50) -> list[TaskSummary]:
        """Past and present tasks, newest first."""

        return list(self._store.list()[:limit])

    def interrupted_tasks(self, workspace: Workspace | None = None) -> list[InterruptedTask]:
        """Tasks that were in flight when the application last closed.

        These are *not* marked failed and *not* resumed. A task interrupted after a
        tool call may already have changed a file, so its outcome is genuinely
        unknown; the honest report is "this was interrupted, here is what it had
        done", and continuing is a decision the user makes.
        """

        current_root = str(workspace.root) if workspace is not None else None
        found: list[InterruptedTask] = []
        for summary in self._store.list():
            task = self._store.load(summary.id)
            if task is None or task.status not in INTERRUPTED_STATUSES:
                continue
            report = analyse_task(
                task,
                workspace_root=task.workspace,
                current_workspace=current_root or task.workspace,
            )
            found.append(InterruptedTask(task=task, report=report))
        return found

    # -- recovery ----------------------------------------------------------
    def recovery_service(self, workspace: Workspace | None = None) -> RecoveryService:
        """A recovery decision service bound to the world as it is now.

        Built per call rather than once, because the tools and the open project
        are exactly the facts that decide the answer, and both can change between
        one question and the next.
        """

        registry = self._registry_factory(workspace) if workspace is not None else None
        names = set(getattr(registry, "names", ()) or ())
        return RecoveryService(
            known_tools=frozenset(names),
            current_workspace=str(workspace.root) if workspace is not None else "",
        )

    def recovery_verdict(self, task: Task, workspace: Workspace | None = None) -> RecoveryVerdict:
        """Whether a stored task may continue, and why."""

        return self.recovery_service(workspace).decide(task, checkpoint=latest_checkpoint(task))

    def resume(self, task: Task, workspace: Workspace) -> Orchestrator:
        """Continue an interrupted task, but only when that has been decided.

        The verdict is recomputed here rather than trusted from the caller: the
        tools, the workspace or the plan may have changed between asking and doing,
        and a resume is exactly where an optimistic assumption costs the user a
        repeated side effect.
        """

        verdict = self.recovery_verdict(task, workspace)
        if not verdict.resumable:
            # A refusal is evidence too. Keep it on the record even though there
            # is no orchestrator yet, so the next person asking why is not met
            # with a task that looks as though nobody tried.
            now = self._clock() if self._clock is not None else (task.updated_at or time.time())
            task.record(
                TraceEvent(
                    TraceKind.RECOVERY,
                    now,
                    verdict.describe(),
                    decision=str(verdict.decision),
                )
            )
            task.updated_at = now
            self._store.save(task)
            raise RecoveryRefused(verdict)

        orchestrator = self.adopt(task, workspace)
        # A crash can leave the record claiming it is still running. Move it
        # through the legal stopped state before approving it again, so the
        # timeline shows that the interruption happened instead of pretending it
        # did not. A task already at a stopped or approved boundary is left as it
        # is, because moving it would invent an event.
        if task.status is TaskStatus.RUNNING:
            task.move_to(TaskStatus.PAUSED, detail="the session was interrupted")
        if task.status is TaskStatus.PAUSED:
            task.move_to(
                TaskStatus.APPROVED
                if task.approved_plan is not None
                else TaskStatus.AWAITING_APPROVAL,
                detail="continuing from a checkpoint",
            )
        orchestrator.emit(
            "resumed_after_review" if task.approval is not None else "resumed",
            verdict.describe(),
            decision=str(verdict.decision),
        )
        orchestrator.persist()
        return orchestrator

    def adopt(self, task: Task, workspace: Workspace) -> Orchestrator:
        """Bring a stored task back under management, *without* resuming it.

        Adopting is not resuming: the caller gets an orchestrator to inspect and
        decide about. Execution continues only through the approval path, which an
        interrupted task has not been given.

        Adopting a task that is already under management returns the manager that
        is already there. Building a second one would leave two objects driving
        the same record -- which is how a resume starts an execution nobody
        intended.
        """

        existing = self._orchestrator
        if existing is not None and existing.task.id == task.id:
            existing.task = task
            return existing
        orchestrator = self._build(task, workspace)
        self._orchestrator = orchestrator
        return orchestrator

    def renew_approval(
        self, *, approved: bool = True, scope: tuple[str, ...] | None = None
    ) -> Orchestrator:
        """Re-authorise an interrupted task, bound to its *current* plan.

        Only called after an explicit user decision. The record is rebuilt from
        the plan as stored now, so it cannot carry authority for a plan that
        changed while the application was closed.
        """

        orchestrator = self._require()
        task = orchestrator.task
        if task.plan is None:
            raise RuntimeError("there is no plan to re-approve")
        existing = task.approval.scope_paths if task.approval is not None else ()
        task.approval = renewed_approval(
            task, approved=approved, scope=scope if scope is not None else existing
        )
        # Re-authorising an interrupted task stops it first and then approves it,
        # in two legal moves. There is no RUNNING -> APPROVED edge, and there
        # should not be: a task that was mid-flight when the process died must
        # pass through a stopped state before anything runs again, so the record
        # shows a pause rather than pretending execution never stopped.
        if not task.terminal and task.status is not TaskStatus.PAUSED:
            task.move_to(TaskStatus.PAUSED)
        if approved and can_transition(task.status, TaskStatus.APPROVED):
            task.move_to(TaskStatus.APPROVED)
        self._store.save(task)
        return orchestrator

    # -- helpers -----------------------------------------------------------
    def _require(self) -> Orchestrator:
        if self._orchestrator is None:
            raise RuntimeError("there is no task in play")
        return self._orchestrator

    def _build(self, task: Task, workspace: Workspace) -> Orchestrator:
        """Build the orchestrator that owns ``task``.

        This is the only place an orchestrator is constructed for the product, so
        the registry, the policy, the store and the event observer are assembled
        once instead of once per widget.
        """

        return Orchestrator(
            task,
            registry=self._registry_factory(workspace),
            policy=TaskPolicy(workspace=workspace),
            store=self._store,
            workspace=workspace,
            clock=self._clock or time.time,
            observer=self._observer,
        )
