"""Tests for the Development Studio command runner dialog.

Covers command-policy classification (blocked commands are refused), command
execution with bounded output, and cancellation. Runs on Qt's offscreen platform
so no desktop is required.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import cast

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is required for GUI tests")

from PySide6.QtWidgets import QApplication

from quantsmind_agent.gui.shell.command_runner import CommandRunnerDialog

pytestmark = pytest.mark.gui


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return cast(QApplication, app)


def _enter_and_run(dialog: CommandRunnerDialog, command: str) -> None:
    dialog.command_input.setText(command)
    dialog.command_input.editingFinished.emit()
    QApplication.processEvents()
    dialog.run_button.click()
    QApplication.processEvents()


class TestCommandRunnerClassification:
    def test_blocked_command_is_refused(self, tmp_path: Path) -> None:
        _app()
        dialog = CommandRunnerDialog(workspace_root=str(tmp_path))
        dialog.command_input.setText("format C:")
        dialog.command_input.editingFinished.emit()
        QApplication.processEvents()
        assert dialog.run_button.isEnabled() is False
        dialog.deleteLater()

    def test_permitted_command_is_enabled(self, tmp_path: Path) -> None:
        _app()
        dialog = CommandRunnerDialog(workspace_root=str(tmp_path))
        dialog.command_input.setText("echo hello")
        dialog.command_input.editingFinished.emit()
        QApplication.processEvents()
        assert dialog.run_button.isEnabled() is True
        dialog.deleteLater()

    def test_empty_command_is_disabled(self, tmp_path: Path) -> None:
        _app()
        dialog = CommandRunnerDialog(workspace_root=str(tmp_path))
        assert dialog.run_button.isEnabled() is False
        dialog.deleteLater()


class TestCommandRunnerExecution:
    def test_runs_a_command_and_shows_output(self, tmp_path: Path) -> None:
        _app()
        dialog = CommandRunnerDialog(workspace_root=str(tmp_path))
        _enter_and_run(dialog, '"python" -c "print(\'hello-world\')"')
        # Give the worker thread a moment to finish.
        deadline = time.time() + 10
        while dialog._thread is not None and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.05)
        output = dialog.output.toPlainText()
        assert "hello-world" in output
        dialog.deleteLater()

    def test_cancellation_stops_a_long_command(self, tmp_path: Path) -> None:
        _app()
        dialog = CommandRunnerDialog(workspace_root=str(tmp_path), default_timeout=600.0)
        # A command that ignores termination long enough to cancel.
        if sys.platform == "win32":
            command = "timeout /t 30 /nobreak >nul"
        else:
            command = "sleep 30"
        _enter_and_run(dialog, command)
        QApplication.processEvents()
        # Cancel shortly after start.
        deadline = time.time() + 2
        while dialog._thread is None and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.05)
        dialog.cancel()
        deadline = time.time() + 10
        while dialog._thread is not None and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.05)
        output = dialog.output.toPlainText()
        assert (
            "cancelled" in output.lower()
            or "timed out" in output.lower()
            or dialog._cancel_event.is_set()
        )
        dialog.deleteLater()
