"""Validated, versioned data models for controlled autonomous execution.

v0.3 gave the agent a conversation and a project to reason about. v0.4 adds a
*task*: a bounded piece of work with a plan the user approves, steps the
orchestrator schedules, evidence of what changed, and a verification result the
user can trust or disbelieve.

Three decisions shape this module.

**The model does not own the state machine.** Plans arrive from a language
model, which means they arrive untrusted: a plan can name a tool that does not
exist, a path outside the workspace, a dependency on a step that does not exist,
or a cycle. :func:`validate_plan` rejects all of that *before* anything runs, and
the orchestrator -- not the plan, and not the model -- decides what happens next.
A plan that parses is not a plan that is allowed.

**Approval is bound to a plan version, a workspace and a scope.** Not to a
conversation, not to a session, and not to the model having said something
polite. :class:`ApprovalRecord` carries the plan digest it was granted against,
and :meth:`ApprovalRecord.covers` is what makes a later check fail closed when
the plan has moved on.

**Every model is round-trippable and migratable.** Tasks outlive the process, so
``to_dict``/``from_dict`` pair with an explicit ``SCHEMA_VERSION``; an unreadable
record is quarantined by the store rather than guessed at, and an older one is
migrated rather than discarded.

Persistence lives in :mod:`quantsmind_agent.task_store`, which mirrors the
conversation store. This module holds no I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "AcceptanceCriterion",
    "ApprovalRecord",
    "CheckResult",
    "CheckStatus",
    "FileChangeRecord",
    "Plan",
    "PlanRejected",
    "PlannedStep",
    "RecoveryDecision",
    "RepairAttempt",
    "RiskLevel",
    "StepStatus",
    "Task",
    "TaskBudget",
    "TaskReport",
    "TaskStatus",
    "ToolExecutionRecord",
    "migrate",
    "plan_digest",
    "validate_plan",
]

#: Bumped when a stored task record needs migrating on read.
SCHEMA_VERSION = 1

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class TaskStatus(StrEnum):
    """Lifecycle of a task.

    Transitions are enforced by the orchestrator; this enum only names them.
    ``PAUSED`` is distinct from ``BLOCKED``: a paused task is waiting for the user
    and can be resumed, a blocked task has hit a stop condition and needs a
    decision before anything else happens.
    """

    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    VERIFYING = "verifying"
    REPAIRING = "repairing"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class RiskLevel(StrEnum):
    """How much damage a wrong execution could do."""

    READ_ONLY = "read_only"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def requires_approval(self) -> bool:
        """Whether a step at this risk may not run on plan approval alone."""

        return self in (RiskLevel.MEDIUM, RiskLevel.HIGH)


class CheckStatus(StrEnum):
    """Outcome of one verification check.

    ``SKIPPED`` and ``NOT_RUN`` are separate from ``FAILED`` on purpose: a check
    that could not run is not a check that failed, and reporting it as a failure
    would teach the user to ignore failures.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_RUN = "not_run"
    BLOCKED = "blocked"


class PlanRejected(ValueError):
    """A plan the application refuses to execute, with every reason."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons) or "plan rejected")
        self.reasons = reasons


def _require_id(value: Any, label: str, reasons: list[str]) -> str:
    text = "" if value is None else str(value).strip()
    if not _ID_RE.match(text):
        reasons.append(f"{label} {text!r} is not a valid identifier")
    return text


def _coerce_arguments(value: object) -> dict[str, Any]:
    """Accept only a JSON object as a step's arguments.

    A plan comes from a language model, so ``arguments`` may be a string, a list
    or ``null``. Coercing to an empty object keeps deserialisation total -- the
    alternative is an exception while reading a stored task -- and the step is
    still rejected afterwards for having nothing usable to run.
    """

    return dict(value) if isinstance(value, dict) else {}


def _normalise_workspace(workspace: str) -> str:
    text = str(workspace or "").replace("\\", "/").rstrip("/")
    return text.lower() if len(text) > 1 else text


def _normalise_path(path: str) -> str:
    """Collapse separators and ``.``/``..`` segments for a containment check.

    Without collapsing, an approved scope of ``src`` would "cover"
    ``src/../../etc/passwd`` because the string starts with the scope. This is a
    textual normalisation, not a filesystem resolution -- the workspace does the
    real resolution -- but it has to happen here or a traversal can walk straight
    through a scope check.
    """

    text = str(path or "").replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    absolute = text.startswith("/")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append(part)
            continue
        parts.append(part)
    joined = "/".join(parts)
    return (f"/{joined}" if absolute else joined).rstrip("/")


def _looks_absolute(text: str) -> bool:
    """Whether a plan-supplied path looks absolute, on either separator style."""

    return bool(text) and (
        text.startswith(("/", "\\")) or (len(text) > 2 and text[1] == ":" and text[2] in "\\/")
    )


def _inside_workspace(path: str, workspace: str) -> bool:
    """A textual containment check used at validation time.

    Deliberately conservative and not a security boundary on its own: the real
    boundary is :class:`~quantsmind_agent.workspace.Workspace` at execution time.
    This exists to reject obviously wrong plans before anything runs.
    """

    root = _normalise_workspace(workspace)
    target = _normalise_path(path)
    if _looks_absolute(target):
        absolute = _normalise_workspace(target)
        return absolute == root or absolute.startswith(root + "/")
    return ".." not in target.split("/")


# __PART2__


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    """Something the task must achieve, in the user's terms.

    Criteria belong to the plan rather than to the agent: an agent that can edit
    its own definition of done will always conclude it succeeded.
    """

    id: str
    description: str
    verified_by: str = ""
    status: str = "not_run"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AcceptanceCriterion:
        return cls(
            id=str(payload.get("id", "")),
            description=str(payload.get("description", "")),
            verified_by=str(payload.get("verified_by", "")),
            status=str(payload.get("status", "not_run")),
        )


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """One unit of work the orchestrator can schedule.

    ``tool`` and ``arguments`` name an operation in the *existing* tool registry.
    A step does not say how to run it, and the orchestrator does not care: an
    operation is either a registered tool in this application, or it is not a step
    this application will run.
    """

    id: str
    description: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    expected_paths: tuple[str, ...] = ()
    expected_command: str = ""
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "depends_on": list(self.depends_on),
            "risk": str(self.risk),
            "expected_paths": list(self.expected_paths),
            "expected_command": self.expected_command,
            "optional": self.optional,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PlannedStep:
        known_risks = {level.value for level in RiskLevel}
        risk = str(payload.get("risk", RiskLevel.LOW))
        return cls(
            id=str(payload.get("id", "")),
            description=str(payload.get("description", "")),
            tool=str(payload.get("tool", "")),
            arguments=_coerce_arguments(payload.get("arguments")),
            depends_on=tuple(str(item) for item in payload.get("depends_on") or []),
            risk=RiskLevel(risk) if risk in known_risks else RiskLevel.LOW,
            expected_paths=tuple(str(item) for item in payload.get("expected_paths") or []),
            expected_command=str(payload.get("expected_command", "")),
            optional=bool(payload.get("optional", False)),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """A reviewed, validated execution plan.

    ``digest`` is derived from the content that matters -- steps, criteria and
    scope -- rather than stored independently, because an approval that matches a
    digest the model could have edited is not a binding at all.
    """

    objective: str
    steps: tuple[PlannedStep, ...] = ()
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    scope_paths: tuple[str, ...] = ()
    notes: str = ""
    version: int = 1

    @property
    def digest(self) -> str:
        return plan_digest(self)

    def step(self, step_id: str) -> PlannedStep | None:
        return next((item for item in self.steps if item.id == step_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "steps": [item.to_dict() for item in self.steps],
            "acceptance_criteria": [item.to_dict() for item in self.acceptance_criteria],
            "scope_paths": list(self.scope_paths),
            "notes": self.notes,
            "version": self.version,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Plan:
        return cls(
            objective=str(payload.get("objective", "")),
            steps=tuple(PlannedStep.from_dict(item) for item in payload.get("steps") or []),
            acceptance_criteria=tuple(
                AcceptanceCriterion.from_dict(item)
                for item in payload.get("acceptance_criteria") or []
            ),
            scope_paths=tuple(str(item) for item in payload.get("scope_paths") or []),
            notes=str(payload.get("notes", "")),
            version=int(payload.get("version", 1)),
        )


def plan_digest(plan: Plan) -> str:
    """A stable digest of exactly what the user approved."""

    payload = json.dumps(
        {
            "objective": plan.objective,
            "steps": [step.to_dict() for step in plan.steps],
            "criteria": [item.to_dict() for item in plan.acceptance_criteria],
            "scope": list(plan.scope_paths),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# __PART3__


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """A user's decision about a plan, bound to what they decided on.

    ``scope_paths`` is the set of paths the approval covers; an empty tuple means
    the whole workspace, which is what a user who approves a plan without
    narrowing it means. There is deliberately no reusable token: an approval is a
    fact about one plan in one workspace, and anything reusable would have to be
    re-validated anyway -- so it is stored as the fact it is.
    """

    plan_digest: str
    workspace: str
    approved: bool
    scope_paths: tuple[str, ...] = ()
    decided_at: float = 0.0
    reason: str = ""

    def covers(self, path: str, workspace: str) -> bool:
        """Whether this approval authorises touching ``path``.

        Fails closed: a different workspace, a different plan, or a path outside
        an explicit scope all answer "no".
        """

        if not self.approved:
            return False
        if _normalise_workspace(workspace) != _normalise_workspace(self.workspace):
            return False
        if not self.scope_paths:
            return True
        target = _normalise_path(path)
        return any(
            target == _normalise_path(scope) or target.startswith(_normalise_path(scope) + "/")
            for scope in self.scope_paths
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_digest": self.plan_digest,
            "workspace": self.workspace,
            "approved": self.approved,
            "scope_paths": list(self.scope_paths),
            "decided_at": self.decided_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ApprovalRecord:
        return cls(
            plan_digest=str(payload.get("plan_digest", "")),
            workspace=str(payload.get("workspace", "")),
            approved=bool(payload.get("approved", False)),
            scope_paths=tuple(str(item) for item in payload.get("scope_paths") or []),
            decided_at=float(payload.get("decided_at", 0.0) or 0.0),
            reason=str(payload.get("reason", "")),
        )


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    """One executed operation and what it actually did."""

    step_id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    summary: str = ""
    error: str = ""
    started_at: float = 0.0
    duration_ms: int = 0
    approval_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolExecutionRecord:
        return cls(
            step_id=str(payload.get("step_id", "")),
            tool=str(payload.get("tool", "")),
            arguments=_coerce_arguments(payload.get("arguments")),
            ok=bool(payload.get("ok", False)),
            summary=str(payload.get("summary", "")),
            error=str(payload.get("error", "")),
            started_at=float(payload.get("started_at", 0.0) or 0.0),
            duration_ms=int(payload.get("duration_ms", 0) or 0),
            approval_id=str(payload.get("approval_id", "")),
        )


@dataclass(frozen=True, slots=True)
class FileChangeRecord:
    """Evidence that a file changed: what it was, what it is, and whether.

    Existence is recorded alongside content hashes because "the file was not there
    before" is the difference between a creation and an edit, and recovery has to
    tell those apart.
    """

    path: str
    operation: str
    existed_before: bool = False
    exists_after: bool = True
    before_hash: str = ""
    after_hash: str = ""
    step_id: str = ""
    bytes_before: int = 0
    bytes_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FileChangeRecord:
        return cls(
            path=str(payload.get("path", "")),
            operation=str(payload.get("operation", "")),
            existed_before=bool(payload.get("existed_before", False)),
            exists_after=bool(payload.get("exists_after", True)),
            before_hash=str(payload.get("before_hash", "")),
            after_hash=str(payload.get("after_hash", "")),
            step_id=str(payload.get("step_id", "")),
            bytes_before=int(payload.get("bytes_before", 0) or 0),
            bytes_after=int(payload.get("bytes_after", 0) or 0),
        )


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of one verification check."""

    check_id: str
    command: tuple[str, ...] = ()
    status: CheckStatus = CheckStatus.NOT_RUN
    exit_code: int | None = None
    duration_ms: int = 0
    output: str = ""
    detail: str = ""
    step_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "command": list(self.command),
            "status": str(self.status),
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "detail": self.detail,
            "step_id": self.step_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CheckResult:
        statuses = {status.value for status in CheckStatus}
        status = str(payload.get("status", CheckStatus.NOT_RUN))
        exit_code = payload.get("exit_code")
        return cls(
            check_id=str(payload.get("check_id", "")),
            command=tuple(str(item) for item in payload.get("command") or []),
            status=CheckStatus(status) if status in statuses else CheckStatus.NOT_RUN,
            exit_code=int(exit_code) if isinstance(exit_code, int) else None,
            duration_ms=int(payload.get("duration_ms", 0) or 0),
            output=str(payload.get("output", "")),
            detail=str(payload.get("detail", "")),
            step_id=str(payload.get("step_id", "")),
        )


# __PART4__


@dataclass(frozen=True, slots=True)
class RepairAttempt:
    """One bounded attempt to fix a failing step."""

    step_id: str
    attempt: int
    plan: Plan | None = None
    status: StepStatus = StepStatus.PENDING
    rationale: str = ""
    approved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "attempt": self.attempt,
            "plan": self.plan.to_dict() if self.plan else None,
            "status": str(self.status),
            "rationale": self.rationale,
            "approved": self.approved,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RepairAttempt:
        statuses = {status.value for status in StepStatus}
        status = str(payload.get("status", StepStatus.PENDING))
        plan = payload.get("plan")
        return cls(
            step_id=str(payload.get("step_id", "")),
            attempt=int(payload.get("attempt", 0) or 0),
            plan=Plan.from_dict(plan) if isinstance(plan, dict) else None,
            status=StepStatus(status) if status in statuses else StepStatus.PENDING,
            rationale=str(payload.get("rationale", "")),
            approved=bool(payload.get("approved", False)),
        )


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """A question for the user about work that may or may not have happened.

    Recovery never guesses. An interrupted command that might have completed, a
    file whose content no longer matches what was recorded, a workspace that is
    gone: each becomes a decision with the consequences spelled out, because the
    alternative -- silently replaying -- is how an agent repeats a side effect the
    user did not ask for twice.
    """

    kind: str
    subject: str
    detail: str = ""
    options: tuple[str, ...] = ()
    resolved: bool = False
    choice: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "detail": self.detail,
            "options": list(self.options),
            "resolved": self.resolved,
            "choice": self.choice,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RecoveryDecision:
        return cls(
            kind=str(payload.get("kind", "")),
            subject=str(payload.get("subject", "")),
            detail=str(payload.get("detail", "")),
            options=tuple(str(item) for item in payload.get("options") or []),
            resolved=bool(payload.get("resolved", False)),
            choice=str(payload.get("choice", "")),
        )


@dataclass(frozen=True, slots=True)
class TaskBudget:
    """Ceilings on what a task may consume.

    Defaults are conservative and every one has a hard maximum: a configurable
    "unlimited" budget is not a configuration option, it is an outage waiting for
    a model that miscounts. Counters are part of the task and survive restarts,
    because a budget that resets on restart is not a budget.
    """

    max_steps: int = 20
    max_repairs_per_step: int = 3
    max_active_seconds: int = 3600
    max_checks_per_task: int = 40

    HARD_MAX_STEPS = 200
    HARD_MAX_REPAIRS = 10
    HARD_MAX_SECONDS = 8 * 3600
    HARD_MAX_CHECKS = 200

    def clamped(self) -> TaskBudget:
        return TaskBudget(
            max_steps=max(1, min(self.max_steps, self.HARD_MAX_STEPS)),
            max_repairs_per_step=max(0, min(self.max_repairs_per_step, self.HARD_MAX_REPAIRS)),
            max_active_seconds=max(1, min(self.max_active_seconds, self.HARD_MAX_SECONDS)),
            max_checks_per_task=max(1, min(self.max_checks_per_task, self.HARD_MAX_CHECKS)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> TaskBudget:
        defaults = cls()
        if not isinstance(payload, dict):
            return defaults
        raw = {key: payload.get(key) for key in defaults.to_dict()}
        return cls(
            max_steps=int(raw["max_steps"] or defaults.max_steps),
            max_repairs_per_step=int(raw["max_repairs_per_step"] or defaults.max_repairs_per_step),
            max_active_seconds=int(raw["max_active_seconds"] or defaults.max_active_seconds),
            max_checks_per_task=int(raw["max_checks_per_task"] or defaults.max_checks_per_task),
        ).clamped()


@dataclass(frozen=True, slots=True)
class TaskReport:
    """The final, truthful account of what happened.

    Built from recorded evidence rather than from what the model says went well.
    ``unverified`` is the field that matters: a check that could not run belongs
    there, not quietly in ``passed``.
    """

    task_id: str
    status: TaskStatus = TaskStatus.DRAFT
    summary: str = ""
    steps_completed: int = 0
    steps_failed: int = 0
    files_changed: tuple[str, ...] = ()
    checks_passed: int = 0
    checks_failed: int = 0
    checks_not_run: tuple[str, ...] = ()
    repairs_attempted: int = 0
    acceptance: tuple[AcceptanceCriterion, ...] = ()
    blockers: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": str(self.status),
            "summary": self.summary,
            "steps_completed": self.steps_completed,
            "steps_failed": self.steps_failed,
            "files_changed": list(self.files_changed),
            "checks_passed": self.checks_passed,
            "checks_failed": self.checks_failed,
            "checks_not_run": list(self.checks_not_run),
            "repairs_attempted": self.repairs_attempted,
            "acceptance": [item.to_dict() for item in self.acceptance],
            "blockers": list(self.blockers),
            "unverified": list(self.unverified),
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TaskReport:
        statuses = {status.value for status in TaskStatus}
        status = str(payload.get("status", TaskStatus.DRAFT))
        return cls(
            task_id=str(payload.get("task_id", "")),
            status=TaskStatus(status) if status in statuses else TaskStatus.DRAFT,
            summary=str(payload.get("summary", "")),
            steps_completed=int(payload.get("steps_completed", 0) or 0),
            steps_failed=int(payload.get("steps_failed", 0) or 0),
            files_changed=tuple(str(item) for item in payload.get("files_changed") or []),
            checks_passed=int(payload.get("checks_passed", 0) or 0),
            checks_failed=int(payload.get("checks_failed", 0) or 0),
            checks_not_run=tuple(str(item) for item in payload.get("checks_not_run") or []),
            repairs_attempted=int(payload.get("repairs_attempted", 0) or 0),
            acceptance=tuple(
                AcceptanceCriterion.from_dict(item) for item in payload.get("acceptance") or []
            ),
            blockers=tuple(str(item) for item in payload.get("blockers") or []),
            unverified=tuple(str(item) for item in payload.get("unverified") or []),
            duration_ms=int(payload.get("duration_ms", 0) or 0),
        )


# __PART5__


@dataclass(slots=True)
class Task:
    """Everything known about one task, in one mutable record.

    Mutable because the orchestrator updates it on every state transition and
    persists it as it goes. The plan and its digest are not edited in place: a
    running task cannot quietly rewrite what the user approved.
    """

    id: str
    objective: str
    workspace: str
    status: TaskStatus = TaskStatus.DRAFT
    plan: Plan | None = None
    budget: TaskBudget = field(default_factory=TaskBudget)
    created_at: float = 0.0
    updated_at: float = 0.0
    approval: ApprovalRecord | None = None
    step_status: dict[str, StepStatus] = field(default_factory=dict)
    executions: list[ToolExecutionRecord] = field(default_factory=list)
    file_changes: list[FileChangeRecord] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    repairs: list[RepairAttempt] = field(default_factory=list)
    recovery: list[RecoveryDecision] = field(default_factory=list)
    steps_executed: int = 0
    checks_run: int = 0
    active_seconds: float = 0.0
    conversation_id: str = ""
    report: TaskReport | None = None

    def status_of(self, step_id: str) -> StepStatus:
        return self.step_status.get(step_id, StepStatus.PENDING)

    def set_status(self, step_id: str, status: StepStatus) -> None:
        self.step_status[step_id] = status

    @property
    def terminal(self) -> bool:
        return self.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)

    @property
    def approved_plan(self) -> Plan | None:
        """The plan, but only while an approval for that exact plan stands.

        A digest mismatch means the plan moved after approval, so there is no
        approved plan and every execution path fails closed.
        """

        if self.plan is None or self.approval is None:
            return None
        if self.approval.plan_digest != self.plan.digest:
            return None
        return self.plan

    def changed_paths(self) -> tuple[str, ...]:
        seen: list[str] = []
        for record in self.file_changes:
            if record.path not in seen:
                seen.append(record.path)
        return tuple(seen)

    def budget_remaining(self) -> dict[str, float]:
        budget = self.budget
        return {
            "steps": max(0.0, float(budget.max_steps - self.steps_executed)),
            "checks": max(0.0, float(budget.max_checks_per_task - self.checks_run)),
            "seconds": max(0.0, float(budget.max_active_seconds - self.active_seconds)),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "objective": self.objective,
            "workspace": self.workspace,
            "status": str(self.status),
            "plan": self.plan.to_dict() if self.plan else None,
            "budget": self.budget.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approval": self.approval.to_dict() if self.approval else None,
            "step_status": {key: str(value) for key, value in self.step_status.items()},
            "executions": [item.to_dict() for item in self.executions],
            "file_changes": [item.to_dict() for item in self.file_changes],
            "checks": [item.to_dict() for item in self.checks],
            "repairs": [item.to_dict() for item in self.repairs],
            "recovery": [item.to_dict() for item in self.recovery],
            "steps_executed": self.steps_executed,
            "checks_run": self.checks_run,
            "active_seconds": self.active_seconds,
            "conversation_id": self.conversation_id,
            "report": self.report.to_dict() if self.report else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Task:
        """Read a stored task, migrating older records forward."""

        migrated = migrate(payload)
        plan_payload = migrated.get("plan")
        report_payload = migrated.get("report")
        statuses = {status.value for status in TaskStatus}
        status = str(migrated.get("status", TaskStatus.DRAFT))
        return cls(
            id=str(migrated.get("id", "")),
            objective=str(migrated.get("objective", "")),
            workspace=str(migrated.get("workspace", "")),
            status=TaskStatus(status) if status in statuses else TaskStatus.DRAFT,
            plan=Plan.from_dict(plan_payload) if isinstance(plan_payload, dict) else None,
            budget=TaskBudget.from_dict(migrated.get("budget")),
            created_at=float(migrated.get("created_at", 0.0) or 0.0),
            updated_at=float(migrated.get("updated_at", 0.0) or 0.0),
            approval=(
                ApprovalRecord.from_dict(migrated["approval"])
                if isinstance(migrated.get("approval"), dict)
                else None
            ),
            step_status={
                str(key): StepStatus(str(value))
                for key, value in (migrated.get("step_status") or {}).items()
            },
            executions=[
                ToolExecutionRecord.from_dict(item) for item in migrated.get("executions") or []
            ],
            file_changes=[
                FileChangeRecord.from_dict(item) for item in migrated.get("file_changes") or []
            ],
            checks=[CheckResult.from_dict(item) for item in migrated.get("checks") or []],
            repairs=[RepairAttempt.from_dict(item) for item in migrated.get("repairs") or []],
            recovery=[RecoveryDecision.from_dict(item) for item in migrated.get("recovery") or []],
            steps_executed=int(migrated.get("steps_executed", 0) or 0),
            checks_run=int(migrated.get("checks_run", 0) or 0),
            active_seconds=float(migrated.get("active_seconds", 0.0) or 0.0),
            conversation_id=str(migrated.get("conversation_id", "")),
            report=(
                TaskReport.from_dict(report_payload) if isinstance(report_payload, dict) else None
            ),
        )


def migrate(payload: object) -> dict[str, Any]:
    """Bring a stored record up to :data:`SCHEMA_VERSION`.

    Version 1 is the first task schema, so today this is a guard rather than a
    rewrite: a record claiming a *newer* schema than this build understands is
    refused, because half-reading a future record is how a task silently loses its
    approval scope.
    """

    if not isinstance(payload, dict):
        return {}
    try:
        version = int(payload.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION)
    except (TypeError, ValueError):
        version = SCHEMA_VERSION
    if version > SCHEMA_VERSION:
        raise PlanRejected([f"task record schema {version} is newer than this build"])
    migrated = dict(payload)
    migrated["schema_version"] = SCHEMA_VERSION
    return migrated


# __PART6__


def validate_plan(
    plan: Plan,
    *,
    known_tools: set[str] | frozenset[str] | None = None,
    workspace: str | None = None,
    max_steps: int = 100,
) -> Plan:
    """Validate a plan from an untrusted source, or raise :class:`PlanRejected`.

    Every reason is collected rather than raising on the first, because a model
    that has misread the tool set learns far more from being told all of it at
    once than from discovering one problem per turn.

    Rejects: an empty objective, malformed identifiers, unknown tools, duplicate
    step ids, dependencies on steps that do not exist, dependency cycles, unknown
    risk levels, path arguments that escape the workspace, and plans larger than
    the step ceiling.
    """

    reasons: list[str] = []
    objective = plan.objective.strip()
    if len(objective) < 4:
        reasons.append("objective is missing or too short to describe a task")
    if not plan.steps:
        reasons.append("plan has no steps")
    if len(plan.steps) > max_steps:
        reasons.append(f"plan has {len(plan.steps)} steps, more than the limit of {max_steps}")

    risks = {level.value for level in RiskLevel}
    seen: set[str] = set()
    for step in plan.steps:
        step_id = _require_id(step.id, "step id", reasons)
        if step_id and step_id in seen:
            reasons.append(f"step id {step_id!r} is used more than once")
        seen.add(step_id)
        if not step.tool.strip():
            reasons.append(f"step {step_id or '?'} names no tool")
        elif known_tools is not None and step.tool not in known_tools:
            reasons.append(f"step {step_id or '?'} names unknown tool {step.tool!r}")
        if str(step.risk) not in risks:
            reasons.append(f"step {step_id or '?'} has unknown risk {step.risk!r}")
        for dependency in step.depends_on:
            if not _ID_RE.match(dependency):
                reasons.append(f"step {step_id or '?'} depends on invalid id {dependency!r}")
            if dependency == step.id:
                reasons.append(f"step {step_id or '?'} depends on itself")

    known = {step.id for step in plan.steps}
    for step in plan.steps:
        for dependency in step.depends_on:
            if _ID_RE.match(dependency) and dependency not in known:
                reasons.append(f"step {step.id or '?'} depends on {dependency!r}, not a step")

    reasons.extend(_dependency_cycles(plan))

    if workspace:
        for step in plan.steps:
            for path in _path_arguments(step):
                if not _inside_workspace(path, workspace):
                    reasons.append(f"step {step.id or '?'} targets {path!r}, outside the workspace")

    criteria_ids: set[str] = set()
    for criterion in plan.acceptance_criteria:
        criterion_id = _require_id(criterion.id, "criterion id", reasons)
        if criterion_id in criteria_ids:
            reasons.append(f"acceptance criterion {criterion_id!r} is duplicated")
        criteria_ids.add(criterion_id)
        if len(criterion.description.strip()) < 3:
            reasons.append(f"acceptance criterion {criterion_id or '?'} has no description")

    if reasons:
        raise PlanRejected(reasons)
    return replace(plan, objective=objective)


def _dependency_cycles(plan: Plan) -> list[str]:
    """Report dependency cycles, which would otherwise deadlock scheduling."""

    graph = {step.id: list(step.depends_on) for step in plan.steps}
    cycles: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node: str, trail: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            cycle = " -> ".join([*trail[trail.index(node) :], node])
            message = f"dependency cycle: {cycle}"
            if message not in cycles:
                cycles.append(message)
            return
        visiting.add(node)
        for dependency in graph.get(node, []):
            if dependency in graph:
                walk(dependency, [*trail, node])
        visiting.discard(node)
        done.add(node)

    for step in plan.steps:
        walk(step.id, [])
    return cycles


def _path_arguments(step: PlannedStep) -> list[str]:
    """Values in a step's arguments that name a filesystem path."""

    keys = {"path", "file", "target", "directory", "new_path", "old_path"}
    found: list[str] = []
    for key, value in step.arguments.items():
        if isinstance(value, str) and value and (key in keys or key.endswith("_path")):
            found.append(value)
    found.extend(path for path in step.expected_paths if path)
    return found
