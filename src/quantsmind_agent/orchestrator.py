"""The orchestrator: the only thing that decides what happens next.

Everything about v0.4's safety reduces to a property of this class. The model
proposes plans and repairs; :class:`Orchestrator` decides whether a plan is
valid, whether an operation is permitted, whether a step is eligible now, whether
the budget allows another attempt, and whether the task is done. A model cannot
set a step's status, cannot extend its own approval, cannot skip a failed check,
and cannot talk the orchestrator into running a tool that is not in the approved
plan.

The execution model is deliberately *stepwise* rather than "run until done":
:meth:`Orchestrator.advance` performs at most one action and returns what it did,
so a GUI can drive it from a worker thread, a test can drive it step by step, and
an interruption leaves a task in a state the next call can reason about. There is
no internal loop that could run to completion while the user watches a frozen
window.

Three rules shape the transitions:

* **Approval precedes effect.** A task leaves ``AWAITING_APPROVAL`` only with a
  record bound to the current plan digest. Change the plan and the approval stops
  matching, so the task returns to awaiting approval rather than continuing.
* **Budgets are state, not settings.** Steps executed, checks run, repairs spent
  and active seconds live on the task and are persisted, so a restart continues
  the count instead of handing out a fresh allowance.
* **Nothing is reported that was not recorded.** :meth:`Orchestrator.report`
  counts completed steps from step statuses and passed checks from check
  results, and ``unverified`` carries everything that could not run.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quantsmind_agent.task_policy import PolicyVerdict, TaskPolicy, classify
from quantsmind_agent.tasks import (
    AcceptanceCriterion,
    ApprovalRecord,
    CheckResult,
    CheckStatus,
    FileChangeRecord,
    Plan,
    PlannedStep,
    PlanRejected,
    RepairAttempt,
    RiskLevel,
    StepStatus,
    Task,
    TaskReport,
    TaskStatus,
    ToolExecutionRecord,
    validate_plan,
)
from quantsmind_agent.verification import Check, run_checks

__all__ = ["AdvanceResult", "Orchestrator", "StepOutcome", "TaskEvent", "hash_file"]

logger = logging.getLogger(__name__)


def hash_file(path: Path) -> str:
    """SHA-256 of a file's bytes, or an empty string when it does not exist."""

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What executing one step actually did."""

    step_id: str
    executed: bool
    ok: bool = False
    reason: str = ""
    record: ToolExecutionRecord | None = None
    changes: tuple[FileChangeRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    """The outcome of one :meth:`Orchestrator.advance` call."""

    action: str
    status: TaskStatus
    step_id: str = ""
    ok: bool = False
    reason: str = ""
    verdict: PolicyVerdict | None = None


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """A progress event for the interface, carrying no model-authored text."""

    kind: str
    task_id: str
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


Observer = Callable[[TaskEvent], None]


# __PART2__


class Orchestrator:
    """Drives one task through its lifecycle.

    Not thread-safe by itself: it is owned by whichever thread is running the
    task, and the GUI moves that onto a worker. Everything it mutates is
    persisted through the store, so losing the thread loses no evidence.
    """

    def __init__(
        self,
        task: Task,
        *,
        registry: Any,
        policy: TaskPolicy,
        store: Any | None = None,
        workspace: Any | None = None,
        clock: Callable[[], float] = time.time,
        observer: Observer | None = None,
    ) -> None:
        self.task = task
        self._registry = registry
        self._policy = policy
        self._store = store
        self._workspace = workspace
        self._clock = clock
        self._observer = observer
        self._cancel_requested = False
        self._paused = False

    @property
    def task_id(self) -> str:
        return self.task.id

    def emit(self, kind: str, detail: str = "", **payload: Any) -> None:
        if self._observer is not None:
            self._observer(
                TaskEvent(kind=kind, task_id=self.task.id, detail=detail, payload=payload)
            )

    def set_observer(self, observer: Observer | None) -> None:
        """Attach or replace the progress observer.

        Public because the GUI attaches its worker's emitter here rather than
        reaching into a private attribute: the worker thread must be the thing
        that receives events, and a worker thread cannot be bolted on afterwards.
        """

        self._observer = observer

    def persist(self) -> None:
        """Write the task now, so an interruption loses at most one action."""

        if self._store is None:
            return
        self.task.updated_at = self._clock()
        self._store.save(self.task)

    # -- lifecycle --------------------------------------------------------
    def submit_plan(self, plan: Plan) -> Plan:
        """Validate and attach a proposed plan, then wait for approval.

        A rejected plan never becomes the task's plan: the reasons go back to the
        caller and the task keeps whatever it had, with nothing approved.
        """

        validated = validate_plan(
            plan,
            known_tools=self._tool_names(),
            workspace=self.task.workspace,
            max_steps=self.task.budget.max_steps,
        )
        self.task.plan = validated
        self.task.status = TaskStatus.AWAITING_APPROVAL
        for step in validated.steps:
            self.task.step_status.setdefault(step.id, StepStatus.PENDING)
        self.emit("plan_ready", validated.objective, steps=len(validated.steps))
        self.persist()
        return validated

    def approve(self, approval: ApprovalRecord) -> None:
        """Record the user's decision about the current plan."""

        plan = self.task.plan
        if plan is None:
            raise PlanRejected(["there is no plan to approve"])
        if approval.plan_digest != plan.digest:
            raise PlanRejected(["that approval was granted for a different plan"])
        self.task.approval = approval
        self.task.status = TaskStatus.APPROVED if approval.approved else TaskStatus.CANCELLED
        self.emit(
            "approved" if approval.approved else "rejected",
            approval.reason or "the user decided",
            scope=list(approval.scope_paths),
        )
        self.persist()

    def cancel(self, reason: str = "") -> None:
        """Stop the task. No further action is taken after this."""

        self._cancel_requested = True
        self.task.status = TaskStatus.CANCELLED
        self.emit("cancelled", reason)
        self.persist()

    def pause(self) -> None:
        """Pause between actions, without losing the step in flight."""

        if self.task.terminal:
            return
        self._paused = True
        self.task.status = TaskStatus.PAUSED
        self.emit("paused", "")
        self.persist()

    def resume(self) -> None:
        """Resume a paused task whose approval still matches its plan."""

        if self.task.status is not TaskStatus.PAUSED:
            return
        if self.task.approved_plan is None:
            self.task.status = TaskStatus.AWAITING_APPROVAL
            self.emit("approval_required", "the plan changed while the task was paused")
        else:
            self._paused = False
            self.task.status = TaskStatus.APPROVED
            self.emit("resumed", "")
        self.persist()

    @property
    def cancelled(self) -> bool:
        return self._cancel_requested

    @property
    def paused(self) -> bool:
        return self._paused

    # -- scheduling -------------------------------------------------------
    def eligible_step(self) -> PlannedStep | None:
        """The next step that may run, or ``None``.

        Eligibility is dependency order, status and budget together. A step whose
        dependency failed is skipped rather than run: executing it would operate
        on state the failed step never produced.
        """

        plan = self.task.approved_plan
        if plan is None:
            return None
        if self.task.steps_executed >= self.task.budget.max_steps:
            return None
        failed = {
            step_id
            for step_id, status in self.task.step_status.items()
            if status in (StepStatus.FAILED, StepStatus.BLOCKED)
        }
        for step in plan.steps:
            status = self.task.status_of(step.id)
            # FAILED and BLOCKED are terminal until something changes them. A
            # failed step that stays "eligible" would simply run again until the
            # budget ran out, which is unbounded retry wearing a budget's
            # clothes: retrying is the repair controller's decision, with the
            # failure evidence in hand.
            if status in (
                StepStatus.SUCCEEDED,
                StepStatus.SKIPPED,
                StepStatus.RUNNING,
                StepStatus.FAILED,
                StepStatus.BLOCKED,
            ):
                continue
            if any(dependency in failed for dependency in step.depends_on):
                self.task.set_status(step.id, StepStatus.SKIPPED)
                continue
            if all(
                self.task.status_of(dependency) is StepStatus.SUCCEEDED
                for dependency in step.depends_on
            ):
                self.task.set_status(step.id, StepStatus.READY)
                return step
        return None

    # __PART3__

    def advance(self, *, extra_approval: ApprovalRecord | None = None) -> AdvanceResult:
        """Perform at most one action and return what happened.

        Never raises for an expected refusal: a denied step is an outcome the
        caller needs to see, not an exception. Unexpected exceptions become a
        failed step with the message attached, because a task that cannot continue
        should say why rather than vanish.
        """

        if self._cancel_requested or self.task.status is TaskStatus.CANCELLED:
            return AdvanceResult("cancelled", self.task.status, reason="cancelled by the user")
        if self._paused or self.task.status is TaskStatus.PAUSED:
            return AdvanceResult("paused", self.task.status, reason="paused")
        if self.task.terminal:
            return AdvanceResult("finished", self.task.status, reason=self.task.status.value)

        step = self.eligible_step()
        if step is None:
            return self._settle()

        verdict = self._policy.evaluate(
            self.task, step, extra_approval=extra_approval, workspace_root=self.task.workspace
        )
        if not verdict.allowed:
            self.task.set_status(step.id, StepStatus.BLOCKED)
            self.task.status = TaskStatus.BLOCKED
            self.emit("blocked", verdict.reason, step=step.id)
            self.persist()
            return AdvanceResult(
                "blocked", self.task.status, step_id=step.id, reason=verdict.reason, verdict=verdict
            )

        outcome = self.execute(step)
        return AdvanceResult(
            "step", self.task.status, step_id=step.id, ok=outcome.ok, reason=outcome.reason
        )

    def execute(self, step: PlannedStep) -> StepOutcome:
        """Run one already-authorised step and record exactly what happened."""

        started = self._clock()
        self.task.set_status(step.id, StepStatus.RUNNING)
        self.task.status = TaskStatus.RUNNING
        before = self._snapshot(step)

        try:
            result = self._registry.execute(step.tool, dict(step.arguments))
        except Exception as exc:
            logger.warning("task %s step %s raised: %s", self.task.id, step.id, exc)
            record = ToolExecutionRecord(
                step_id=step.id,
                tool=step.tool,
                arguments=dict(step.arguments),
                ok=False,
                error=str(exc),
                started_at=started,
            )
            self.task.executions.append(record)
            self.task.set_status(step.id, StepStatus.FAILED)
            self.task.steps_executed += 1
            self.emit("step_failed", str(exc), step=step.id)
            self.persist()
            return StepOutcome(step.id, executed=True, ok=False, reason=str(exc), record=record)

        record = ToolExecutionRecord(
            step_id=step.id,
            tool=step.tool,
            arguments=dict(step.arguments),
            ok=bool(result.ok),
            summary=str(result.summary or "")[:2000],
            error="" if result.ok else str(result.error or "")[:2000],
            started_at=started,
            duration_ms=int((self._clock() - started) * 1000),
        )
        self.task.executions.append(record)
        self.task.steps_executed += 1
        self.task.active_seconds += max(0.0, self._clock() - started)

        changes = self._changes_since(step, before)
        self.task.file_changes.extend(changes)
        self.task.set_status(step.id, StepStatus.SUCCEEDED if result.ok else StepStatus.FAILED)
        self.emit(
            "step_succeeded" if result.ok else "step_failed",
            record.summary or record.error,
            step=step.id,
            changed=[item.path for item in changes],
        )
        self.persist()
        return StepOutcome(
            step.id,
            executed=True,
            ok=bool(result.ok),
            reason="" if result.ok else record.error,
            record=record,
            changes=tuple(changes),
        )

    # -- verification and repair ------------------------------------------
    def verify(
        self, checks: list[Check], *, timeout_seconds: int = 300
    ) -> list[CheckResult]:
        """Run the project's declared checks and record every outcome.

        A check that could not run is recorded as skipped, blocked or not run --
        never as a pass. The report is built from these records, so a missing
        tool shows up as unverified rather than as success.
        """

        if self.task.checks_run >= self.task.budget.max_checks_per_task:
            self.emit("budget_exhausted", "verification budget spent", resource="checks")
            return []
        self.task.status = TaskStatus.VERIFYING
        results = run_checks(
            checks,
            workspace_root=self.task.workspace,
            on_start=lambda check: self.emit("check_started", check.description or check.id),
            timeout_seconds=timeout_seconds,
        )
        for result in results:
            self.task.checks.append(result)
            self.task.checks_run += 1
            self.emit(
                "check_finished",
                f"{result.check_id}: {result.status.value}",
                exit_code=result.exit_code,
            )
        self.persist()
        return results

    def repair(self, step: PlannedStep, plan: Plan, *, rationale: str = "") -> RepairAttempt:
        """Admit a proposed repair, within budget and within the original scope.

        A repair that changes the objective, adds steps outside the original plan,
        or exceeds the retry budget is recorded and refused. The point of the
        bound is that a failing test cannot become an excuse to rewrite the task.
        """

        spent = sum(1 for item in self.task.repairs if item.step_id == step.id)
        if spent >= self.task.budget.max_repairs_per_step:
            attempt = RepairAttempt(
                step_id=step.id,
                attempt=spent,
                plan=plan,
                status=StepStatus.BLOCKED,
                rationale="repair budget exhausted",
            )
            self.task.repairs.append(attempt)
            self.task.status = TaskStatus.BLOCKED
            self.emit("repair_refused", "the repair budget for this step is spent", step=step.id)
            self.persist()
            return attempt

        original = self.task.approved_plan
        added = [item.id for item in plan.steps if original and original.step(item.id) is None]
        if added:
            attempt = RepairAttempt(
                step_id=step.id,
                attempt=spent,
                plan=plan,
                status=StepStatus.BLOCKED,
                rationale=f"repair introduces steps outside the approved plan: {added}",
            )
            self.task.repairs.append(attempt)
            self.task.status = TaskStatus.BLOCKED
            self.emit(
                "repair_refused",
                f"the repair adds steps that were not approved: {added}",
                step=step.id,
            )
            self.persist()
            return attempt

        attempt = RepairAttempt(
            step_id=step.id,
            attempt=spent + 1,
            plan=plan,
            status=StepStatus.PENDING,
            rationale=rationale,
        )
        self.task.repairs.append(attempt)
        self.task.status = TaskStatus.REPAIRING
        self.task.set_status(step.id, StepStatus.PENDING)
        self.emit("repair_admitted", rationale, step=step.id, attempt=attempt.attempt)
        self.persist()
        return attempt

    def complete_repair(self, attempt: RepairAttempt, ok: bool, detail: str = "") -> None:
        """Record the outcome of a repair attempt."""

        for index, item in enumerate(self.task.repairs):
            if item is attempt or (
                item.step_id == attempt.step_id and item.attempt == attempt.attempt
            ):
                self.task.repairs[index] = RepairAttempt(
                    step_id=item.step_id,
                    attempt=item.attempt,
                    plan=item.plan,
                    status=StepStatus.SUCCEEDED if ok else StepStatus.FAILED,
                    rationale=item.rationale or detail,
                    approved=item.approved,
                )
                break
        self.task.status = TaskStatus.VERIFYING if ok else TaskStatus.FAILED
        self.emit("repair_finished", detail, ok=ok, attempt=attempt.attempt)
        self.persist()

    # __PART4__

    # -- completion -------------------------------------------------------
    def report(self) -> TaskReport:
        """Build the final report from recorded evidence only."""

        statuses = list(self.task.step_status.values())
        completed = sum(
            1 for status in statuses if status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED)
        )
        failed = sum(1 for status in statuses if status is StepStatus.FAILED)
        passed = sum(1 for check in self.task.checks if check.status is CheckStatus.PASSED)
        failed_checks = sum(1 for check in self.task.checks if check.status is CheckStatus.FAILED)
        not_run = tuple(
            f"{check.check_id} ({check.status.value})"
            for check in self.task.checks
            if check.status not in (CheckStatus.PASSED, CheckStatus.FAILED, CheckStatus.NOT_RUN)
        )
        blockers: list[str] = []
        if failed:
            blockers.append(f"{failed} step(s) did not succeed")
        if failed_checks:
            blockers.append(f"{failed_checks} check(s) failed")
        if not_run:
            blockers.append(f"{len(not_run)} check(s) could not be run")
        blocked_steps = [
            step_id
            for step_id, status in self.task.step_status.items()
            if status is StepStatus.BLOCKED
        ]
        if blocked_steps:
            blockers.append(f"blocked steps: {', '.join(sorted(blocked_steps))}")

        report = TaskReport(
            task_id=self.task.id,
            status=self.task.status,
            summary=self._summary_text(),
            steps_completed=completed,
            steps_failed=failed,
            files_changed=self.task.changed_paths(),
            checks_passed=passed,
            checks_failed=failed_checks,
            checks_not_run=not_run,
            repairs_attempted=len(self.task.repairs),
            acceptance=tuple(self._acceptance_with_status()),
            blockers=tuple(blockers),
            unverified=not_run,
            duration_ms=int(self.task.active_seconds * 1000),
        )
        self.task.report = report
        self.persist()
        return report

    def _summary_text(self) -> str:
        parts = [f"status {self.task.status.value}"]
        if self.task.changed_paths():
            parts.append(f"{len(self.task.changed_paths())} file(s) changed")
        executed = len(self.task.executions)
        if executed:
            succeeded = sum(1 for item in self.task.executions if item.ok)
            parts.append(f"{succeeded}/{executed} step(s) succeeded")
        passed = sum(1 for check in self.task.checks if check.status is CheckStatus.PASSED)
        if self.task.checks:
            parts.append(f"{passed}/{len(self.task.checks)} check(s) passed")
        return "; ".join(parts)

    def _acceptance_with_status(self) -> list[Any]:
        """Criteria marked by what actually ran, not by what the model claims.

        ``verified`` requires a passing check that names the criterion -- as the
        step it verifies, or as the check id it was wired to. A criterion with no
        such evidence stays unverified, which is the whole point of keeping
        criteria out of the model's reach.
        """

        plan = self.task.plan
        if plan is None:
            return []
        succeeded_steps = {item.step_id for item in self.task.executions if item.ok}
        passing = [check for check in self.task.checks if check.status is CheckStatus.PASSED]
        marked: list[AcceptanceCriterion] = []
        for criterion in plan.acceptance_criteria:
            verified = any(
                check.step_id == criterion.id or check.check_id == criterion.verified_by
                for check in passing
            )
            if verified:
                status = "verified"
            elif criterion.id in succeeded_steps:
                status = "executed"
            else:
                status = "not_run"
            marked.append(
                AcceptanceCriterion(
                    id=criterion.id,
                    description=criterion.description,
                    verified_by=criterion.verified_by,
                    status=status,
                )
            )
        return marked

    def _settle(self) -> AdvanceResult:
        """Nothing eligible: decide whether the task is finished, blocked or failed."""

        plan = self.task.approved_plan
        if plan is None:
            return AdvanceResult("waiting", self.task.status, reason="no approved plan")

        statuses = [self.task.status_of(step.id) for step in plan.steps]
        if any(status is StepStatus.BLOCKED for status in statuses):
            self.task.status = TaskStatus.BLOCKED
            reason = "a step was blocked by policy"
        elif any(status is StepStatus.FAILED for status in statuses):
            self.task.status = TaskStatus.FAILED
            reason = "a step failed"
        elif all(status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED) for status in statuses):
            if self.task.checks_run and any(
                check.status is CheckStatus.FAILED for check in self.task.checks
            ):
                self.task.status = TaskStatus.FAILED
                reason = "verification failed"
            elif self.task.steps_executed >= self.task.budget.max_steps:
                self.task.status = TaskStatus.FAILED
                reason = "the step budget was exhausted"
            else:
                self.task.status = TaskStatus.COMPLETED
                reason = "every step completed"
        else:
            self.task.status = TaskStatus.BLOCKED
            reason = "no step is eligible and none failed"

        self.report()
        self.emit("finished", reason, status=self.task.status.value)
        self.persist()
        return AdvanceResult("finished", self.task.status, reason=reason)

    # -- helpers ----------------------------------------------------------
    def _tool_names(self) -> set[str]:
        try:
            return set(self._registry.names)
        except AttributeError:  # pragma: no cover - a stub registry in tests
            return set()

    def _snapshot(self, step: PlannedStep) -> dict[str, tuple[bool, str, int]]:
        """Record the state of every file this step could change.

        Taken before execution so that "the agent changed this" is a comparison
        against a fact, not an inference from the fact that a write tool ran.
        """

        targets = [
            str(value)
            for value in step.arguments.values()
            if isinstance(value, str) and classify(step.tool, step.arguments).value != "command"
        ]
        targets += list(step.expected_paths)
        state: dict[str, tuple[bool, str, int]] = {}
        for relative in targets:
            resolved = self._resolve(relative)
            if resolved is None:
                continue
            state[relative] = (
                resolved.is_file(),
                hash_file(resolved) if resolved.is_file() else "",
                resolved.stat().st_size if resolved.is_file() else 0,
            )
        return state

    def _changes_since(
        self, step: PlannedStep, before: dict[str, tuple[bool, str, int]]
    ) -> list[FileChangeRecord]:
        """Evidence of what changed, by comparing the filesystem to the snapshot."""

        changes: list[FileChangeRecord] = []
        for relative, (existed, digest, size) in before.items():
            resolved = self._resolve(relative)
            if resolved is None:
                continue
            now_exists = resolved.is_file()
            now_hash = hash_file(resolved) if now_exists else ""
            now_size = resolved.stat().st_size if now_exists else 0
            if existed == now_exists and digest == now_hash:
                continue
            changes.append(
                FileChangeRecord(
                    path=relative,
                    operation="modified" if existed else "created",
                    existed_before=existed,
                    exists_after=now_exists,
                    before_hash=digest,
                    after_hash=now_hash,
                    step_id=step.id,
                    bytes_before=size,
                    bytes_after=now_size,
                )
            )
        return changes

    def _resolve(self, relative: str) -> Path | None:
        if self._workspace is not None:
            try:
                resolved: Path = self._workspace.resolve(relative)
            except Exception:
                return None
            return resolved
        try:
            return (Path(self.task.workspace) / relative).resolve()
        except OSError:  # pragma: no cover - unresolvable path
            return None

    def risk_of(self, step: PlannedStep) -> RiskLevel:
        """The step's declared risk, for the interface to display."""

        return step.risk
