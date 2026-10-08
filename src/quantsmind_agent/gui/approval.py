"""Human approval for ASK-permission tool calls.

Threading contract
------------------
The agent loop runs on a :class:`~quantsmind_agent.gui.worker.AgentWorker`
thread, so :meth:`PermissionBroker.require` -- and therefore this approval
callback -- is invoked *off* the GUI thread. The GUI thread must never block on
it, and the worker thread must wait for the answer.

:class:`ApprovalBridge` owns that handover:

* the worker calls :meth:`ApprovalBridge.request`, which emits a queued Qt
    signal (delivered on the GUI thread) and then waits on a
    :class:`threading.Event`,
* the GUI thread shows :class:`ApprovalDialog`, then calls
    :meth:`ApprovalBridge.resolve`,
* ``resolve`` stores the answer and sets the event, releasing the worker.

The wait is a bounded poll loop rather than an indefinite ``wait()`` so that a
window close, a session reset or a cancel cannot leave the worker blocked
forever.

Because the answer is an :class:`~quantsmind_agent.permissions.ApprovalOutcome`
rather than a boolean, ``Allow for Session`` is expressible, and a denial is a
final ``DENY`` -- it is never upgraded into an allow anywhere below this module.
"""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.command_policy import CommandClass, classify_command
from quantsmind_agent.permissions import ApprovalOutcome, Decision, Permission, PermissionRequest
from quantsmind_agent.version import APP_NAME

__all__ = [
    "ApprovalBridge",
    "ApprovalDialog",
    "ApprovalPrompt",
    "build_prompt",
    "dialog_title",
    "format_prompt",
]

#: How often a waiting worker re-checks for an answer. Short enough that a
#: cancel feels immediate, long enough not to spin.
_POLL_SECONDS = 0.05


class ApprovalPrompt:
    """Everything the dialog needs, derived from one :class:`PermissionRequest`.

    Built on the GUI thread from a payload the worker handed over, so the
    dialog never touches the permission layer directly and stays testable
    without Qt.
    """

    __slots__ = (
        "details",
        "operation",
        "permission",
        "reason",
        "request",
        "risk",
        "risk_label",
        "summary",
        "target",
        "title",
        "tool",
        "workspace",
    )

    def __init__(
        self,
        request: PermissionRequest,
        *,
        workspace: str = "",
        risk: str = "",
        risk_label: str = "",
    ) -> None:
        self.request = request
        self.tool = request.tool
        self.permission = request.permission
        self.operation = _OPERATION_LABELS.get(request.permission, request.permission.value)
        self.summary = request.summary
        self.target = request.target
        self.details = dict(request.details)
        self.reason = str(self.details.get("reason") or "")
        self.workspace = workspace
        self.risk = risk
        self.risk_label = risk_label
        self.title = f"Allow {request.tool}?"

    @property
    def is_command(self) -> bool:
        return self.permission is Permission.EXECUTE_COMMAND

    @property
    def is_delete(self) -> bool:
        return self.permission is Permission.WRITE_WORKSPACE and self.tool == "delete_file"

    @property
    def blocking_warning(self) -> str:
        """Extra warning text for the operations that deserve one."""

        if (
            self.permission is Permission.EXECUTE_COMMAND
            and self.risk == CommandClass.DESTRUCTIVE.value
        ):
            return (
                "This command was classified as high risk. It can change or destroy data "
                "outside this project and may not be reversible."
            )
        if self.is_delete:
            return "Deleting a file cannot be undone from the agent."
        return ""

    def argument_lines(self) -> list[str]:
        """Human-readable arguments, with credential-shaped values withheld."""

        lines: list[str] = []
        for key, value in self.details.items():
            if key in {"reason", "cwd"}:
                continue
            rendered = _render(value)
            if key == "content":
                rendered = f"{rendered[:200]}{'...' if len(str(value)) > 200 else ''}"
            if key == "command" and self.is_command:
                rendered = str(value)
            lines.append(f"{key}: {rendered}")
        return lines


_OPERATION_LABELS: dict[Permission, str] = {
    Permission.READ_WORKSPACE: "Read files in this project",
    Permission.WRITE_WORKSPACE: "Modify a file in this project",
    Permission.EXECUTE_COMMAND: "Run a command on this computer",
    Permission.GIT: "Inspect Git state",
    Permission.NETWORK: "Access the network",
}


def _render(value: Any) -> str:
    """Render one argument for display, never echoing a credential."""

    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    text = str(value)
    lowered = text.lower()
    if any(marker in lowered for marker in ("sk-", "bearer ", "token=", "api_key=")):
        return "(withheld)"
    return text


def build_prompt(
    request: PermissionRequest,
    *,
    workspace: str = "",
) -> ApprovalPrompt:
    """Turn a permission request into dialog-ready data.

    The risk label is recomputed here from the same
    :func:`~quantsmind_agent.command_policy.classify_command` the tool used, so
    the dialog and the tool can never disagree about how risky a command is.
    """

    risk = ""
    risk_label = ""
    if request.permission is Permission.EXECUTE_COMMAND:
        command = request.details.get("command")
        if isinstance(command, str) and command:
            argv = [command, *[str(a) for a in request.details.get("arguments") or []]]
            verdict = classify_command(argv, workspace_root=workspace or None)
            risk = verdict.command_class.value
            risk_label = verdict.risk_label
    return ApprovalPrompt(request, workspace=workspace, risk=risk, risk_label=risk_label)


def format_prompt(prompt: ApprovalPrompt) -> str:
    """Plain-text rendering, used by GUI tests and by the CLI when headless."""

    lines = [
        f"{prompt.operation}",
        f"tool      : {prompt.tool}",
        f"target    : {prompt.target or '(none)'}",
        f"workspace : {prompt.workspace or '(none)'}",
        f"risk      : {prompt.risk_label or prompt.permission.value}",
    ]
    if prompt.reason:
        lines.append(f"reason    : {prompt.reason}")
    for line in prompt.argument_lines():
        lines.append(f"  {line}")
    if prompt.blocking_warning:
        lines.append(f"WARNING   : {prompt.blocking_warning}")
    return "\n".join(lines)


class ApprovalBridge(QObject):
    """Marshals an approval request from the worker thread to the GUI thread."""

    #: Emitted on the GUI thread with an :class:`ApprovalPrompt`.
    approval_requested = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pending: threading.Event | None = None
        self._answer: ApprovalOutcome = ApprovalOutcome.DENY
        self._cancelled = False
        self._lock = threading.Lock()

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def cancel_pending(self) -> None:
        """Deny any outstanding request (window closing, session reset, Stop)."""

        with self._lock:
            self._cancelled = True
            self._answer = ApprovalOutcome.DENY
        if self._pending is not None:
            self._pending.set()

    def request(self, prompt: ApprovalPrompt) -> ApprovalOutcome:
        """Called on the **worker** thread. Blocks until the user answers."""

        done = threading.Event()
        with self._lock:
            self._pending = done
            self._answer = ApprovalOutcome.DENY
            self._cancelled = False

        self.approval_requested.emit(prompt)

        try:
            while not done.wait(_POLL_SECONDS):
                pass
        finally:
            with self._lock:
                answer = self._answer if not self._cancelled else ApprovalOutcome.DENY
                self._pending = None
                self._cancelled = False
        return answer

    def resolve(self, outcome: ApprovalOutcome) -> None:
        """Called on the **GUI** thread once the dialog is answered."""

        with self._lock:
            self._answer = outcome
        if self._pending is not None:
            self._pending.set()

    def connect_dialog(self, slot: Any) -> None:
        """Wire the request signal to a GUI-thread slot (queued delivery)."""

        self.approval_requested.connect(slot, Qt.ConnectionType.QueuedConnection)


def decision_for(outcome: ApprovalOutcome) -> Decision:
    """Map an approval outcome onto the decision the permission layer records."""

    return Decision.ALLOW if outcome is not ApprovalOutcome.DENY else Decision.DENY


def dialog_title(prompt: ApprovalPrompt) -> str:
    return f"{APP_NAME} - {prompt.title}"


class ApprovalDialog(QDialog):
    """Modal prompt showing exactly what is about to happen.

    The user sees the operation, the target, the workspace, the arguments, the
    reason the model gave, and the risk classification. For command execution
    the buttons are ``Allow Once`` and ``Deny``; ``Allow for Session`` is offered
    separately so granting it is always a deliberate second click.

    A denial is the default outcome: closing the window, or pressing Escape,
    denies the operation. There is no path from this dialog to "run it anyway"
    other than an explicit Allow button.
    """

    def __init__(self, prompt: ApprovalPrompt, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prompt = prompt
        self.outcome: ApprovalOutcome = ApprovalOutcome.DENY
        self.setWindowTitle(dialog_title(prompt))
        self.setModal(True)
        self.setMinimumWidth(560)
        self._build()

    # -- construction ----------------------------------------------------
    def _build(self) -> None:
        prompt = self._prompt
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        heading = QLabel(f"The agent wants to {prompt.operation.lower()}")
        heading.setObjectName("SectionLabel")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        detail = QLabel(format_prompt(prompt))
        detail.setObjectName("StatusText")
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail.setWordWrap(True)
        layout.addWidget(detail)

        if prompt.blocking_warning:
            warning = QLabel(prompt.blocking_warning)
            warning.setObjectName("ErrorText")
            warning.setWordWrap(True)
            layout.addWidget(warning)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        if prompt.permission is Permission.WRITE_WORKSPACE and not prompt.is_delete:
            self.allow_session_button = QPushButton("Allow for Session")
            self.allow_session_button.setToolTip(
                "Allow this tool without asking again until the conversation, project "
                "or settings change. Delete always asks."
            )
            self.allow_session_button.clicked.connect(self._on_allow_session)
            buttons.addWidget(self.allow_session_button)

        buttons.addStretch(1)

        self.deny_button = QPushButton("Deny")
        self.deny_button.setObjectName("DangerButton")
        self.deny_button.setDefault(True)
        self.deny_button.clicked.connect(self._on_deny)
        buttons.addWidget(self.deny_button)

        label = "Allow Once" if prompt.is_command else "Allow"
        self.allow_button = QPushButton(label)
        self.allow_button.setObjectName("PrimaryButton")
        self.allow_button.clicked.connect(self._on_allow)
        buttons.addWidget(self.allow_button)

        layout.addLayout(buttons)

    # -- outcomes --------------------------------------------------------
    def _finish(self, outcome: ApprovalOutcome) -> None:
        self.outcome = outcome
        self.accept() if outcome is not ApprovalOutcome.DENY else self.reject()

    def _on_allow(self) -> None:
        self._finish(ApprovalOutcome.ALLOW_ONCE)

    def _on_allow_session(self) -> None:
        self._finish(ApprovalOutcome.ALLOW_FOR_SESSION)

    def _on_deny(self) -> None:
        self._finish(ApprovalOutcome.DENY)

    def reject(self) -> None:
        """Escape or closing the window denies the operation."""

        self.outcome = ApprovalOutcome.DENY
        super().reject()

    @property
    def prompt(self) -> ApprovalPrompt:
        return self._prompt

    def button_labels(self) -> list[str]:
        """Visible button texts, in order (used by GUI tests)."""

        return [
            button.text()
            for button in self.findChildren(QPushButton)
            if button.isVisible() or not self.isVisible()
        ]
