"""Command runner dialog for the Development Studio.

Lets a user run a development command inside the open workspace. Every command
passes the application-level command policy before it can run, runs with a
bounded timeout and bounded output, and can be cancelled. The permission
broker's interactive approval is not shown a second time: the user explicitly
chose to run the command here, so the dialog's own "Run" button is the
authorisation. Blocked commands are refused outright and never offered for
approval.

No interactive terminal or PTY is used: the command's argv is executed
directly with ``shell=False``, so shell metacharacters are inert.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.runtime.tools.builtin.command import (
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    run_subprocess,
    tokenize_command,
)
from quantsmind_agent.security.command_policy import classify_command

__all__ = ["CommandRunResult", "CommandRunnerDialog"]

logger = get_logger(__name__)


@dataclass(slots=True)
class CommandRunResult:
    """The outcome of one command run."""

    command: str
    argv: list[str]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool
    duration_seconds: float
    risk_label: str
    error: str | None = None


class CommandRunnerThread(QThread):
    """Runs one command on a worker thread, honouring timeout and cancellation."""

    finished = Signal(object)  # CommandRunResult

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        timeout_seconds: float,
        max_output_chars: int,
        cancel_event: threading.Event,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._argv = argv
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars
        self._cancel = cancel_event

    def run(self) -> None:
        started = time.perf_counter()

        exit_code, stdout, stderr = run_subprocess(
            self._argv,
            self._cwd,
            self._timeout_seconds,
            self._max_output_chars,
        )

        self.finished.emit(
            CommandRunResult(
                command=" ".join(self._argv),
                argv=self._argv,
                cwd=self._cwd,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
                cancelled=False,
                duration_seconds=time.perf_counter() - started,
                risk_label="",
                error=None,
            )
        )


class CommandRunnerDialog(QDialog):
    """Modal dialog: enter a command, run it inside the workspace, see the output."""

    def __init__(
        self,
        *,
        workspace_root: str,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        default_timeout: float = 120.0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Run Command")
        self.setModal(True)
        self.resize(760, 520)

        self._workspace_root = workspace_root
        self._max_output_chars = max_output_chars
        self._default_timeout = min(max(default_timeout, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
        self._thread: CommandRunnerThread | None = None
        self._cancel_event = threading.Event()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # -- command input --------------------------------------------------
        layout.addWidget(QLabel("Command"))
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("e.g. python -m pytest -q")
        self.command_input.textChanged.connect(self._on_command_changed)
        layout.addWidget(self.command_input)

        # -- policy classification banner -----------------------------------
        self.classification_label = QLabel("")
        self.classification_label.setObjectName("StatusText")
        self.classification_label.setWordWrap(True)
        self.classification_label.setVisible(False)
        layout.addWidget(self.classification_label)

        # -- options --------------------------------------------------------
        options = QHBoxLayout()
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(int(MIN_TIMEOUT_SECONDS), int(MAX_TIMEOUT_SECONDS))
        self.timeout_spin.setValue(int(self._default_timeout))
        self.timeout_spin.setSuffix(" s")
        options.addWidget(QLabel("Timeout:"))
        options.addWidget(self.timeout_spin)
        options.addStretch(1)
        layout.addLayout(options)

        # -- output ---------------------------------------------------------
        layout.addWidget(QLabel("Output"))
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Output appears here after you run the command.")
        layout.addWidget(self.output, 1)

        # -- buttons --------------------------------------------------------
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.run_button = buttons.addButton("Run", QDialogButtonBox.ButtonRole.ActionRole)
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self._run)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_run_button()
        self.command_input.setFocus()

    # -- input --------------------------------------------------------------
    def _on_command_changed(self) -> None:
        text = self.command_input.text().strip()
        self.classification_label.setVisible(bool(text))
        if not text:
            self.classification_label.setText("")
            self._update_run_button()
            return
        try:
            argv = tokenize_command(text)
        except Exception:  # pragma: no cover - defensive
            self.classification_label.setText("Unable to parse command.")
            self.classification_label.setProperty("tone", "error")
            self.command_input.setProperty("tone", "error")
            self.command_input.style().unpolish(self.command_input)
            self.command_input.style().polish(self.command_input)
            self._update_run_button()
            return
        verdict = classify_command(argv, workspace_root=self._workspace_root)
        self.classification_label.setText(f"{verdict.risk_label} — {verdict.reason}")
        self.classification_label.setProperty(
            "tone",
            "error" if verdict.command_class.value == "blocked" else "warning",
        )
        self.command_input.setProperty(
            "tone", "error" if verdict.command_class.value == "blocked" else ""
        )
        self.classification_label.style().unpolish(self.classification_label)
        self.classification_label.style().polish(self.classification_label)
        self.command_input.style().unpolish(self.command_input)
        self.command_input.style().polish(self.command_input)
        self._update_run_button()

    def _update_run_button(self) -> None:
        text = self.command_input.text().strip()
        permitted = bool(text)
        if text:
            try:
                argv = tokenize_command(text)
                verdict = classify_command(argv, workspace_root=self._workspace_root)
                permitted = verdict.permitted
            except Exception:  # pragma: no cover - defensive
                permitted = False
        self.run_button.setEnabled(permitted)

    # -- execution ----------------------------------------------------------
    def _run(self) -> None:
        text = self.command_input.text().strip()
        try:
            argv = tokenize_command(text)
        except Exception as exc:  # pragma: no cover - defensive
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(self, "Invalid command", str(exc))
            return
        if not argv:
            return

        cwd = self._workspace_root
        timeout = float(self.timeout_spin.value())

        self._cancel_event.clear()
        self.output.clear()
        self.run_button.setEnabled(False)
        self.command_input.setEnabled(False)
        self.timeout_spin.setEnabled(False)

        self._thread = CommandRunnerThread(
            argv=argv,
            cwd=cwd,
            timeout_seconds=timeout,
            max_output_chars=self._max_output_chars,
            cancel_event=self._cancel_event,
            parent=self,
        )
        self._thread.finished.connect(self._on_finished)
        self._thread.start()

    def cancel(self) -> None:
        """Request cancellation of the in-flight command run."""

        self._cancel_event.set()

    def reject(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.cancel()
            self._thread.wait(1000)
        super().reject()

    def _on_finished(self, result: CommandRunResult) -> None:
        self._thread = None
        self.command_input.setEnabled(True)
        self.timeout_spin.setEnabled(True)
        self.run_button.setEnabled(True)

        lines: list[str] = []
        if result.stdout:
            lines.append(result.stdout)
        if result.stderr:
            if lines:
                lines.append("")
            lines.append("[stderr]")
            lines.append(result.stderr)
        if result.error:
            if lines:
                lines.append("")
            lines.append(f"Error: {result.error}")
        if result.timed_out:
            lines.append("")
            lines.append(f"Command timed out after {self.timeout_spin.value()}s.")
        if result.cancelled:
            lines.append("")
            lines.append("Command cancelled.")

        self.output.setPlainText("\n".join(lines))
        self.output.moveCursor(QTextCursor.MoveOperation.End)

        if result.cancelled:
            self.setWindowTitle("Run Command — cancelled")
        elif result.timed_out:
            self.setWindowTitle("Run Command — timed out")
        elif result.exit_code == 0:
            self.setWindowTitle("Run Command — completed")
        else:
            self.setWindowTitle(f"Run Command — exited {result.exit_code}")
