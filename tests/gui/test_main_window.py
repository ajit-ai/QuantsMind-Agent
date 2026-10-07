"""GUI tests for the main window: startup, workspace, conversation, errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.errors import LLMError
from quantsmind_agent.llm import ChatRequest, ChatResponse
from tests.fakes import FakeModelProvider, text_response, tool_response
from tests.gui.harness import GuiHarness

pytestmark = pytest.mark.gui


class TestStartup:
    def test_window_builds_with_all_panels(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        assert window.windowTitle().startswith("QuantsMind-Agent")
        assert window.conversation is not None
        assert window.activity is not None
        assert window.sidebar is not None
        assert window.send_button.isEnabled()
        assert window.cancel_button.isVisible() is False

    def test_transcript_starts_with_guidance(self, gui_harness: GuiHarness) -> None:
        assert "Open a project" in gui_harness.transcript

    def test_status_bar_shows_the_model(self, gui_harness: GuiHarness) -> None:
        assert "gpt-4o-mini" in gui_harness.window._status_model.text()

    def test_startup_hint_is_shown_when_not_ready(self, gui_harness: GuiHarness) -> None:
        assert "Open a project folder first." in gui_harness.transcript


class TestWorkspaceSelection:
    def test_opening_a_project_updates_the_sidebar(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        assert str(sample_project.resolve()) in (gui_harness.window.sidebar._workspace_value.text())
        assert "Project opened" in gui_harness.transcript
        assert gui_harness.context.tools is not None
        assert "read_file" in gui_harness.context.tools.names

    def test_invalid_project_is_reported_in_the_gui(
        self, gui_harness: GuiHarness, tmp_path: Path
    ) -> None:
        gui_harness.open_project(tmp_path / "missing")
        assert "could not be opened" in gui_harness.transcript
        assert gui_harness.window.sidebar._workspace_value.text() == "No project open"

    def test_closing_the_project_clears_tools(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        assert gui_harness.context.tools is not None
        assert gui_harness.context.tools.names
        gui_harness.window._on_close_workspace()
        assert gui_harness.context.tools.names == []
        assert "Project closed." in gui_harness.transcript


class TestConversationFlow:
    def test_answer_is_rendered(self, gui_harness: GuiHarness, sample_project: Path) -> None:
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(text_response("It is a small sample project."))
        gui_harness.ask("Explain the structure of this project.")

        text = gui_harness.transcript
        assert "Explain the structure of this project." in text
        assert "It is a small sample project." in text
        assert gui_harness.window.input.toPlainText() == ""

    def test_tool_activity_is_visible_in_both_panels(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(
            tool_response([("read_file", {"path": "README.md"})]),
            text_response("The README describes a sample project."),
        )
        gui_harness.ask("Read the README and summarize the project.")

        assert any("read_file" in row for row in gui_harness.activity)
        assert any("✓" in row for row in gui_harness.activity)
        assert "read_file" in gui_harness.transcript
        assert "The README describes a sample project." in gui_harness.transcript

    def test_tool_escape_is_surfaced_as_an_error(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(
            tool_response([("read_file", {"path": "../../../etc/passwd"})]),
            text_response("That path is outside the workspace, so I could not read it."),
        )
        gui_harness.ask("Read the passwd file.")
        assert "outside the current workspace" in gui_harness.transcript
        assert any("✗" in row for row in gui_harness.activity)

    def test_sidebar_counts_update(self, gui_harness: GuiHarness, sample_project: Path) -> None:
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(text_response("done"))
        gui_harness.ask("hello")
        assert "2 messages" in gui_harness.window.sidebar._session_value.text()

    def test_send_is_disabled_while_busy(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        release = gui_harness.block_next_turn()
        gui_harness.provider.script(text_response("done"))
        gui_harness.window.input.setPlainText("hello")
        gui_harness.window._on_send_clicked()

        # ``busy_changed(True)`` is emitted synchronously by the bridge, so the
        # window is already in its working state here -- no event pumping.
        assert gui_harness.window.send_button.isEnabled() is False
        assert gui_harness.window.cancel_button.isHidden() is False
        assert gui_harness.window.sidebar._open_button.isEnabled() is False

        release.set()
        gui_harness.pump()
        assert gui_harness.window.send_button.isEnabled() is True
        assert gui_harness.window.cancel_button.isHidden() is True
        assert "done" in gui_harness.transcript

    def test_stop_cancels_a_running_turn(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        release = gui_harness.block_next_turn()
        gui_harness.provider.script(text_response("never shown"))
        gui_harness.window.input.setPlainText("hello")
        gui_harness.window._on_send_clicked()
        gui_harness.window._on_cancel_clicked()
        release.set()
        gui_harness.pump()
        assert "cancelled" in gui_harness.transcript.lower()
        assert "never shown" not in gui_harness.transcript

    def test_new_conversation_clears_the_view(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(text_response("first answer"))
        gui_harness.ask("hello")
        gui_harness.window._on_new_conversation()
        assert "first answer" not in gui_harness.transcript
        assert gui_harness.window.activity.row_count == 0
        assert gui_harness.context.session.messages == []
        assert gui_harness.context.workspace is not None

    def test_permission_decisions_are_surfaced(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(
            tool_response([("list_directory", {"path": "."})]),
            text_response("Listed."),
        )
        gui_harness.ask("List the project.")
        assert any("permission ALLOW" in row for row in gui_harness.activity)


class TestWorkerBridge:
    """Regression tests for cross-thread signal delivery.

    PySide6 silently drops events on cross-thread *signal-to-signal*
    connections, which used to hide the second and later tool-activity rows.
    The bridge therefore connects the worker to real methods.
    """

    def test_every_tool_event_reaches_the_window(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(
            tool_response(
                [
                    ("project_structure", {}),
                    ("list_directory", {"path": "src"}),
                    ("read_file", {"path": "README.md"}),
                ]
            ),
            text_response("Inspected three things."),
        )
        gui_harness.ask("Inspect the project.")
        rows = gui_harness.activity
        for tool in ("project_structure", "list_directory", "read_file"):
            assert sum(1 for row in rows if tool in row) >= 2, (
                f"expected a started and a completed row for {tool}, got {rows}"
            )

    def test_tool_events_precede_the_answer(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(
            tool_response([("read_file", {"path": "README.md"})]),
            text_response("ANSWER_MARKER"),
        )
        gui_harness.ask("Read it.")
        text = gui_harness.transcript
        assert text.index("read_file") < text.index("ANSWER_MARKER")

    def test_text_deltas_are_all_delivered(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        words = ["alpha", "bravo", "charlie", "delta", "echo"]
        gui_harness.provider.script(text_response(" ".join(words)))
        gui_harness.ask("Say five words.")
        text = gui_harness.transcript
        for word in words:
            assert word in text


class TestErrorHandling:
    def test_provider_failure_is_shown_without_a_traceback(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        class BrokenProvider(FakeModelProvider):
            def complete(self, request: ChatRequest) -> ChatResponse:
                raise LLMError("provider exploded", user_message="The provider exploded.")

        gui_harness.open_project(sample_project)
        gui_harness.use_provider(BrokenProvider())
        gui_harness.ask("hello")

        text = gui_harness.transcript
        assert "The provider exploded." in text
        assert "Traceback" not in text
        assert "LLMError" not in text
        assert gui_harness.window.send_button.isEnabled() is True

    def test_empty_message_is_ignored(self, gui_harness: GuiHarness) -> None:
        before = gui_harness.transcript
        gui_harness.window.input.setPlainText("   ")
        gui_harness.window._on_send_clicked()
        gui_harness.pump()
        assert gui_harness.transcript == before

    def test_no_credentials_appear_in_the_transcript(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.context.config_manager.set_api_key("super-secret-key-9999")
        gui_harness.open_project(sample_project)
        gui_harness.provider.script(text_response("all good"))
        gui_harness.ask("hello")
        combined = gui_harness.transcript + " ".join(gui_harness.activity)
        assert "super-secret-key-9999" not in combined
