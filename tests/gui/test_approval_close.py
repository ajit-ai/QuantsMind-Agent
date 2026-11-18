"""Regression tests for the approval *window-close* path.

The safety promise is that a turn waiting for a human decision can never run the
operation by accident, and that closing the application releases the worker
instead of stranding it. Both halves used to be exercised only indirectly: the
dialog's Escape key has a test, and ``ApprovalBridge.cancel_pending()`` is
driven directly in ``tests/integration/test_app_approvals.py``, but nothing sent
a real close event through :meth:`MainWindow.closeEvent` and asserted what the
waiting worker received.

The path under test:

    MainWindow.closeEvent
      -> cancel_pending_approval
           -> dialog.reject()            (if a dialog is on screen)
           -> ApprovalBridge.cancel_pending()
      -> _context.shutdown()

A denial is the default: :attr:`ApprovalDialog.outcome` starts as ``DENY`` and
only an explicit Allow button changes it, so no close route can approve.

Every wait here is bounded by construction -- a fixed number of ``settle()``
calls or an ``Event.wait`` with a timeout -- because a test that hangs tells you
nothing about the code it meant to cover.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quantsmind_agent.gui.security.approval import build_prompt
from quantsmind_agent.security.permissions import ApprovalOutcome, Permission, PermissionRequest
from tests.fakes import text_response
from tests.gui.harness import GuiHarness

EDIT = {
    "path": "README.md",
    "operation": "replace",
    "old_text": "# Sample Project",
    "new_text": "# Accepted Project",
}

#: Upper bound on how long a worker may take to answer after being released.
_ANSWER_TIMEOUT = 10.0

#: Bounded wait: each attempt drains queued events for a fixed short period, so
#: the loop always terminates even if the condition is never met.
_MAX_SETTLE_ATTEMPTS = 40


def _settle_until(gui_harness: GuiHarness, condition: Callable[[], bool]) -> bool:
    """Drain events until ``condition`` holds, giving up after a fixed budget."""

    for _ in range(_MAX_SETTLE_ATTEMPTS):
        if condition():
            return True
        gui_harness.settle()
    return condition()


def _workspace_prompt(sample_project: Path) -> Any:
    return build_prompt(
        PermissionRequest(
            permission=Permission.WRITE_WORKSPACE,
            tool="edit_file",
            summary="edit README.md",
            target="README.md",
            details=dict(EDIT),
        ),
        workspace=str(sample_project),
    )


def _request_in_background(
    gui_harness: GuiHarness, prompt: Any
) -> tuple[list[ApprovalOutcome], threading.Event]:
    """Ask the window's bridge for a decision from a worker thread.

    This is the shape the application uses: the bridge blocks the worker, not
    the GUI thread. The returned list receives the outcome and the event fires
    when the worker has been released.
    """

    outcomes: list[ApprovalOutcome] = []
    released = threading.Event()

    def worker() -> None:
        try:
            outcomes.append(gui_harness.window.approval.request(prompt))
        finally:
            released.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return outcomes, released


class TestClosingTheWindowDenies:
    def test_closing_the_window_denies_the_pending_request(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        outcomes, released = _request_in_background(gui_harness, _workspace_prompt(sample_project))
        assert _settle_until(gui_harness, lambda: gui_harness.window.approval.has_pending)

        gui_harness.window.close()

        assert released.wait(timeout=_ANSWER_TIMEOUT), "the worker was left blocked"
        assert outcomes == [ApprovalOutcome.DENY]

    def test_the_dialog_is_shown_and_then_dismissed_by_the_close(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        _, released = _request_in_background(gui_harness, _workspace_prompt(sample_project))
        assert _settle_until(gui_harness, lambda: gui_harness.window._active_dialog is not None)
        dialog = gui_harness.window._active_dialog
        assert dialog is not None
        assert dialog.outcome is ApprovalOutcome.DENY

        gui_harness.window.close()

        assert released.wait(timeout=_ANSWER_TIMEOUT)
        # A dialog left dangling after the window closed would keep a decision
        # surface alive for a turn that no longer exists.
        assert gui_harness.window._active_dialog is None

    def test_the_request_is_resolved_exactly_once(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        answered_before = gui_harness.window.approval.answered_count
        _, released = _request_in_background(gui_harness, _workspace_prompt(sample_project))
        assert _settle_until(gui_harness, lambda: gui_harness.window.approval.has_pending)

        gui_harness.window.close()

        assert released.wait(timeout=_ANSWER_TIMEOUT)
        # closeEvent denies twice over (dialog.reject() then cancel_pending()), so
        # a bug here would show up as one request counted twice.
        assert gui_harness.window.approval.answered_count == answered_before + 1
        assert gui_harness.window.approval.has_pending is False


class TestClosingTheDialogDenies:
    def test_closing_the_dialog_itself_denies_the_request(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        # The dialog's own close button: a different route to the same promise.
        gui_harness.open_project(sample_project)
        outcomes, released = _request_in_background(gui_harness, _workspace_prompt(sample_project))
        assert _settle_until(gui_harness, lambda: gui_harness.window._active_dialog is not None)
        dialog = gui_harness.window._active_dialog
        assert dialog is not None

        dialog.close()

        assert released.wait(timeout=_ANSWER_TIMEOUT), "the worker was left blocked"
        assert outcomes == [ApprovalOutcome.DENY]
        assert gui_harness.window.approval.has_pending is False

    def test_a_closed_dialog_leaves_no_window_reference(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        _, released = _request_in_background(gui_harness, _workspace_prompt(sample_project))
        assert _settle_until(gui_harness, lambda: gui_harness.window._active_dialog is not None)
        dialog = gui_harness.window._active_dialog
        assert dialog is not None

        dialog.close()

        assert released.wait(timeout=_ANSWER_TIMEOUT)
        assert gui_harness.window._active_dialog is None


class TestCloseWithoutAnythingPending:
    def test_closing_an_idle_window_is_harmless(self, gui_harness: GuiHarness) -> None:
        gui_harness.window.close()
        gui_harness.settle()

        assert gui_harness.bridge.is_busy is False
        assert gui_harness.window.approval.answered_count == 0

    def test_closing_after_a_finished_turn_is_harmless(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(text_response("done"))
        gui_harness.ask("hello")

        gui_harness.window.close()
        gui_harness.settle()

        assert gui_harness.bridge.is_busy is False
        assert "done" in gui_harness.transcript
