"""GUI tests that do not need Qt.

These cover the widgets' data-layer contracts (masking, formatting, transcripts)
by driving the widgets through PySide6 when it is available and skipping
cleanly when it is not, so ``pytest`` on a machine without a Qt install still
exercises everything else.

Run with::

    pytest tests/gui -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is required for GUI tests")

from quantsmind_agent.gui.activity import ActivityView
from quantsmind_agent.gui.conversation import ConversationView
from quantsmind_agent.gui.sidebar import Sidebar

pytestmark = pytest.mark.gui


class TestConversationView:
    def test_starts_with_the_placeholder(self, qapp: object) -> None:
        view = ConversationView()
        assert view.has_content is False
        assert "Open a project" in view.toPlainText()

    def test_user_and_assistant_blocks(self, qapp: object) -> None:
        view = ConversationView()
        view.append_user("Explain this project.")
        view.append_assistant("It is a sample app.", meta="fake-model")
        text = view.toPlainText()
        assert "Explain this project." in text
        assert "It is a sample app." in text
        assert "fake-model" in text
        assert view.has_content

    def test_streaming_updates_in_place(self, qapp: object) -> None:
        view = ConversationView()
        view.begin_stream()
        view.append_stream_delta("Hello")
        view.append_stream_delta(", world")
        view.end_stream(meta="1 iteration")
        text = view.toPlainText()
        assert "Hello, world" in text
        assert "1 iteration" in text

    def test_delta_without_begin_still_renders(self, qapp: object) -> None:
        view = ConversationView()
        view.append_stream_delta("orphan text")
        assert "orphan text" in view.toPlainText()

    def test_tool_notices_are_visible(self, qapp: object) -> None:
        view = ConversationView()
        view.append_tool_notice("read_file", "README.md", "started")
        view.append_tool_notice("read_file", "README.md", "completed")
        text = view.toPlainText()
        assert "read_file" in text
        assert "README.md" in text

    def test_errors_are_announced(self, qapp: object) -> None:
        view = ConversationView()
        view.append_notice("No API key configured.", kind="error")
        text = view.toPlainText()
        assert "Error" in text
        assert "No API key configured." in text

    def test_html_in_content_is_escaped(self, qapp: object) -> None:
        view = ConversationView()
        view.append_user("<b>not bold</b>")
        assert "<b>not bold</b>" not in view.toPlainText()
        assert "not bold" in view.toPlainText()

    def test_clear_resets_to_the_placeholder(self, qapp: object) -> None:
        view = ConversationView()
        view.append_user("hello")
        view.clear_transcript()
        assert view.has_content is False
        assert "hello" not in view.toPlainText()


class TestActivityView:
    def test_records_states(self, qapp: object) -> None:
        view = ActivityView()
        view.record("read_file", "README.md", "started")
        view.record("read_file", "README.md", "completed")
        texts = view.texts()
        assert len(texts) == 2
        assert "read_file" in texts[0]
        assert "README.md" in texts[1]

    def test_failed_rows_are_marked(self, qapp: object) -> None:
        view = ActivityView()
        view.record("search_content", "denied", "failed")
        assert "✗" in view.last_text()

    def test_notice_rows(self, qapp: object) -> None:
        view = ActivityView()
        view.notice("configuration updated")
        assert "configuration updated" in view.last_text()

    def test_rows_are_bounded(self, qapp: object) -> None:
        view = ActivityView()
        for index in range(400):
            view.record("read_file", f"file-{index}", "completed")
        assert view.row_count <= 300

    def test_clear(self, qapp: object) -> None:
        view = ActivityView()
        view.record("read_file", "a.py", "completed")
        view.clear_activity()
        assert view.row_count == 0
        assert view.last_text() == ""


class TestSidebar:
    def test_starts_without_a_project(self, qapp: object) -> None:
        sidebar = Sidebar()
        assert sidebar._workspace_value.text() == "No project open"
        assert sidebar._clear_button.isEnabled() is False

    def test_set_workspace(self, qapp: object, tmp_path: Path) -> None:
        sidebar = Sidebar()
        sidebar.set_workspace(str(tmp_path))
        assert str(tmp_path) in sidebar._workspace_value.text()
        assert sidebar._clear_button.isEnabled() is True

    def test_set_workspace_none(self, qapp: object, tmp_path: Path) -> None:
        sidebar = Sidebar()
        sidebar.set_workspace(str(tmp_path))
        sidebar.set_workspace(None)
        assert sidebar._workspace_value.text() == "No project open"

    def test_model_summary_never_shows_a_key(self, qapp: object) -> None:
        sidebar = Sidebar()
        sidebar.set_model("openai_compatible", "gpt-4o-mini", "https://api.test/v1", "sk-***op")
        rendered = sidebar._api_key_value.text() + sidebar._endpoint_value.text()
        assert "sk-abcdefghijklmnop" not in rendered
        assert "gpt-4o-mini" in sidebar._model_value.text()

    def test_status_and_error(self, qapp: object) -> None:
        sidebar = Sidebar()
        sidebar.set_status("Working...")
        assert "Working" in sidebar._status.text()
        sidebar.set_error("Something went wrong")
        assert sidebar._error.isVisible() is False  # hidden until the window shows
        sidebar.set_error("")
        assert sidebar._error.text() == ""

    def test_session_counts(self, qapp: object) -> None:
        sidebar = Sidebar()
        sidebar.set_session_counts(4, 2, 3)
        assert "4 messages" in sidebar._session_value.text()
        assert "3 tool calls" in sidebar._session_value.text()

    def test_chosen_directory_defaults_to_empty(self, qapp: object) -> None:
        assert Sidebar().chosen_directory == ""
