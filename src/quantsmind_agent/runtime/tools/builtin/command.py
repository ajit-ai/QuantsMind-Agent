"""The v0.2 ``run_command`` tool.

Executes one development command inside the configured workspace, after the
user has approved it.

Design notes
------------
**No shell, ever.** ``shell=False`` with an explicit argv list is the primary
safety property, and it is what makes the structural argument in
:mod:`quantsmind_agent.security.command_policy` sound. Shell metacharacters (``;``,
``&&``, ``|``, backticks, ``$VAR``) are *not* interpreted: they arrive at the
target program as ordinary argument text. A command line can therefore not be
turned into a second program by injection.

**Two independent gates.** :meth:`run` refuses anything the policy marks
``BLOCKED`` before touching the process table. The permission registry separately
requires an approval (``ASK``) for everything that reaches ``run``. Passing the
policy is not permission, and permission is not a bypass of the policy.

**Bounded everything.** Output is capped, the runtime is capped, and the
argument vector is capped. A command that prints forever or hangs forever
cannot exhaust memory or wedge the GUI.

**Credentials are not inherited.** Model-provider keys are stripped from the
child environment, so a command that prints its own environment cannot leak the
API key into the transcript.

Known limitation
----------------
Cancellation terminates the direct child only. On Windows a process tree has no
single portable kill, so a command that spawned grandchildren may leave them
running. Isolating that properly needs job objects or a container, which is out
of scope for v0.2.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import shlex
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import (
    CommandDeniedError,
    CommandExecutionError,
    CommandTimeoutError,
    InvalidPathError,
)
from quantsmind_agent.runtime.tools import ToolResult, ToolSpec
from quantsmind_agent.security.audit import (
    AuditEventType,
    AuditPersistenceError,
    SecurityAuditLog,
)
from quantsmind_agent.security.command_policy import PolicyVerdict, classify_command
from quantsmind_agent.security.permissions import Permission
from quantsmind_agent.services.workspace.containment import Workspace

__all__ = [
    "CappedReader",
    "RunCommandTool",
    "build_command_tools",
    "sanitized_environment",
    "tokenize_command",
]

logger = get_logger(__name__)

#: The isolation a sandboxed command requires by default. ``LIFECYCLE`` is the
#: level this host can establish without elevation; a caller handling
#: untrusted code asks for ``UNTRUSTED_CODE`` and the run is refused when the
#: boundary cannot provide it (M3B-S1.4).
from quantsmind_agent.sandbox.policy import IsolationLevel as _IsolationLevel  # noqa: E402

_DEFAULT_SANDBOX_ISOLATION: Any = _IsolationLevel.LIFECYCLE


# -- Windows Job Object support for process-tree cancellation -----------------
# On Windows, terminating the direct child process does not kill grandchildren
# it may have spawned. A Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
# ensures that closing the job handle terminates every process assigned to it,
# preventing orphan processes after cancellation or timeout.
if sys.platform == "win32":
    _kernel32 = ctypes.windll.kernel32

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_longlong),
            ("WriteOperationCount", ctypes.c_longlong),
            ("OtherOperationCount", ctypes.c_longlong),
            ("ReadTransferCount", ctypes.c_longlong),
            ("WriteTransferCount", ctypes.c_longlong),
            ("OtherTransferCount", ctypes.c_longlong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _create_job_object() -> Any:
        """Create a Windows Job Object with KILL_ON_JOB_CLOSE.

        Returns the job handle, or None if creation failed (caller proceeds
        without job-object isolation).
        """

        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        if not _kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            _kernel32.CloseHandle(job)
            return None

        return job

    def _assign_process_to_job(job: Any, process_handle: int) -> bool:
        """Assign a process handle to a Job Object.

        Returns True on success, False otherwise.
        """

        return bool(_kernel32.AssignProcessToJobObject(job, process_handle))

else:

    def _create_job_object():
        """No-op on non-Windows platforms."""
        return None

    def _assign_process_to_job(job, process_handle):
        """No-op on non-Windows platforms."""
        return False


#: Upper bounds, so a misbehaving command cannot exhaust the machine.
MAX_OUTPUT_CHARS = 20_000
MAX_TIMEOUT_SECONDS = 600.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_ARGUMENTS = 64
MAX_ARGUMENT_CHARS = 4_000
#: How often the wait loop wakes to check the cancel flag.
_POLL_SECONDS = 0.2
#: Grace period between asking a child to stop and killing it.
_TERMINATE_GRACE_SECONDS = 3.0

#: Environment variables removed from every child process.
_CREDENTIAL_ENV_NAMES: tuple[str, ...] = (
    "QM_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_ORGANIZATION",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
)


def tokenize_command(command: str) -> list[str]:
    """Split a command line into an argv list without shell semantics.

    ``shlex`` in non-POSIX mode keeps the quotes so that a Windows path such as
    ``"C:\\Program Files\\x"`` survives as one argument; the surrounding quotes
    are stripped per token afterwards.
    """

    try:
        raw = shlex.split(command, posix=False)
    except ValueError as exc:
        raise CommandExecutionError(
            command,
            f"the command line could not be parsed ({exc})",
        ) from exc
    tokens: list[str] = []
    for item in raw:
        if len(item) >= 2 and item[0] == item[-1] and item[0] in {'"', "'"}:
            item = item[1:-1]
        if item:
            tokens.append(item)
    return tokens


def sanitized_environment(extra_redactions: tuple[str, ...] = ()) -> dict[str, str]:
    """A copy of the environment with model credentials removed."""

    env = dict(os.environ)
    for name in (*_CREDENTIAL_ENV_NAMES, *extra_redactions):
        env.pop(name, None)
    return env


class CappedReader(threading.Thread):
    """Drain one pipe into memory, keeping at most ``cap`` characters.

    The drain continues past the cap so the child never blocks on a full pipe;
    only the retained text is bounded.

    When output overflows, the *tail* is kept as well as the head. A build or
    test command prints its interesting part last -- the failing assertion, the
    traceback, the error summary -- and a head-only cap silently throws away the
    one thing the agent needs in order to fix the failure it just caused. The
    middle is dropped, with an explicit marker so a truncated observation never
    reads as a complete one.
    """

    #: Marker inserted where the middle of the output was dropped. Kept short so
    #: it fits inside a modest cap -- the marker is part of the budget.
    OMITTED = "\n... [{omitted} chars omitted] ...\n"

    def __init__(self, stream: Any, cap: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._cap = max(0, int(cap))
        self._head_budget = (self._cap + 1) // 2
        self._tail_budget = self._cap // 2
        self._head: list[str] = []
        self._tail_text = ""
        self._head_size = 0
        self._omitted = 0
        self.total = 0

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(4096)
                if not chunk:
                    return
                self.total += len(chunk)
                self._keep(chunk)
        except (OSError, ValueError):  # pragma: no cover - pipe closed mid-read
            return
        finally:
            with contextlib.suppress(OSError, ValueError):  # pragma: no cover
                self._stream.close()

    def _keep(self, chunk: str) -> None:
        """Route one chunk to the head, the tail, or the omitted middle.

        Half the budget goes to each end. Once a buffer is full, everything
        after it is only useful from the other side, so the tail slides instead
        of growing.
        """

        head_room = self._head_budget - self._head_size
        if head_room > 0:
            self._head.append(chunk[:head_room])
            self._head_size += min(head_room, len(chunk))
            chunk = chunk[head_room:]
            if not chunk:
                return
        self._keep_tail(chunk)

    def _keep_tail(self, chunk: str) -> None:
        """Slide the tail window so it always ends at the newest byte seen.

        A tail that simply stopped filling would be wrong for exactly the case
        that matters: a command that prints a lot and then fails, where the
        diagnosis arrives after the window is already full.
        """

        if self._tail_budget == 0:
            self._omitted += len(chunk)
            return
        combined = self._tail_text + chunk
        if len(combined) <= self._tail_budget:
            self._tail_text = combined
            return
        self._omitted += len(combined) - self._tail_budget
        self._tail_text = combined[-self._tail_budget :]

    @property
    def text(self) -> str:
        """The retained text, never longer than the cap, marker included."""

        head = "".join(self._head)
        tail = self._tail_text
        if not self._omitted:
            return head + tail
        marker = self.OMITTED.format(omitted=self._omitted)
        # The marker is part of the budget: a cap is a promise about how much
        # text the model is handed, not just about how much is kept.
        room = self._cap - len(head) - len(marker)
        if room >= 0:
            return head + marker + tail[-room:] if room else head + marker
        # A cap too small to carry the head, the marker and the tail: keep the
        # tail alone, because that is where the failure is. The omission is
        # still reported in the result metadata, so nothing here is silent.
        return tail[-self._cap :]

    @property
    def omitted_chars(self) -> int:
        return self._omitted

    @property
    def truncated(self) -> bool:
        return self._omitted > 0


def _truncation_metadata(prefix: str, reader: CappedReader) -> dict[str, Any]:
    """Truncation facts for one stream, so a cut is never silent.

    ``*_omitted_chars`` says how much was dropped, which is the difference
    between "this output was complete" and "look at what survived".
    """

    return {
        f"{prefix}_truncated": reader.truncated,
        f"{prefix}_omitted_chars": reader.omitted_chars,
    }


class RunCommandTool:
    """Run one approved development command inside the workspace."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        cancel_event: threading.Event | None = None,
        default_timeout: float = 120.0,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        redact_env: tuple[str, ...] = (),
        sandbox_runner: Any | None = None,
        sandbox_isolation: Any | None = None,
        sandbox_max_child_processes: int = 8,
        audit: SecurityAuditLog | None = None,
    ) -> None:
        self._workspace = workspace
        self._cancel = cancel_event
        self._default_timeout = min(max(default_timeout, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
        self._max_output_chars = max_output_chars
        self._redact_env = redact_env
        #: Windows Job Object handle for process-tree cancellation. When set,
        #: closing the handle terminates every process assigned to the job,
        #: preventing orphan grandchildren after cancellation or timeout.
        self._job: Any = None
        #: What the audit trail is told about each finished command. The record
        #: carries the argv summary, the exit code and the policy verdict -- the
        #: output itself stays out of it, because output can hold anything.
        self._audit = audit
        #: When a runner is supplied, every command runs inside the OS
        #: boundary (M3B-S1.4) and a refusal is a failure, never a fall back
        #: to an ordinary subprocess.
        self._sandbox_runner = sandbox_runner
        self._sandbox_isolation = sandbox_isolation or _DEFAULT_SANDBOX_ISOLATION
        self._sandbox_max_child_processes = int(sandbox_max_child_processes)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="run_command",
            description=(
                "Run one development command inside the project folder, such as running "
                "the test suite, the linter or a version query. The command always runs "
                "with the project folder as its working directory and needs the user's "
                "approval first. Commands are not run through a shell, so ; | && and "
                "redirects are passed to the program literally and do nothing. "
                "Prefer passing arguments as a list to avoid any quoting ambiguity. "
                "Destructive host operations are refused outright and cannot be approved."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "The command to run, e.g. 'python -m pytest -q'. Use "
                            "'arguments' instead when a path or value contains spaces."
                        ),
                    },
                    "arguments": {
                        "type": "array",
                        "description": (
                            "Optional argument list. When present it is used verbatim "
                            "and 'command' supplies only the program."
                        ),
                        "items": {"type": "string"},
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Workspace-relative directory to run in. Default: the project "
                            "root. Absolute paths and '..' are rejected."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": (
                            f"Stop the command after this many seconds "
                            f"({MIN_TIMEOUT_SECONDS:g}-{MAX_TIMEOUT_SECONDS:g})."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One line on why this command is needed. Shown to the user in "
                            "the approval prompt."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            permission=Permission.EXECUTE_COMMAND,
            summary_template="run {command}",
            read_only=False,
            guard=self._refuse_if_blocked,
        )

    def _refuse_if_blocked(self, arguments: dict[str, Any]) -> None:
        """Policy gate, run by the registry before the user is asked.

        A blocked command must never reach an approval dialog: showing "Allow"
        for ``format C:`` would make the dialog decorative. This raises for
        refusals only -- it can never turn a denial into an approval.
        """

        argv = self._argv(arguments)
        verdict = self.classify(argv)
        if not verdict.permitted:
            raise CommandDeniedError(
                " ".join(argv),
                verdict.reason,
                rule=verdict.rule,
                details={"risk": verdict.risk_label},
            )

    # -- helpers ---------------------------------------------------------
    def _argv(self, arguments: dict[str, Any]) -> list[str]:
        """Build the exact argv to execute, or raise a domain error."""

        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise CommandExecutionError(
                str(command or ""),
                "no command was provided",
            )

        supplied = arguments.get("arguments")
        if isinstance(supplied, list) and supplied:
            tokens = [command.strip(), *[str(item) for item in supplied]]
        elif isinstance(supplied, list):
            tokens = tokenize_command(command)
        else:
            tokens = tokenize_command(command)

        if not tokens:
            raise CommandExecutionError(command, "no command was provided")

        if len(tokens) > MAX_ARGUMENTS:
            raise CommandExecutionError(
                command,
                f"too many arguments ({len(tokens)} > {MAX_ARGUMENTS})",
            )
        oversized = next((item for item in tokens if len(item) > MAX_ARGUMENT_CHARS), None)
        if oversized is not None:
            raise CommandExecutionError(
                command,
                f"one argument is longer than {MAX_ARGUMENT_CHARS} characters",
            )
        for token in tokens:
            if "\x00" in token:
                raise CommandExecutionError(command, "an argument contains a null byte")
        return tokens

    def _resolve_cwd(self, raw: str | None) -> Any:
        """Containment for the working directory, via the workspace."""

        if not raw or raw == ".":
            return self._workspace.root
        try:
            target = self._workspace.resolve(raw)
        except InvalidPathError as exc:
            raise InvalidPathError(
                f"run_command working directory rejected: {raw!r}",
                user_message=(
                    f"'{raw}' is not a folder inside the open project, so the command "
                    "cannot run there."
                ),
                details={"path": str(raw), "reason": exc.details.get("reason")},
            ) from exc
        if not target.is_dir():
            raise InvalidPathError(
                f"run_command working directory is not a directory: {raw}",
                user_message=f"'{raw}' is not a folder in this project.",
                details={"path": str(raw)},
            )
        return target

    def _timeout(self, arguments: dict[str, Any]) -> float:
        raw = arguments.get("timeout_seconds")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return self._default_timeout
        return min(max(float(raw), MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)

    def _cancelled(self) -> bool:
        return self._cancel is not None and self._cancel.is_set()

    def classify(self, argv: list[str]) -> PolicyVerdict:
        """Public classification hook, used by the GUI to label the risk."""

        return classify_command(argv, workspace_root=str(self._workspace.root))

    # -- execution -------------------------------------------------------
    def run(self, arguments: dict[str, Any]) -> ToolResult:
        argv = self._argv(arguments)
        verdict = self.classify(argv)

        if not verdict.permitted:
            raise CommandDeniedError(
                " ".join(argv),
                verdict.reason,
                rule=verdict.rule,
                details={"risk": verdict.risk_label},
            )

        cwd = self._resolve_cwd(
            arguments.get("cwd") if isinstance(arguments.get("cwd"), str) else None
        )
        timeout = self._timeout(arguments)
        started = time.perf_counter()

        creation_flags = 0
        if sys.platform == "win32":
            # Keep a console-less child: a GUI app must not flash console
            # windows, and the pipes carry everything we need.
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        if self._sandbox_runner is not None:
            # The command runs inside the OS-enforced boundary (M3B-S1.4).
            # The policy is built from the *approved* argv, so nothing about
            # the execution can be changed after the approval, and a refusal
            # is a failure -- never a fall back to an ordinary subprocess.
            from quantsmind_agent.sandbox.policy import (
                SandboxPolicy,
            )

            policy = SandboxPolicy(
                workspace_root=cwd,
                allowed_command=tuple(argv),
                timeout_seconds=timeout,
                max_output_bytes=self._max_output_chars,
                max_child_processes=self._sandbox_max_child_processes,
                required_isolation=self._sandbox_isolation,
                reason="run_command via the sandbox",
            )
            result = self._sandbox_runner.run(policy, cancel_event=self._cancel)
            if not result.isolation_verified:
                raise CommandExecutionError(
                    " ".join(argv),
                    (
                        "the command was not executed: the sandbox could not establish "
                        f"the required isolation ({result.failure_reason})"
                    ),
                    details={"exit_code": None, "isolation": result.established_level},
                )
            return ToolResult.success(
                self.spec.name,
                (
                    f"`{' '.join(argv)}` exited with code {result.exit_code} "
                    f"in {result.duration_seconds:.1f}s"
                ),
                {
                    "command": " ".join(argv),
                    "argv": argv,
                    "cwd": str(cwd),
                    "exit_code": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "timed_out": result.timed_out,
                    "duration_seconds": round(result.duration_seconds, 2),
                    "isolation": result.established_level,
                    "sandboxed": True,
                    "risk": verdict.command_class.value,
                    "risk_label": verdict.risk_label,
                    "policy_rule": verdict.rule,
                },
            )

        try:
            # argv list, shell=False, and the policy has already approved it.
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=sanitized_environment(self._redact_env),
                creationflags=creation_flags,
            )
        except FileNotFoundError as exc:
            raise CommandExecutionError(
                " ".join(argv),
                f"'{argv[0]}' was not found on this machine",
                details={"exit_code": None},
            ) from exc
        except OSError as exc:
            raise CommandExecutionError(
                " ".join(argv),
                f"the process could not be started ({exc.strerror or exc})",
            ) from exc

        # On Windows, assign the child to a Job Object with KILL_ON_JOB_CLOSE
        # so that cancellation or timeout terminates the entire process tree,
        # not just the direct child.
        self._job = _create_job_object()
        if self._job is not None:
            handle = getattr(process, "_handle", None)
            if handle is not None:
                assigned = _assign_process_to_job(self._job, int(handle))
                if not assigned:
                    # Assignment can fail when the caller already runs inside a
                    # job that disallows nesting. Degrade path: cancellation
                    # still terminates the direct child; the job handle is
                    # released in the finally block below.
                    logger.warning(
                        "could not assign process to Job Object; "
                        "process-tree kill falls back to direct child only"
                    )

        try:
            out_reader = CappedReader(process.stdout, self._max_output_chars)
            err_reader = CappedReader(process.stderr, self._max_output_chars)
            out_reader.start()
            err_reader.start()

            exit_code = self._wait(process, argv, timeout)
            elapsed = time.perf_counter() - started
            out_reader.join(timeout=2)
            err_reader.join(timeout=2)

            stdout = out_reader.text
            stderr = err_reader.text
            logger.info(
                "run_command finished: exit=%s elapsed=%.2fs class=%s",
                exit_code,
                elapsed,
                verdict.command_class.value,
            )
            self._audit_record(
                argv=argv,
                cwd=cwd,
                exit_code=exit_code,
                elapsed=elapsed,
                verdict=verdict,
                timed_out=False,
            )

            return ToolResult.success(
                self.spec.name,
                f"`{' '.join(argv)}` exited with code {exit_code} in {elapsed:.1f}s",
                {
                    "command": " ".join(argv),
                    "argv": argv,
                    "cwd": str(cwd),
                    "exit_code": exit_code,
                    "stdout": stdout,
                    "stderr": stderr,
                    **_truncation_metadata("stdout", out_reader),
                    **_truncation_metadata("stderr", err_reader),
                    "timed_out": False,
                    "duration_seconds": round(elapsed, 2),
                    "risk": verdict.command_class.value,
                    "risk_label": verdict.risk_label,
                    "policy_rule": verdict.rule,
                },
            )
        finally:
            # Release the Job Object handle on every path -- normal completion,
            # cancellation, timeout or error -- so handles never leak and no
            # process tree is left bound to a live job at shutdown.
            self._close_job()

    def _audit_record(
        self,
        *,
        argv: list[str],
        cwd: Any,
        exit_code: int | None,
        elapsed: float,
        verdict: PolicyVerdict,
        timed_out: bool,
    ) -> None:
        """Persist that a command ran. Names and outcomes only, never output.

        Recorded after the fact, so it is not a gate -- but it is written
        durably, because a command whose existence the trail lost is a command
        no one can account for later.
        """

        if self._audit is None:
            return
        outcome = "timed_out" if timed_out else ("succeeded" if exit_code == 0 else "failed")
        try:
            self._audit.record(
                AuditEventType.COMMAND_EXECUTED,
                decision=outcome,
                outcome=outcome,
                reason_code=verdict.rule or "command_executed",
                subject=" ".join(str(part) for part in argv),
                detail=(
                    f"exit={exit_code} in {elapsed:.2f}s cwd={cwd} "
                    f"class={verdict.command_class.value}"
                ),
                durable=True,
            )
        except AuditPersistenceError as exc:
            logger.warning("command execution was not audited: %s", exc)

    def _wait(self, process: Any, argv: list[str], timeout: float) -> int:
        """Wait for the child, honouring both the timeout and cancellation."""

        deadline = time.monotonic() + timeout
        while True:
            try:
                process.wait(timeout=_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            else:
                return int(process.returncode or 0)

            if self._cancelled():
                self._terminate(process)
                raise CommandExecutionError(
                    " ".join(argv),
                    "it was cancelled before it finished",
                    details={"cancelled": True},
                )
            if time.monotonic() >= deadline:
                self._terminate(process)
                raise CommandTimeoutError(" ".join(argv), timeout)

    def _close_job(self) -> None:
        """Release the Job Object handle exactly once.

        Closing the handle terminates every process still assigned to the job
        on Windows (KILL_ON_JOB_CLOSE). Idempotent: a second call is a no-op.
        """

        job = self._job
        self._job = None
        if job is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(job)
            except Exception:  # pragma: no cover - defensive
                logger.warning("Failed to close Job Object handle", exc_info=True)

    def _terminate(self, process: Any) -> None:
        """Ask the child to stop, then insist.

        On Windows, closing the Job Object handle terminates every process
        assigned to the job (due to KILL_ON_JOB_CLOSE), which prevents orphan
        grandchildren after cancellation or timeout. The direct process is
        terminated/killed as a fallback on all platforms.
        """

        # Close the Job Object FIRST, before the direct-process check below.
        # Even when the direct child has already exited, its grandchildren may
        # still be running; closing the job kills them. Closing the handle is
        # idempotent, so a subsequent finally block is harmless.
        self._close_job()
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:  # pragma: no cover - already gone
            return
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):  # pragma: no cover - already gone
                process.kill()
            try:
                process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
                logger.warning("Command did not exit after kill; leaving it alone")


def build_command_tools(
    workspace: Workspace,
    *,
    cancel_event: threading.Event | None = None,
    default_timeout: float = 120.0,
    redact_env: tuple[str, ...] = (),
    max_output_chars: int = MAX_OUTPUT_CHARS,
    audit: SecurityAuditLog | None = None,
) -> list[Any]:
    """Instantiate the v0.2 command tool bound to ``workspace``.

    ``max_output_chars`` is the per-stream cap on captured output, validated by
    the configuration model before it reaches here; the default keeps the
    constant's value so a caller that never configures it is unaffected.
    """

    return [
        RunCommandTool(
            workspace,
            cancel_event=cancel_event,
            default_timeout=default_timeout,
            redact_env=redact_env,
            max_output_chars=max_output_chars,
            audit=audit,
        )
    ]


def run_subprocess(
    argv: list[str],
    cwd: str | Path,
    timeout_seconds: float,
    max_output_chars: int,
) -> tuple[int, str, str]:
    """Run a command via subprocess.Popen and return (exit_code, stdout, stderr).

    Used by the GUI command runner so that subprocess.Popen is not called
    directly from gui/shell/ (which is excluded from importing filesystem
    modules by the import-boundary tests).
    """
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=sanitized_environment(),
            creationflags=creation_flags,
        )
    except FileNotFoundError:
        return 1, "", f"'{argv[0]}' was not found on this machine"
    except OSError as exc:
        return 1, "", f"the process could not be started: {exc}"

    out_reader = CappedReader(process.stdout, max_output_chars)
    err_reader = CappedReader(process.stderr, max_output_chars)
    out_reader.start()
    err_reader.start()

    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            logger.warning("Command did not exit after kill; leaving it alone")

    out_reader.join(timeout=2)
    err_reader.join(timeout=2)
    return process.returncode or 0, out_reader.text, err_reader.text
