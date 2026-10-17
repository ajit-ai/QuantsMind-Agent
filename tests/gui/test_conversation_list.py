"""The conversation list in the sidebar, driven through the real window.

A durable conversation is only useful if a person can get back to it, so these
exercise the sidebar wiring rather than the store: choosing an entry reopens the
conversation, New saves what was open, and Delete removes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from PySide6.QtWidgets import QGridLayout, QPushButton, QWidget

from quantsmind_agent.gui.main_window import _safe_filename
from quantsmind_agent.gui.sidebar import Sidebar
from quantsmind_agent.permissions import ApprovalOutcome
from quantsmind_agent.schema import UserMessage
from tests.fakes import FakeModelProvider, text_response, tool_response
from tests.gui.harness import GuiHarness


def _context_ready(gui_harness: GuiHarness) -> None:
    gui_harness.context.tools.permissions.set_approver(lambda request: ApprovalOutcome.ALLOW_ONCE)


class TestConversationList:
    def test_a_new_conversation_appears_in_the_list(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question worth keeping")

        assert gui_harness.window.sidebar.conversations_available()

    def test_starting_a_new_one_keeps_the_old(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("first question")
        first_id = gui_harness.context.session.id

        gui_harness.window._on_new_conversation()

        summaries = gui_harness.context.list_conversations()
        assert first_id in {summary.id for summary in summaries}
        assert gui_harness.context.session.id != first_id

    def test_choosing_a_stored_conversation_reopens_it(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("the first answer")]))
        gui_harness.ask("the first question")
        first_id = gui_harness.context.session.id
        gui_harness.window._on_new_conversation()

        gui_harness.window._on_conversation_selected(first_id)

        assert gui_harness.context.session.id == first_id
        assert "the first question" in gui_harness.transcript

    def test_deleting_removes_it_from_the_list(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_conversation_delete_requested(conversation_id)

        assert conversation_id not in {
            summary.id for summary in gui_harness.context.list_conversations()
        }
        assert "deleted" in gui_harness.transcript.lower()

    def test_deleting_an_unknown_conversation_is_reported_not_crashed(
        self, gui_harness: GuiHarness
    ) -> None:
        gui_harness.window._on_conversation_delete_requested("nope")

        assert "nothing to delete" in gui_harness.transcript.lower()

    def test_a_missing_workspace_after_resuming_is_explained(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id
        gui_harness.window._on_new_conversation()

        import shutil

        shutil.rmtree(sample_project)
        gui_harness.window._on_conversation_selected(conversation_id)

        assert "not available" in gui_harness.transcript.lower()

    def test_choosing_the_open_conversation_does_nothing(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_conversation_selected(conversation_id)

        assert gui_harness.context.session.id == conversation_id
        assert "Reopened" not in gui_harness.transcript


class TestRestoredHistoryIsVisible:
    def test_a_reopened_conversation_shows_its_whole_transcript(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("the first answer")]))
        gui_harness.ask("the first question")
        gui_harness.use_provider(FakeModelProvider([text_response("the second answer")]))
        gui_harness.ask("the second question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_new_conversation()
        gui_harness.window._on_conversation_selected(conversation_id)

        transcript = gui_harness.transcript
        assert "the first question" in transcript
        assert "the first answer" in transcript
        assert "the second question" in transcript
        assert "the second answer" in transcript

    def test_the_restored_transcript_shows_each_thing_once(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("only once")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_new_conversation()
        gui_harness.window._on_conversation_selected(conversation_id)
        first_pass = gui_harness.transcript.count("a question")

        # Re-opening the same conversation twice must not stack another copy.
        gui_harness.window._on_new_conversation()
        gui_harness.window._on_conversation_selected(conversation_id)
        second_pass = gui_harness.transcript.count("a question")

        assert first_pass == 1
        assert second_pass == 1

    def test_tool_activity_is_shown_in_the_restored_transcript(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(
            FakeModelProvider(
                [
                    tool_response([("read_file", {"path": "mod.py"})]),
                    text_response("it returns one"),
                ]
            )
        )
        gui_harness.ask("what does mod.py do?")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_new_conversation()
        gui_harness.window._on_conversation_selected(conversation_id)

        transcript = gui_harness.transcript
        assert "read_file" in transcript
        assert transcript.index("what does mod.py do?") < transcript.index("read_file")
        assert transcript.index("read_file") < transcript.index("it returns one")

    def test_a_failed_tool_call_is_shown_as_failed(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(
            FakeModelProvider(
                [
                    tool_response(
                        [("edit_file", {"path": "mod.py", "old_text": "nope", "new_text": "x"})]
                    ),
                    text_response("that did not match"),
                ]
            )
        )
        gui_harness.ask("change something")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_new_conversation()
        gui_harness.window._on_conversation_selected(conversation_id)

        assert "edit_file" in gui_harness.transcript

    def test_tool_payloads_are_not_shown_in_the_transcript(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(
            FakeModelProvider(
                [
                    tool_response([("read_file", {"path": "mod.py"})]),
                    text_response("read it"),
                ]
            )
        )
        gui_harness.ask("read mod.py")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_new_conversation()
        gui_harness.window._on_conversation_selected(conversation_id)

        # The file's contents are what the model saw, not what a person reopening the
        # conversation is reading.
        assert "def value" not in gui_harness.transcript

    def test_an_interrupted_turn_is_admitted_not_glossed_over(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("all done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id
        gui_harness.context._last_turn_finished = False
        gui_harness.context.save_conversation()

        gui_harness.window._on_new_conversation()
        gui_harness.window._on_conversation_selected(conversation_id)

        assert "interrupted" in gui_harness.transcript.lower()

    def test_a_clean_turn_is_not_labelled_interrupted(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("all done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_new_conversation()
        gui_harness.window._on_conversation_selected(conversation_id)

        assert "interrupted" not in gui_harness.transcript.lower()

    def test_a_truncated_history_is_admitted(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.context.config_manager.config.agent.max_stored_messages = 2
        gui_harness.context._build_store()
        for index in range(6):
            gui_harness.context.session.add_message(UserMessage(content=f"question {index}"))
        gui_harness.context.save_conversation()
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_new_conversation()
        gui_harness.window._on_conversation_selected(conversation_id)

        assert "size limit" in gui_harness.transcript.lower()

    def test_refreshing_the_list_does_not_reopen_the_open_conversation(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")

        # _refresh_conversations moves the highlighted row. That must not emit a
        # selection, or the transcript is replayed on top of itself.
        gui_harness.window._refresh_conversations()
        gui_harness.window._refresh_conversations()

        assert gui_harness.transcript.count("a question") == 1


class TestExportFromTheSidebar:
    def test_exporting_the_open_conversation_writes_a_file(
        self,
        gui_harness: GuiHarness,
        sample_project: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("an answer")]))
        gui_harness.ask("a question worth keeping")
        target = tmp_path / "export.md"
        monkeypatch.setattr(
            gui_harness.window.sidebar,
            "choose_export_destination",
            lambda suggested: (str(target), "md"),
        )

        gui_harness.window._on_conversation_export_requested(gui_harness.context.session.id)

        assert target.is_file()
        assert "a question worth keeping" in target.read_text(encoding="utf-8")
        assert "Exported to" in gui_harness.transcript

    def test_a_cancelled_export_writes_nothing(
        self,
        gui_harness: GuiHarness,
        sample_project: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("an answer")]))
        gui_harness.ask("a question")
        monkeypatch.setattr(
            gui_harness.window.sidebar,
            "choose_export_destination",
            lambda suggested: ("", ""),
        )

        gui_harness.window._on_conversation_export_requested(gui_harness.context.session.id)

        assert not list(tmp_path.glob("*.md"))

    def test_exporting_a_deleted_conversation_is_reported(
        self,
        gui_harness: GuiHarness,
        sample_project: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("an answer")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id
        gui_harness.context.delete_conversation(conversation_id)
        monkeypatch.setattr(
            gui_harness.window.sidebar,
            "choose_export_destination",
            lambda suggested: (str(tmp_path / "out.md"), "md"),
        )

        gui_harness.window._on_conversation_export_requested(conversation_id)

        assert "could not be exported" in gui_harness.transcript.lower()

    def test_a_suggested_export_name_is_usable_as_a_filename(self, gui_harness: GuiHarness) -> None:
        assert _safe_filename("Refactor: parser/tests?") == "Refactor-parsertests"
        assert _safe_filename("***") == "conversation"

    def test_renaming_shows_the_new_name_in_the_list(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_conversation_rename_requested(conversation_id, "Refactor plan")

        assert gui_harness.context.conversation_title == "Refactor plan"
        summary = next(
            item for item in gui_harness.context.list_conversations() if item.id == conversation_id
        )
        assert summary.title == "Refactor plan"
        # The listed label carries the timestamp too, so match on the name.
        assert any(
            "Refactor plan" in label for label in gui_harness.window.sidebar.conversation_labels()
        )

    def test_renaming_the_open_conversation_survives_the_next_save(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("one"), text_response("two")]))
        gui_harness.ask("first question")
        conversation_id = gui_harness.context.session.id
        gui_harness.window._on_conversation_rename_requested(conversation_id, "Keep me")

        gui_harness.ask("second question")

        store = gui_harness.context.conversations
        assert store is not None
        payload = store.load(conversation_id)
        assert payload is not None
        assert payload["title"] == "Keep me"

    def test_a_blank_rename_is_refused(self, gui_harness: GuiHarness, sample_project: Path) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_conversation_rename_requested(conversation_id, "   ")

        assert gui_harness.context.conversation_title != ""
        assert "was not applied" in gui_harness.transcript

    def test_renaming_an_unknown_conversation_is_reported_not_crashed(
        self, gui_harness: GuiHarness
    ) -> None:
        gui_harness.window._on_conversation_rename_requested("nope", "Title")

        assert "could not be renamed" in gui_harness.transcript.lower()

    def test_a_stale_unavailable_project_is_not_reported_against_the_next_conversation(
        self, gui_harness: GuiHarness, tmp_path: Path, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("one"), text_response("two")]))
        gui_harness.ask("first question")
        lost_id = gui_harness.context.session.id
        gui_harness.window._on_new_conversation()

        second_project = tmp_path / "second"
        second_project.mkdir()
        (second_project / "mod.py").write_text("x = 1\n", encoding="utf-8")
        gui_harness.open_project(second_project)
        gui_harness.ask("second question")
        healthy_id = gui_harness.context.session.id
        gui_harness.window._on_new_conversation()

        import shutil

        shutil.rmtree(sample_project)
        gui_harness.window._on_conversation_selected(lost_id)
        assert gui_harness.context.unavailable_workspace == str(sample_project)

        gui_harness.window._on_conversation_selected(healthy_id)

        assert gui_harness.context.unavailable_workspace == ""
        assert "not available" not in gui_harness.transcript.rsplit("Reopened", 1)[-1]


class TestConversationControlsFit:
    """The conversation controls have to be readable, not merely present.

    Four buttons in one row did not fit the sidebar's minimum width. Qt keeps the
    buttons and elides their labels instead, so "Export" rendered as "xpor" and
    "Rename" as "enarr" -- the controls still worked, which is exactly why
    nothing failed and nobody noticed. A screenshot of the installed
    application is what surfaced it.
    """

    def test_every_control_label_is_shown_in_full(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        window = gui_harness.window
        window.resize(window.minimumWidth(), 800)
        window.show()
        gui_harness.pump(300)
        sidebar = window.sidebar

        buttons = _conversation_buttons(sidebar)
        assert buttons, "no conversation controls were found in the sidebar"
        for button in buttons:
            # sizeHint is the width the widget needs to draw its full text. Given
            # less, Qt elides -- which is what turned "Export" into "xpor".
            needed = button.sizeHint().width()
            assert button.width() >= needed - 1, (
                f"'{button.text()}' is {button.width()}px wide but needs {needed}px"
            )

    def test_the_controls_are_not_in_a_single_row(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        sidebar = gui_harness.window.sidebar

        holding: tuple[QGridLayout, set[str]] | None = None
        for layout in sidebar.findChildren(QGridLayout):
            buttons = _grid_buttons(layout)
            texts = {button.text() for button in buttons}
            if "Export" in texts:
                holding = (layout, texts)
                break

        assert holding is not None, "the export control is not in a grid layout"
        layout, texts = holding
        rows = _grid_rows(layout)
        assert len(texts) == 4, f"expected all four conversation controls, found {texts}"
        assert len(rows) > 1, f"all conversation controls share row {rows} and will be elided"


def _grid_entries(layout: QGridLayout) -> list[tuple[int, QWidget]]:
    """(row, widget) for every occupied cell, skipping cells with no widget."""

    entries: list[tuple[int, QWidget]] = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if widget is not None:
            entries.append((index, widget))
    return entries


def _grid_buttons(layout: QGridLayout) -> list[QPushButton]:
    return [widget for _index, widget in _grid_entries(layout) if isinstance(widget, QPushButton)]


def _grid_rows(layout: QGridLayout) -> set[int]:
    """Which grid rows hold the controls, read through the layout's own API.

    ``getItemPosition`` is typed as returning an opaque object in the PySide6
    stubs, so the tuple is cast rather than indexed blind.
    """

    rows: set[int] = set()
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item is not None and item.widget() is not None:
            position = cast("tuple[int, int, int, int]", layout.getItemPosition(index))
            rows.add(position[0])
    return rows


def _conversation_buttons(sidebar: Sidebar) -> list[QPushButton]:
    """The four conversation controls, whatever layout holds them."""

    return [
        button
        for button in sidebar.findChildren(QPushButton)
        if button.text() in {"New", "Export", "Rename", "Delete"}
    ]
