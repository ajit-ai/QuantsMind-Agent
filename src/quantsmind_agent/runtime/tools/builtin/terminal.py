"""Interactive terminal tool (WP1 of v0.12.0).

Executes a program inside a pseudo-terminal so that interactive programs
(REPLs, prompts, screen-oriented tools) see a real terminal rather than a
pipe. Unlike :mod:`quantsmind_agent.runtime.tools.builtin.command` (which runs
one command to completion and captures its output), the terminal is
*bidirectional* and *stateful*: the caller writes stdin and reads
stdout/stderr interleaved as they arrive, can resize the terminal, and can
cancel mid-run.

Design notes
------------
**No shell, ever.** As with run_command, ``shell=False`` with an explicit argv
list is the primary safety property. Shell metacharacters are not interpreted;
they arrive at the target program as ordinary argument text.

**Two independent gates.** :meth:`run` refuses anything the policy marks
``BLOCKED`` before touching the process table. The permission registry
separately requires an approval (``ASK``) for ``EXECUTE_COMMAND``. Passing the
policy is not permission, and permission is not a bypass of the policy.

**Bounded everything.** Output is capped, the runtime is capped, and the
argument vector is capped. A terminal that prints forever or hangs forever
cannot exhaust memory or wedge the GUI.

**Credentials are not inherited.** Model-provider keys are stripped from the
child environment.

**Stateful sessions.** A ``start`` call returns a session id; subsequent
``write``/``resize``/``close`` calls target that session. Sessions are owned by
the tool instance and are terminated when the tool is discarded or the session
is explicitly closed.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import (
    CommandDeniedError,
    CommandExecutionError,
    InvalidPathError,
    QuantsMindError,
)
from quantsmind_agent.runtime.tools import ToolResult, ToolSpec
from quantsmind_agent.runtime.tools.builtin.command import (
    MAX_ARGUMENT_CHARS,
    MAX_ARGUMENTS,
    tokenize_command,
)
from quantsmind_agent.runtime.tools.builtin.pty_backend import (
    PtyBackend,
    PtySession,
    PtyUnavailable,
)
from quantsmind_agent.security.audit import (
    AuditEventType,
    AuditPersistenceError,
    SecurityAuditLog,
)
from quantsmind_agent.security.command_policy import PolicyVerdict, classify_command
from quantsmind_agent.security.permissions import Permission
from quantsmind_agent.services.workspace.containment import Workspace

__all__ = [
    "MAX_OUTPUT_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "TerminalTool",
    "build_terminal_tools",
]

logger = get_logger(__name__)

#: Upper bounds, so a misbehaving terminal cannot exhaust memory or wedge the GUI.
MAX_TIMEOUT_SECONDS = 600.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_OUTPUT_CHARS = 50_000

#: How often the terminal pump wakes to check for output / exit / cancel.
_PUMP_SECONDS = 0.05


def _now() -> float:
    return time.time()


class TerminalTool:
    """Run a program inside an interactive pseudo-terminal.

    The tool is stateful: a ``start`` action creates a session and returns a
    session id; ``write`` sends input to that session; ``resize`` changes the
    terminal dimensions; ``close`` terminates the session. Output is returned
    incrementally so the caller can stream it to a terminal widget.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        cancel_event: threading.Event | None = None,
        default_timeout: float = 120.0,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        audit: SecurityAuditLog | None = None,
    ) -> None:
        self._workspace = workspace
        self._cancel = cancel_event
        self._default_timeout = min(max(default_timeout, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
        self._max_output_chars = max_output_chars
        self._audit = audit
        self._backend = PtyBackend()
        self._sessions: dict[str, PtySession] = {}
        self._session_lock = threading.Lock()
        self._next_session = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="terminal",
            description=(
                "Run a program inside an interactive pseudo-terminal inside the "
                "workspace. Unlike run_command, the terminal is bidirectional: "
                "use action='start' to launch, action='write' to send stdin, "
                "action='resize' to change dimensions, and action='close' to "
                "terminate. Output is returned incrementally and may be empty "
                "while the program is running. Shell metacharacters are not "
                "interpreted; pass arguments as a list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The program to run, e.g. 'pwsh' or 'bash'.",
                    },
                    "arguments": {
                        "type": "array",
                        "description": "Optional argument list, used verbatim.",
                        "items": {"type": "string"},
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Workspace-relative directory to run in. Default: the "
                            "project root. Absolute paths and '..' are rejected."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": (
                            f"Stop the terminal after this many seconds "
                            f"({MIN_TIMEOUT_SECONDS:g}-{MAX_TIMEOUT_SECONDS:g})."
                        ),
                    },
                    "columns": {
                        "type": "integer",
                        "description": "Terminal width in characters (default 80).",
                    },
                    "rows": {
                        "type": "integer",
                        "description": "Terminal height in characters (default 24).",
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "Session action: 'start' (default), 'write', 'resize' or 'close'."
                        ),
                        "enum": ["start", "write", "resize", "close"],
                    },
                    "input": {
                        "type": "string",
                        "description": "Text to send to the terminal stdin (write action).",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session id returned by a prior start call.",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            permission=Permission.EXECUTE_COMMAND,
            summary_template="terminal {command}",
            read_only=False,
            guard=self._refuse_if_blocked,
        )

    # -- permission / policy guard ----------------------------------------
    def _refuse_if_blocked(self, arguments: dict[str, Any]) -> None:
        """Policy gate, run by the registry before the user is asked.

        A blocked command must never reach an approval dialog.
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

    # -- argv / cwd / timeout helpers -------------------------------------
    def _argv(self, arguments: dict[str, Any]) -> list[str]:
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
        if not raw or raw == ".":
            return self._workspace.root
        try:
            target = self._workspace.resolve(raw)
        except InvalidPathError as exc:
            raise InvalidPathError(
                f"terminal working directory rejected: {raw!r}",
                user_message=(
                    f"'{raw}' is not a folder inside the open project, so the "
                    "terminal cannot run there."
                ),
                details={"path": str(raw), "reason": exc.details.get("reason")},
            ) from exc
        if not target.is_dir():
            raise InvalidPathError(
                f"terminal working directory is not a directory: {raw}",
                user_message=f"'{raw}' is not a folder in this project.",
                details={"path": str(raw)},
            )
        return target

    def _timeout(self, arguments: dict[str, Any]) -> float:
        raw = arguments.get("timeout_seconds")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return self._default_timeout
        return min(max(float(raw), MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)

    def classify(self, argv: list[str]) -> PolicyVerdict:
        """Public classification hook, used by the GUI to label the risk."""
        return classify_command(argv, workspace_root=str(self._workspace.root))

    # -- run --------------------------------------------------------------
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
        action = arguments.get("action", "start")
        if action not in {"start", "write", "resize", "close"}:
            action = "start"

        try:
            if action == "start":
                return self._start(argv, cwd, timeout, arguments, verdict)
            if action == "write":
                return self._write(arguments, verdict)
            if action == "resize":
                return self._resize(arguments, verdict)
            return self._close(arguments, verdict)
        except CommandDeniedError:
            raise
        except PtyUnavailable as exc:
            raise CommandExecutionError(
                " ".join(argv),
                exc.message,
                details={"code": "pty_unavailable", "reason": exc.message},
            ) from exc
        except QuantsMindError as exc:
            raise CommandExecutionError(
                " ".join(argv),
                exc.message,
                details=exc.details or {},
            ) from exc

    # -- session actions --------------------------------------------------
    def _start(
        self,
        argv: list[str],
        cwd: Any,
        timeout: float,
        arguments: dict[str, Any],
        verdict: PolicyVerdict,
    ) -> ToolResult:
        columns = int(arguments.get("columns") or 80)
        rows = int(arguments.get("rows") or 24)

        try:
            session = self._backend.spawn(
                argv=argv,
                cwd=cwd,
                timeout_seconds=timeout,
                columns=columns,
                rows=rows,
                cancel_event=self._cancel,
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
                f"the process could not be started ({exc})",
            ) from exc

        session_id = self._register_session(session)

        # Read any initial output immediately (non-blocking).
        initial = session.read(timeout=0.1)
        output_text = initial.text if initial else ""
        exited = initial.exited if initial else False
        exit_code = initial.exit_code if initial else None

        self._audit_record(
            argv=argv,
            cwd=cwd,
            session_id=session_id,
            exit_code=exit_code,
            verdict=verdict,
            action="start",
        )

        return ToolResult.success(
            self.spec.name,
            f"terminal session started (pid {session.pid})",
            {
                "command": " ".join(argv),
                "argv": argv,
                "cwd": str(cwd),
                "session_id": session_id,
                "pid": session.pid,
                "output": output_text,
                "exited": exited,
                "exit_code": exit_code,
                "columns": session.columns,
                "rows": session.rows,
                "risk": verdict.command_class.value,
                "risk_label": verdict.risk_label,
                "policy_rule": verdict.rule,
            },
        )

    def _write(self, arguments: dict[str, Any], verdict: PolicyVerdict) -> ToolResult:
        session_id = str(arguments.get("session_id", ""))
        session = self._get_session(session_id)
        text = str(arguments.get("input", ""))

        session.write(text.encode("utf-8", errors="replace"))

        # Read whatever output is available right now.
        output = session.read(timeout=0.2)
        output_text = output.text if output else ""
        exited = output.exited if output else False
        exit_code = output.exit_code if output else None

        self._audit_record(
            argv=["<terminal-write>"],
            cwd=self._workspace.root,
            session_id=session_id,
            exit_code=exit_code,
            verdict=verdict,
            action="write",
        )

        return ToolResult.success(
            self.spec.name,
            "input sent to terminal session",
            {
                "session_id": session_id,
                "output": output_text,
                "exited": exited,
                "exit_code": exit_code,
                "columns": session.columns,
                "rows": session.rows,
                "risk": verdict.command_class.value,
                "risk_label": verdict.risk_label,
                "policy_rule": verdict.rule,
            },
        )

    def _resize(self, arguments: dict[str, Any], verdict: PolicyVerdict) -> ToolResult:
        session_id = str(arguments.get("session_id", ""))
        session = self._get_session(session_id)
        columns = int(arguments.get("columns", session.columns))
        rows = int(arguments.get("rows", session.rows))
        session.resize(columns, rows)

        self._audit_record(
            argv=["<terminal-resize>"],
            cwd=self._workspace.root,
            session_id=session_id,
            exit_code=None,
            verdict=verdict,
            action="resize",
        )

        return ToolResult.success(
            self.spec.name,
            "terminal resized",
            {
                "session_id": session_id,
                "columns": session.columns,
                "rows": session.rows,
                "risk": verdict.command_class.value,
                "risk_label": verdict.risk_label,
                "policy_rule": verdict.rule,
            },
        )

    def _close(self, arguments: dict[str, Any], verdict: PolicyVerdict) -> ToolResult:
        session_id = str(arguments.get("session_id", ""))
        session = self._get_session(session_id)
        session.terminate()
        exit_code = session.exit_code

        with self._session_lock:
            self._sessions.pop(session_id, None)

        self._audit_record(
            argv=["<terminal-close>"],
            cwd=self._workspace.root,
            session_id=session_id,
            exit_code=exit_code,
            verdict=verdict,
            action="close",
        )

        return ToolResult.success(
            self.spec.name,
            "terminal session closed",
            {
                "session_id": session_id,
                "exited": True,
                "exit_code": exit_code,
                "risk": verdict.command_class.value,
                "risk_label": verdict.risk_label,
                "policy_rule": verdict.rule,
            },
        )

    # -- session bookkeeping ----------------------------------------------
    def _register_session(self, session: PtySession) -> str:
        with self._session_lock:
            sid = f"term-{self._next_session}"
            self._next_session += 1
            self._sessions[sid] = session
            return sid

    def _get_session(self, session_id: str) -> PtySession:
        with self._session_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise CommandExecutionError(
                "terminal",
                f"no such session: {session_id}",
                details={"code": "terminal_session_not_found", "session_id": session_id},
            )
        return session

    def close_all(self) -> None:
        """Terminate every live session (called on workspace close / shutdown)."""

        with self._session_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                session.terminate()

    # -- audit ------------------------------------------------------------
    def _audit_record(
        self,
        *,
        argv: list[str],
        cwd: Any,
        session_id: str = "",
        exit_code: int | None,
        verdict: PolicyVerdict,
        action: str,
    ) -> None:
        if self._audit is None:
            return
        with contextlib.suppress(AuditPersistenceError):
            self._audit.record(
                AuditEventType.COMMAND_EXECUTED,
                decision="allowed",
                reason_code=verdict.rule,
                detail=(
                    f"terminal {action}: {' '.join(argv)} (session={session_id}, exit={exit_code})"
                ),
                subject=" ".join(argv),
                outcome=(
                    "exited"
                    if exit_code == 0
                    else "nonzero"
                    if exit_code is not None
                    else "running"
                ),
                durable=True,
                session_id=session_id,
                extra={
                    "action": action,
                    "command": " ".join(argv),
                    "cwd": str(cwd),
                    "exit_code": exit_code,
                    "risk": verdict.command_class.value,
                    "risk_label": verdict.risk_label,
                    "policy_rule": verdict.rule,
                },
            )


def build_terminal_tools(
    workspace: Workspace,
    *,
    cancel_event: threading.Event | None = None,
    default_timeout: float = 120.0,
    max_output_chars: int = MAX_OUTPUT_CHARS,
    audit: SecurityAuditLog | None = None,
) -> list[Any]:
    """Build the terminal tool list.

    Returns a single :class:`TerminalTool`. Kept as a list for symmetry with
    :func:`build_command_tools`.
    """

    return [
        TerminalTool(
            workspace,
            cancel_event=cancel_event,
            default_timeout=default_timeout,
            max_output_chars=max_output_chars,
            audit=audit,
        )
    ]
