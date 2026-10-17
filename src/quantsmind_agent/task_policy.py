"""One policy layer, on every execution path.

The safety argument for v0.4 rests on a single claim: there is exactly one place
that decides whether an operation may run. The GUI cannot bypass it, a retry
cannot bypass it, a recovered task cannot bypass it, and neither can a model that
has decided it is finished.

This module is that place. :class:`TaskPolicy` answers one question -- may this
operation run, right now, for this task -- by weighing five things:

1. **The workspace boundary.** Resolved through the real
   :class:`~quantsmind_agent.workspace.Workspace`, never by string comparison.
2. **The approved plan.** A step that is not in the approved plan is refused even
   if the tool would permit it, and an approval whose digest no longer matches
   the plan authorises nothing.
3. **Approval scope.** An approval limited to some paths does not extend to
   others, and a later, riskier step needs its own decision.
4. **Risk.** Read-only work proceeds; anything medium or higher needs explicit
   approval at the moment it is about to happen.
5. **Lifecycle.** Nothing executes before a plan is approved, after a task is
   cancelled, or while a task is paused.

What this is not
----------------
This is policy enforcement inside an application, not an operating-system
sandbox. A command the user approves can do whatever that program can do; the
protection here is that unapproved commands never start, that the workspace
boundary is enforced by the same code that enforces it everywhere else in the
product, and that every decision is recorded.

Failure is closed. Every check that cannot answer "yes" answers "no", and the
reason is returned so the user is told what stopped the work rather than being
left with a task that silently stopped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from quantsmind_agent.tasks import (
    ApprovalRecord,
    Plan,
    PlannedStep,
    RiskLevel,
    Task,
    TaskStatus,
    plan_digest,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quantsmind_agent.workspace import Workspace

__all__ = [
    "DESTRUCTIVE_TOOLS",
    "MUTATING_TOOLS",
    "NETWORK_COMMANDS",
    "OperationKind",
    "PolicyVerdict",
    "TaskPolicy",
    "needs_separate_approval",
]


class OperationKind(StrEnum):
    """What kind of thing an operation is, independent of which tool performs it."""

    READ = "read"
    ANALYSE = "analyse"
    MUTATE = "mutate"
    DELETE = "delete"
    COMMAND = "command"
    NETWORK = "network"
    GIT = "git"


#: Tools that change the workspace.
MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete_file", "undo_file"})

#: Tools that remove something the user may want back.
DESTRUCTIVE_TOOLS = frozenset({"delete_file"})

#: Tools that reach outside the workspace.
NETWORK_TOOLS = frozenset({"run_command"})

#: Command prefixes that mean "this will reach the network". Used to classify a
#: verification command as read-only or not, because ``git status`` and
#: ``pip install`` are both ``run_command``.
NETWORK_COMMANDS = frozenset(
    {
        "curl",
        "wget",
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "npm",
        "pip",
        "pipx",
        "uv",
        "poetry",
        "cargo",
        "gem",
        "apt",
        "apt-get",
        "brew",
        "choco",
        "winget",
        "scoop",
        "git",
    }
)

#: Commands that change the machine outside the workspace.
_SYSTEM_MUTATING_COMMANDS = frozenset(
    {"pip", "pipx", "uv", "poetry", "npm", "apt", "apt-get", "brew", "choco", "winget", "scoop"}
)


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """The answer, and the reason for it.

    ``reason`` is user-facing on purpose: "the plan was approved for these paths
    only" is something a person can act on, where an opaque denial is not.
    """

    allowed: bool
    reason: str = ""
    kind: OperationKind = OperationKind.READ
    requires_approval: bool = False

    def __bool__(self) -> bool:
        return self.allowed


def needs_separate_approval(kind: OperationKind, risk: RiskLevel, *, network: bool = False) -> bool:
    """Whether an operation needs its own approval beyond the plan's.

    Deletes and commands always do, because their blast radius is not visible
    from a diff. Anything medium or higher risk does. Network operations do,
    unless the caller has already established that this exact operation is
    covered by a narrower configured policy -- which is the caller's decision to
    make and to record, not this function's.
    """

    if kind is OperationKind.DELETE:
        return True
    if network:
        return True
    if kind is OperationKind.COMMAND and risk != RiskLevel.READ_ONLY:
        return True
    return risk.requires_approval


def classify(tool: str, arguments: dict[str, Any] | None = None) -> OperationKind:
    """What kind of operation a tool call is.

    Classification is by registered tool name, not by anything the caller says:
    a step that claims to be a read while naming ``delete_file`` is a delete.
    """

    if tool in DESTRUCTIVE_TOOLS:
        return OperationKind.DELETE
    if tool in MUTATING_TOOLS:
        return OperationKind.MUTATE
    if tool in NETWORK_TOOLS:
        return OperationKind.COMMAND
    if tool.startswith("git_"):
        return OperationKind.GIT
    if tool.startswith(("code_", "project_", "find_")) or tool == "project_symbols":
        return OperationKind.ANALYSE
    return OperationKind.READ


def command_touches_network(argv: list[str]) -> bool:
    """Whether a command is likely to reach the network.

    A prefix match, deliberately: this decides whether *approval* is required,
    and a false positive there costs one prompt while a false negative costs a
    silent dependency install.
    """

    if not argv:
        return False
    program = os.path.basename(str(argv[0])).lower()
    if program.endswith(".exe"):
        program = program[:-4]
    if program == "git":
        # git is only as network-y as its subcommand: `git status` is local and
        # `git clone` is not. Checking the program alone would demand approval
        # for reading the repository's own history.
        sub = str(argv[1]).lower() if len(argv) > 1 else ""
        return sub in {"clone", "fetch", "pull", "push", "remote", "submodule", "ls-remote"}
    return program in NETWORK_COMMANDS or program in _SYSTEM_MUTATING_COMMANDS


def command_is_system_mutating(argv: list[str]) -> bool:
    """Whether a command would change the machine outside the workspace."""

    if not argv:
        return False
    program = os.path.basename(str(argv[0])).lower()
    if program.endswith(".exe"):
        program = program[:-4]
    return program in _SYSTEM_MUTATING_COMMANDS


def _target_arguments(step: PlannedStep) -> list[str]:
    """The arguments that name a filesystem path.

    Keyed by name, not by "any string": a write step's ``content`` is a payload,
    and treating it as a target made every scoped approval fail on the file body
    rather than on the file.
    """

    keys = {"path", "file", "target", "directory", "new_path", "old_path"}
    targets = [
        str(value)
        for key, value in step.arguments.items()
        if isinstance(value, str) and value and (key in keys or key.endswith("_path"))
    ]
    targets.extend(path for path in step.expected_paths if path)
    return targets


class TaskPolicy:
    """Decides whether one operation may run, right now, for one task."""

    def __init__(
        self,
        *,
        workspace: Workspace | None = None,
        approved_network: frozenset[tuple[str, ...]] = frozenset(),
    ) -> None:
        self._workspace = workspace
        #: Exact commands the user has authorised in advance, as argument tuples.
        #: Narrow on purpose: a whole program name would authorise the next thing
        #: somebody thinks of.
        self._approved_network = approved_network

    def bind(self, workspace: Workspace | None) -> None:
        """Attach the workspace whose boundary this policy enforces."""

        self._workspace = workspace

    # -- the decision -----------------------------------------------------
    def evaluate(
        self,
        task: Task,
        step: PlannedStep,
        *,
        extra_approval: ApprovalRecord | None = None,
        workspace_root: str | None = None,
    ) -> PolicyVerdict:
        """Whether ``step`` may run now. Never raises; fails closed."""

        kind = classify(step.tool, step.arguments)
        root = workspace_root or task.workspace

        lifecycle = self._lifecycle_verdict(task)
        if not lifecycle.allowed:
            return lifecycle

        plan = task.approved_plan
        if plan is None:
            return PolicyVerdict(
                False,
                "the plan is not approved, or the plan changed after approval",
                kind,
            )
        if step.id not in {item.id for item in plan.steps}:
            return PolicyVerdict(False, f"step {step.id!r} is not part of the approved plan", kind)

        boundary = self._boundary_verdict(task, step, root, kind)
        if not boundary.allowed:
            return boundary

        scope = self._scope_verdict(task, step, root, kind)
        if not scope.allowed:
            return scope

        command = self._command_arguments(step)
        network = command_touches_network(command) if command else False
        argv_key = tuple(command) if command else ()
        covered = argv_key in self._approved_network
        if network and not covered:
            return PolicyVerdict(
                False,
                "this step reaches the network and no policy authorises that exact command",
                OperationKind.NETWORK,
                True,
            )

        separate = needs_separate_approval(kind, step.risk, network=network and not covered)
        if covered:
            # An exact, configured authorisation for this command replaces the
            # per-step prompt. Asking again would train the user to click through
            # the prompt that the configuration exists to avoid.
            separate = step.risk.requires_approval
        if not separate:
            return PolicyVerdict(True, "within the approved plan", kind)
        if extra_approval is not None and extra_approval.approved:
            if not extra_approval.covers(
                step.expected_paths[0] if step.expected_paths else root, root
            ):
                return PolicyVerdict(
                    False,
                    "the approval for this step does not cover its target",
                    kind,
                    True,
                )
            return PolicyVerdict(True, "approved for this restricted operation", kind)
        return PolicyVerdict(
            False,
            f"{kind.value} work at {step.risk.value} risk needs its own approval",
            kind,
            True,
        )

    # -- individual checks ------------------------------------------------
    def _lifecycle_verdict(self, task: Task) -> PolicyVerdict:
        if task.status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
            return PolicyVerdict(False, f"the task is {task.status.value}", OperationKind.READ)
        if task.status in (TaskStatus.PAUSED, TaskStatus.BLOCKED):
            return PolicyVerdict(False, f"the task is {task.status.value}", OperationKind.READ)
        if task.status not in (TaskStatus.APPROVED, TaskStatus.RUNNING, TaskStatus.REPAIRING):
            return PolicyVerdict(
                False,
                f"the task is {task.status.value}, so execution has not started",
                OperationKind.READ,
            )
        return PolicyVerdict(True, "", OperationKind.READ)

    def _boundary_verdict(
        self, task: Task, step: PlannedStep, root: str, kind: OperationKind
    ) -> PolicyVerdict:
        if kind in (
            OperationKind.READ,
            OperationKind.ANALYSE,
            OperationKind.MUTATE,
            OperationKind.DELETE,
        ):
            for path in _target_arguments(step):
                verdict = self._check_path(path, root)
                if not verdict.allowed:
                    return verdict
        return PolicyVerdict(True, "", kind)

    def _check_path(self, path: str, root: str) -> PolicyVerdict:
        if self._workspace is not None:
            try:
                self._workspace.resolve(path)
            except Exception as exc:  # the workspace reports refusal, not policy
                return PolicyVerdict(False, str(exc), OperationKind.READ)
        if _escapes(path, root):
            return PolicyVerdict(False, f"{path} is outside the workspace", OperationKind.READ)
        return PolicyVerdict(True, "", OperationKind.READ)

    def _scope_verdict(
        self, task: Task, step: PlannedStep, root: str, kind: OperationKind
    ) -> PolicyVerdict:
        approval = task.approval
        if approval is None:
            return PolicyVerdict(False, "there is no approval on this task", kind)
        targets = _target_arguments(step)
        if not targets:
            return PolicyVerdict(True, "", kind)
        for target in targets:
            if not approval.covers(target, root):
                return PolicyVerdict(
                    False,
                    f"the approval does not cover {target}",
                    kind,
                )
        return PolicyVerdict(True, "", kind)

    @staticmethod
    def _command_arguments(step: PlannedStep) -> list[str]:
        command = step.arguments.get("command")
        arguments = step.arguments.get("arguments")
        if isinstance(command, str):
            argv = [command]
        elif isinstance(command, list):
            argv = [str(item) for item in command]
        else:
            argv = []
        if isinstance(arguments, list):
            argv += [str(item) for item in arguments]
        return argv


def _escapes(path: str, root: str) -> bool:
    """A textual containment check used when no workspace object is available.

    The real boundary is the workspace's own resolution; this is the fallback for
    a policy used without one, and it still refuses ``..`` rather than assuming
    the caller cleaned the path.
    """

    text = str(path or "").replace("\\", "/")
    if not text:
        return False
    base = str(root or "").replace("\\", "/").rstrip("/").lower()
    if text.startswith("/") or (len(text) > 2 and text[1] == ":"):
        lowered = text.lower()
        return not (lowered == base or lowered.startswith(base + "/"))
    return ".." in text.split("/")


def approval_for_plan(
    plan: Plan, workspace: str, *, approved: bool, scope: tuple[str, ...] = (), reason: str = ""
) -> ApprovalRecord:
    """Build an approval bound to exactly this plan and workspace."""

    return ApprovalRecord(
        plan_digest=plan_digest(plan),
        workspace=workspace,
        approved=approved,
        scope_paths=scope,
        reason=reason,
    )
