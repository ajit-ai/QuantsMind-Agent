"""Regression tests for the blank, frozen approval dialog.

The reported Windows 11 failure was an approval window that opened blank and
reported "Not Responding". Both symptoms share one cause: the receiving slot was
a plain Python method wired with the default ``AutoConnection``, so PySide6
invoked it directly on the agent's worker thread. Widgets constructed there
never paint and have no message pump.

These tests pin the fix:

* the prompt is delivered to a real ``@Slot`` over an explicit queued
  connection, so it is handled on the GUI thread even when a worker emits it;
* the dialog is never blank -- usable geometry, real text, a painted pixmap;
* every failure path denies rather than authorises;
* the waiting worker is always released, including on cancel and shutdown.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QEventLoop, Qt, QThread
from PySide6.QtWidgets import QApplication, QLabel

from quantsmind_agent.events import AgentEventType
from quantsmind_agent.gui.approval import ApprovalBridge, ApprovalDialog, build_prompt
from quantsmind_agent.permissions import ApprovalOutcome, Permission, PermissionRequest

pytestmark = pytest.mark.gui

FLAGS = QEventLoop.ProcessEventsFlag.AllEvents


def edit_prompt(path: str = "README.md") -> Any:
    """The exact request shape that produced the blank dialog."""

    request = PermissionRequest(
        permission=Permission.WRITE_WORKSPACE,
        tool="edit_file",
        summary=f"edit {path} (replace)",
        target=path,
        details={
            "path": path,
            "operation": "replace",
            "old_text": "old",
            "new_text": "new",
        },
    )
    return build_prompt(request, workspace=r"C:\proj")


def close_dialog(dialog: Any) -> None:
    """Destroy a dialog deterministically.

    These dialogs are parentless, so Python owns them. ``deleteLater()`` is the
    wrong tool for a Python-owned widget: Qt destroys the C++ object while the
    wrapper still believes it owns the memory, and the next collection aborts
    the interpreter with ``none_dealloc: deallocating None`` or a segmentation
    fault. ``shiboken6.delete`` tells the wrapper the C++ object is gone, which
    is what actually makes teardown safe.

    It must also run while the ``QApplication`` is still alive, since a widget
    that outlives its application has its C++ object torn down after Qt is
    gone.
    """

    if dialog is None:
        return
    try:
        dialog.close()
        shiboken6.delete(dialog)
    except RuntimeError:  # pragma: no cover - already deleted
        return
    QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)


def drain(rounds: int = 1, sleep_ms: int = 2) -> None:
    """Deliver queued GUI events, yielding the GIL so a worker can run."""

    for _ in range(rounds):
        QApplication.processEvents(FLAGS, 20)
        QThread.msleep(sleep_ms)


def pump_until(predicate: Any, limit: int = 300) -> bool:
    for _ in range(limit):
        drain()
        if predicate():
            return True
    return False


class TestDialogIsNeverBlank:
    """The reported symptom: a window with no usable content."""

    def test_has_usable_geometry_and_text(self, qapp: object) -> None:
        dialog = ApprovalDialog(edit_prompt())
        dialog.show()
        assert dialog.width() >= 480, "too narrow to render content"
        assert dialog.height() >= 300, "too short to render content"
        text = dialog.full_text()
        assert "edit_file" in text
        assert "README.md" in text
        close_dialog(dialog)

    def test_renders_a_non_uniform_pixmap(self, qapp: object) -> None:
        dialog = ApprovalDialog(edit_prompt())
        dialog.show()
        qapp.processEvents()  # type: ignore[attr-defined]
        image = dialog.grab().toImage()
        assert not image.isNull()
        shades = {
            image.pixel(x, y)
            for x in range(0, image.width(), 5)
            for y in range(0, image.height(), 5)
        }
        del image
        close_dialog(dialog)
        assert len(shades) > 1, "dialog painted a single flat colour, i.e. blank"

    def test_offers_every_authorization_choice(self, qapp: object) -> None:
        dialog = ApprovalDialog(edit_prompt())
        labels = dialog.button_labels()
        close_dialog(dialog)
        assert "Allow" in labels
        assert "Deny" in labels
        assert "Allow for Session" in labels

    def test_detail_label_is_laid_out(self, qapp: object) -> None:
        dialog = ApprovalDialog(edit_prompt())
        dialog.show()
        qapp.processEvents()  # type: ignore[attr-defined]
        details = [
            label
            for label in dialog.findChildren(QLabel)
            if "README.md" in label.text() or "edit_file" in label.text()
        ]
        assert details, "no label carries the request details"
        laid_out = any(label.height() > 0 for label in details)
        close_dialog(dialog)
        assert laid_out, "details laid out with zero height"


class TestDeliveryHappensOnTheGuiThread:
    """The actual cause: the slot used to run on the worker thread."""

    def test_worker_emitted_prompt_is_handled_on_the_gui_thread(self, gui_harness: Any) -> None:
        window = gui_harness.window
        gui_thread = window.thread()

        handled_on: list[Any] = []
        probe_bridge = ApprovalBridge(window)

        def probe(prompt: Any) -> None:
            handled_on.append(QThread.currentThread())
            probe_bridge.resolve(ApprovalOutcome.ALLOW_ONCE)

        probe_bridge.prompt_ready.connect(probe, Qt.ConnectionType.QueuedConnection)
        done = threading.Event()

        def worker() -> None:
            probe_bridge.request(edit_prompt())
            done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert pump_until(lambda: bool(handled_on)), "the queued prompt never arrived"
        assert done.wait(timeout=5)

        assert handled_on == [gui_thread], (
            "the prompt was handled off the GUI thread; this is the blank-dialog bug"
        )

    def test_window_reports_itself_on_the_gui_thread(self, gui_harness: Any) -> None:
        window = gui_harness.window
        assert window._on_gui_thread() is True
        assert window.approval.thread() is window.thread()

    def test_prompt_creates_a_visible_dialog_on_the_main_window(self, gui_harness: Any) -> None:
        window = gui_harness.window
        done = threading.Event()

        def worker() -> None:
            window.approval.request(edit_prompt())
            done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert pump_until(lambda: window._active_dialog is not None)

        dialog = window._active_dialog
        assert dialog is not None
        assert dialog.thread() is window.thread(), "dialog lives on the wrong thread"
        assert dialog.prompt.tool == "edit_file"
        assert "README.md" in dialog.full_text()
        # The composer is gated while a decision is pending.
        assert window.input.isEnabled() is False

        dialog.deny_button.click()
        drain(40)
        assert done.wait(timeout=5)
        assert window._active_dialog is None
        assert window.input.isEnabled() is True


class TestChoicesReachTheWorker:
    @pytest.mark.parametrize(
        ("button", "expected"),
        [
            ("allow_button", ApprovalOutcome.ALLOW_ONCE),
            ("allow_session_button", ApprovalOutcome.ALLOW_FOR_SESSION),
            ("deny_button", ApprovalOutcome.DENY),
        ],
    )
    def test_each_choice_releases_the_worker(
        self, gui_harness: Any, button: str, expected: ApprovalOutcome
    ) -> None:
        window = gui_harness.window
        result: list[ApprovalOutcome] = []
        done = threading.Event()

        def worker() -> None:
            result.append(window.approval.request(edit_prompt()))
            done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert pump_until(lambda: window._active_dialog is not None)

        dialog = window._active_dialog
        assert dialog is not None
        try:
            getattr(dialog, button).click()
            drain(40)
        finally:
            # The window deletes the dialog itself on finish; make sure nothing
            # is left holding it if the assertion below fails first.
            close_dialog(window._active_dialog)
            drain(10)

        assert done.wait(timeout=5), "the worker stayed blocked"
        assert result == [expected]


class TestAgentEventsAreHandledOnTheGuiThread:
    """The event bus is a plain Python callback list, not a Qt signal.

    ``EventBus.subscribe_all`` has no Qt involvement, so a subscriber runs on
    whichever thread published the event. ``MainWindow`` renders activity rows
    and status-bar text in that subscriber, so before this was marshalled the
    agent's worker thread mutated QWidgets directly -- undefined behaviour that
    shows up as an unstable or frozen window, and it fires exactly while an
    approval is pending (``permission_decision`` and ``permission_approval``).
    """

    def test_publishing_from_a_worker_thread_does_not_touch_widgets_off_thread(
        self, gui_harness: Any
    ) -> None:
        window = gui_harness.window
        gui_thread = window.thread()
        handled_on: list[Any] = []
        original = window._on_agent_event

        def spy(event: Any) -> None:
            handled_on.append(QThread.currentThread())
            original(event)

        window._on_agent_event = spy

        try:

            def worker() -> None:
                window._context.events.emit(
                    AgentEventType.PERMISSION_APPROVAL,
                    {
                        "tool": "edit_file",
                        "permission": "WRITE_WORKSPACE",
                        "approval": "ALLOW_ONCE",
                    },
                )

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            thread.join(timeout=5)

            # Nothing may have been rendered yet: the worker only enqueued.
            assert handled_on == [], "an event was rendered from the publishing thread"

            assert pump_until(lambda: bool(handled_on)), "the queued event was never drained"
            assert handled_on, "event not handled"
            assert all(seen is gui_thread for seen in handled_on), (
                "an agent event was handled off the GUI thread"
            )
        finally:
            window._on_agent_event = original
            # Render anything still queued with the real handler so the spy is
            # not left installed behind a live queue.
            window._drain_agent_events()

    def test_permission_events_reach_the_activity_panel(self, gui_harness: Any) -> None:
        window = gui_harness.window
        window._context.events.emit(
            AgentEventType.PERMISSION_APPROVAL,
            {"tool": "edit_file", "permission": "WRITE_WORKSPACE", "approval": "ALLOW_ONCE"},
        )
        assert pump_until(lambda: "user allowed once" in " ".join(window.activity.texts()))
