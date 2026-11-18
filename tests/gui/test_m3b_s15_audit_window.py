"""WP3 shell wiring: the window reports, and hands over, the store it really holds.

A sink that works in isolation is not a feature until the application reads it
and the operator can act on it. These tests drive the real :class:`MainWindow`
over a real :class:`ApplicationContext`; only the save dialog is scripted,
because a native modal cannot be answered by a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is required for GUI tests")

from PySide6.QtWidgets import QPushButton

from quantsmind_agent.gui.shell import window as window_module
from quantsmind_agent.gui.shell.window import MainWindow
from quantsmind_agent.security.audit import AuditEventType
from tests.gui.harness import GuiHarness

pytestmark = pytest.mark.gui


class _ScriptedDialog:
    """Stand-in for the native save dialog, returning whatever the test decides."""

    destination = ""

    @classmethod
    def getSaveFileName(cls, *args: object, **kwargs: object) -> tuple[str, str]:
        return cls.destination, "JSON (*.json)"


def _export_button(window: MainWindow) -> QPushButton:
    for button in window.control_center.findChildren(QPushButton):
        if button.text() == "Export audit evidence":
            return button
    raise AssertionError("the Control Center offers no audit export action")


@pytest.fixture
def scripted_dialog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[type[_ScriptedDialog], Path]:
    dialog = type("_Dialog", (_ScriptedDialog,), {"destination": ""})
    monkeypatch.setattr(window_module, "QFileDialog", dialog)
    return dialog, tmp_path


def _records_shown(window: MainWindow) -> int:
    shown = window.control_center.describe()["audit_records"]
    assert isinstance(shown, int)
    return shown


class TestAuditStoreInShell:
    def test_the_control_center_counts_the_records_that_are_on_disk(
        self, gui_harness: GuiHarness
    ) -> None:
        window = gui_harness.window
        context = gui_harness.context
        reported = _records_shown(window)
        assert reported == context.audit_sink.status().records

        context.security_audit.record(
            AuditEventType.SECURITY_POLICY_DENIED, subject="a-tool", decision="denied"
        )
        window._refresh_control_center()

        state = window.control_center.describe()
        assert _records_shown(window) == reported + 1
        assert state["audit_chain_ok"] is True
        assert state["audit_degraded"] is False

    def test_no_copy_of_the_trail_exists_until_the_operator_asks(
        self, gui_harness: GuiHarness
    ) -> None:
        data_dir = gui_harness.context.paths.data_dir
        window = gui_harness.window
        window.close()

        stray = [path.name for path in data_dir.glob("audit-export*.json")]
        assert stray == []

    def test_an_export_lands_where_the_operator_points_and_carries_the_chain(
        self, gui_harness: GuiHarness, scripted_dialog: tuple[type[_ScriptedDialog], Path]
    ) -> None:
        dialog, tmp_path = scripted_dialog
        window = gui_harness.window
        destination = tmp_path / "handed-over.json"
        dialog.destination = str(destination)

        _export_button(window).click()

        assert window.notification_bar.current_text() != ""
        payload = json.loads(destination.read_text(encoding="utf-8"))
        assert payload["records"] == len(payload["events"])
        assert payload["records"] >= 1
        assert payload["chain"]["ok"] is True
        assert "store" in payload

    def test_a_cancelled_dialog_writes_nothing(
        self, gui_harness: GuiHarness, scripted_dialog: tuple[type[_ScriptedDialog], Path]
    ) -> None:
        dialog, tmp_path = scripted_dialog
        window = gui_harness.window
        dialog.destination = ""

        _export_button(window).click()

        assert window.notification_bar.current_text() == (
            "Audit export cancelled. Nothing was written."
        )
        assert [path for path in tmp_path.rglob("*") if "audit-export" in path.name] == []

    def test_an_unwritable_destination_is_reported_instead_of_raising(
        self, gui_harness: GuiHarness, scripted_dialog: tuple[type[_ScriptedDialog], Path]
    ) -> None:
        dialog, tmp_path = scripted_dialog
        window = gui_harness.window
        dialog.destination = str(tmp_path)

        _export_button(window).click()

        assert window.notification_bar.current_text().startswith("Audit export failed")
        assert window.notification_bar.kind == "error"
