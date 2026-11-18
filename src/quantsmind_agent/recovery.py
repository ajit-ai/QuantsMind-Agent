"""Reconciling a task that was interrupted, and asking rather than guessing.

An application can be closed between two actions, and the interesting cases are
the ones where the record and the world disagree:

* a step was recorded as **running** when the process stopped -- it may have
  finished, or done half of what it said, or nothing at all;
* a file's content no longer matches the hash recorded for it -- somebody edited
  it, the agent edited it twice, or a write was interrupted;
* the workspace is gone, or has become a different project;
* the plan moved while the task was away, so the approval no longer matches.

Every one of those becomes a :class:`~quantsmind_agent.services.tasks.models.RecoveryDecision`
with the consequences spelled out, and the task stops in ``PAUSED`` until the
user answers. What recovery will not do is replay the last tool call. A command
that may have already installed a dependency, written a file or pushed a branch
is not something to re-run "to be sure": the failure mode of replaying is a
second side effect the user never asked for.

What recovery *does* do automatically, because there is no judgement involved:
re-validate the workspace, re-validate the approval against the plan digest,
preserve the budgets, and mark an interrupted step as needing a decision.
"""

from __future__ import annotations

import logging
from pathlib import Path

from quantsmind_agent.services.tasks.models import (
    ApprovalRecord,
    RecoveryDecision,
    StepStatus,
    Task,
    TaskStatus,
    plan_digest,
)
from quantsmind_agent.services.tasks.orchestrator import hash_file

__all__ = ["RecoveryReport", "analyse_task"]

logger = logging.getLogger(__name__)


class RecoveryReport:
    """What recovery found, and what it is waiting for."""

    __slots__ = ("approval_ok", "decisions", "task", "workspace_ok")

    def __init__(
        self,
        task: Task,
        decisions: list[RecoveryDecision],
        *,
        workspace_ok: bool,
        approval_ok: bool,
    ) -> None:
        self.task = task
        self.decisions = decisions
        self.workspace_ok = workspace_ok
        self.approval_ok = approval_ok

    @property
    def resumable(self) -> bool:
        """Whether execution may continue without the user deciding anything.

        True only when nothing is uncertain: the workspace is still the same
        project, every recorded file matches, and the approval still matches the
        plan. Anything else means a question first.
        """

        return self.workspace_ok and self.approval_ok and not self.decisions

    @property
    def pending(self) -> bool:
        return bool(self.decisions)


def analyse_task(
    task: Task,
    *,
    workspace_root: str | None = None,
    current_workspace: str | None = None,
) -> RecoveryReport:
    """Compare a stored task against the world, and list what needs a decision."""

    decisions: list[RecoveryDecision] = []
    root = Path(workspace_root or task.workspace)
    target = current_workspace or task.workspace

    workspace_ok = root.is_dir() and str(root) == str(target)
    if not root.is_dir():
        decisions.append(
            RecoveryDecision(
                kind="workspace_missing",
                subject=str(root),
                detail="the project folder this task ran in is not available",
                options=("stop", "continue without file checks"),
            )
        )
    elif str(root) != str(target):
        decisions.append(
            RecoveryDecision(
                kind="workspace_changed",
                subject=str(root),
                detail=f"the task belongs to {root} but {target} is open",
                options=("stop", "continue against the other project"),
            )
        )

    for step_id, status in task.step_status.items():
        if status is StepStatus.RUNNING:
            decisions.append(
                RecoveryDecision(
                    kind="interrupted_step",
                    subject=step_id,
                    detail=(
                        f"step {step_id!r} was in progress when the application stopped; "
                        "it may have finished, partially run, or not started"
                    ),
                    options=("mark done and continue", "re-run this step", "stop"),
                )
            )

    for change in task.file_changes:
        path = root / change.path
        exists = path.is_file()
        if not exists and change.exists_after:
            decisions.append(
                RecoveryDecision(
                    kind="file_missing",
                    subject=change.path,
                    detail="a file this task changed is no longer present",
                    options=("recreate from the task", "leave it", "stop"),
                )
            )
            continue
        if exists and change.after_hash:
            current = hash_file(path)
            if current and current != change.after_hash:
                decisions.append(
                    RecoveryDecision(
                        kind="file_changed_since",
                        subject=change.path,
                        detail=(
                            f"{change.path} no longer matches what this task recorded; "
                            "it was edited afterwards, by the agent or by you"
                        ),
                        options=("keep the current content", "show a diff and decide", "stop"),
                    )
                )

    for execution in task.executions:
        if execution.tool == "run_command" and not execution.ok and not execution.error:
            decisions.append(
                RecoveryDecision(
                    kind="uncertain_command",
                    subject=execution.step_id,
                    detail=(
                        "a command was interrupted before its outcome was recorded; "
                        "its effect on the machine is unknown"
                    ),
                    options=("re-run knowingly", "assume no effect", "stop"),
                )
            )

    approval_ok = _approval_still_valid(task)
    if task.approval is not None and not approval_ok:
        decisions.append(
            RecoveryDecision(
                kind="approval_invalid",
                subject=task.approval.plan_digest,
                detail=(
                    "the plan changed after approval, so the earlier approval no "
                    "longer authorises this plan"
                ),
                options=("review the new plan", "stop"),
            )
        )

    task.recovery = [*task.recovery, *decisions] if decisions else list(task.recovery)
    if decisions and task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        task.status = TaskStatus.PAUSED
    report = RecoveryReport(task, decisions, workspace_ok=workspace_ok, approval_ok=approval_ok)
    logger.info(
        "task %s recovery: %d decision(s), workspace_ok=%s approval_ok=%s",
        task.id,
        len(decisions),
        workspace_ok,
        approval_ok,
    )
    return report


def _same_workspace(left: str, right: str) -> bool:
    return (
        left.replace("\\", "/").rstrip("/").lower() == right.replace("\\", "/").rstrip("/").lower()
    )


def _approval_still_valid(task: Task) -> bool:
    """An approval is valid only for the exact plan and workspace it was given for."""

    if task.plan is None or task.approval is None:
        return False
    if task.approval.plan_digest != plan_digest(task.plan):
        return False
    return _same_workspace(task.approval.workspace, task.workspace)


def apply_decision(task: Task, decision: RecoveryDecision, choice: str) -> None:
    """Record the user's answer, and act on the ones that are safe to act on.

    Only "stop" is applied mechanically. The others change what the orchestrator
    is allowed to do next -- whether a step may be re-run, whether the task may
    continue -- and the caller decides that from the answer.
    """

    if choice not in decision.options:
        return
    for index, item in enumerate(task.recovery):
        if item is decision or (item.kind == decision.kind and item.subject == decision.subject):
            task.recovery[index] = RecoveryDecision(
                kind=item.kind,
                subject=item.subject,
                detail=item.detail,
                options=item.options,
                resolved=True,
                choice=choice,
            )
            break
    if choice == "mark done and continue" and decision.kind == "interrupted_step":
        task.set_status(decision.subject, StepStatus.SUCCEEDED)
    elif choice == "re-run this step" and decision.kind == "interrupted_step":
        task.set_status(decision.subject, StepStatus.PENDING)
    if choice == "stop" and not task.terminal:
        task.status = TaskStatus.FAILED


def approval_is_stale(task: Task) -> bool:
    """Whether the stored approval no longer matches the stored plan."""

    return not _approval_still_valid(task)


def renewed_approval(task: Task, *, approved: bool, scope: tuple[str, ...] = ()) -> ApprovalRecord:
    """Build a fresh approval for the task's current plan."""

    plan = task.plan
    if plan is None:
        raise ValueError("the task has no plan to approve")
    return ApprovalRecord(
        plan_digest=plan.digest,
        workspace=task.workspace,
        approved=approved,
        scope_paths=scope,
        reason="renewed after a plan change",
    )
