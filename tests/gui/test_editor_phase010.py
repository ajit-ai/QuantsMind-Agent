"""Phase 0.10 editor surface tests: tabs, panes, save/close, find and replace."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is required for GUI tests")

from PySide6.QtGui import QTextCursor

from quantsmind_agent.gui.editor import document as document_module
from quantsmind_agent.gui.editor import tabs as tabs_module
from quantsmind_agent.gui.editor.document import NewlineStyle, TextEncoding
from quantsmind_agent.gui.editor.search import FindOptions
from quantsmind_agent.gui.editor.tabs import MAX_PANES, EditorArea, EditorPane
from quantsmind_agent.persistence.editor_state import EditorTabState
from quantsmind_agent.services.workspace.containment import Workspace

pytestmark = pytest.mark.gui


def _select_line(editor: object, line: int) -> None:
    cursor = QTextCursor(editor.document().findBlockByNumber(line - 1))  # type: ignore[attr-defined]
    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)  # type: ignore[attr-defined]


@pytest.fixture
def area(qapp: object, sample_project: Path) -> Iterator[EditorArea]:
    widget = EditorArea(
        confirm_close=lambda _document: "discard",
        confirm_overwrite=lambda _path: False,
        confirm_replace_all=lambda _count: True,
    )
    widget.set_workspace(Workspace(sample_project))
    yield widget
    widget.close_all(discard=True)
    widget.deleteLater()


class TestOpenAndPreview:
    def test_opening_a_file_shows_it(self, area: EditorArea) -> None:
        tab = area.open_relative("src/app/main.py")
        assert tab is not None
        assert area.current_relative() == "src/app/main.py"
        assert "def main" in tab.editor.plain_text()
        assert tab.editor.line_count() > 1
        assert tab.editor.isReadOnly() is False

    def test_preview_is_replaced_by_the_next_preview(self, area: EditorArea) -> None:
        area.open_relative("src/app/main.py", preview=True)
        area.open_relative("src/app/settings.py", preview=True)
        paths = {tab.path for tab in area.tab_states()}
        assert paths == {"src/app/settings.py"}

    def test_editing_pins_a_preview_tab(self, area: EditorArea) -> None:
        tab = area.open_relative("src/app/main.py", preview=True)
        assert tab is not None
        assert tab.preview is True
        tab.editor.setPlainText("edited\n")
        assert bool(tab.preview) is False
        pane = area._panes[0]
        assert pane.tabText(pane.indexOf(tab.page)).endswith("\u25cf")

    def test_dirty_preview_is_not_dropped(self, area: EditorArea) -> None:
        first = area.open_relative("src/app/main.py", preview=True)
        assert first is not None
        first.editor.setPlainText("edited\n")
        second = area.open_relative("src/app/settings.py", preview=True)
        assert second is not None
        third = area.open_relative("docs/usage.md", preview=True)
        assert third is not None
        paths = {tab.path for tab in area.tab_states()}
        assert "src/app/main.py" in paths
        assert "docs/usage.md" in paths
        assert "src/app/settings.py" not in paths

    def test_open_at_line(self, area: EditorArea) -> None:
        tab = area.open_relative("src/app/main.py", line=4)
        assert tab is not None
        assert tab.editor.current_line_column()[0] == 4

    def test_goto_line_clamps_to_the_target_line(
        self, area: EditorArea, sample_project: Path
    ) -> None:
        (sample_project / "lines.txt").write_bytes(b"one\n\nthree\n")
        tab = area.open_relative("lines.txt", preview=False)
        assert tab is not None
        # A column past the end of an empty line must stay on that line.
        tab.editor.goto_line(2, 5)
        assert tab.editor.current_line_column() == (2, 1)
        tab.editor.goto_line(1, 99)
        assert tab.editor.current_line_column() == (1, 4)

    def test_open_outside_the_workspace_is_refused(self, area: EditorArea) -> None:
        from quantsmind_agent.errors import QuantsMindError

        with pytest.raises(QuantsMindError):
            area.open_relative("../outside.py")

    def test_binary_file_is_read_only_and_explained(self, area: EditorArea) -> None:
        tab = area.open_relative("logo.png", preview=False)
        assert tab is not None
        assert tab.document.binary is True
        assert tab.editor.isReadOnly() is True
        assert not tab.notice.isHidden()
        assert "Binary file" in tab.editor.plain_text()
        notices: list[str] = []
        area.notice.connect(notices.append)
        assert area.save_current() is False
        assert notices
        assert "cannot be saved" in notices[-1]

    def test_oversized_file_opens_as_a_preview_until_explicitly_enabled(
        self, area: EditorArea, sample_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = sample_project / "src" / "app" / "large.txt"
        target.write_text("line\n" * 200, encoding="utf-8")
        monkeypatch.setattr(document_module, "PREVIEW_LIMIT_BYTES", 20)
        tab = area.open_relative("src/app/large.txt", preview=False)
        assert tab is not None
        assert tab.document.preview_only is True
        assert tab.editor.isReadOnly() is True
        assert tab.edit_anyway is not None
        tab.edit_anyway.click()
        assert bool(tab.document.preview_only) is False
        assert tab.editor.isReadOnly() is False

    def test_opening_a_file_leaves_the_buffer_clean(self, area: EditorArea) -> None:
        tab = area.open_relative("src/app/settings.py", preview=False)
        assert tab is not None
        assert tab.document.dirty is False
        assert area.has_dirty() is False

    def test_editing_marks_the_buffer_dirty(self, area: EditorArea) -> None:
        tab = area.open_relative("src/app/settings.py", preview=False)
        assert tab is not None
        tab.editor.setPlainText("DEFAULT_PORT = 1234\n")
        assert tab.document.dirty is True
        assert area.has_dirty() is True

    def test_showing_a_loaded_buffer_does_not_make_it_dirty(
        self, area: EditorArea, qapp: object
    ) -> None:
        """Qt re-emits textChanged when a loaded buffer is first laid out.

        That emission is not an edit, and treating it as one would offer a file
        the user only opened as unsaved work -- with a save prompt on close.
        """

        import time

        tab = area.open_relative("src/app/settings.py", preview=False)
        assert tab is not None
        area.show()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            qapp.processEvents()  # type: ignore[attr-defined]
            time.sleep(0.01)
        assert tab.document.dirty is False


class TestSaving:
    def test_save_writes_and_clears_dirty(self, area: EditorArea, sample_project: Path) -> None:
        tab = area.open_relative("src/app/settings.py", preview=False)
        assert tab is not None
        saved: list[str] = []
        area.document_saved.connect(saved.append)
        tab.editor.setPlainText("DEFAULT_PORT = 9090\n")
        assert area.current_tab() is not None
        assert area.current_tab().document.dirty is True  # type: ignore[union-attr]
        assert area.save_current() is True
        assert (sample_project / "src" / "app" / "settings.py").read_text(
            encoding="utf-8"
        ) == "DEFAULT_PORT = 9090\n"
        assert tab.document.dirty is False
        assert saved

    def test_save_preserves_crlf(self, area: EditorArea, sample_project: Path) -> None:
        target = sample_project / "notes.txt"
        target.write_bytes(b"one\r\ntwo\r\n")
        tab = area.open_relative("notes.txt", preview=False)
        assert tab is not None
        assert tab.document.newline is NewlineStyle.CRLF
        tab.editor.setPlainText("one\ntwo\nthree\n")
        assert area.save_current() is True
        assert target.read_bytes() == b"one\r\ntwo\r\nthree\r\n"

    def test_save_preserves_the_encoding(self, area: EditorArea, sample_project: Path) -> None:
        target = sample_project / "bom.txt"
        target.write_bytes(b"\xef\xbb\xbfhello\n")
        tab = area.open_relative("bom.txt", preview=False)
        assert tab is not None
        assert tab.document.encoding is TextEncoding.UTF8_BOM
        tab.editor.setPlainText("hello\nworld\n")
        assert area.save_current() is True
        assert target.read_bytes() == b"\xef\xbb\xbfhello\nworld\n"

    def test_save_all_reports_the_number_saved(self, area: EditorArea) -> None:
        first = area.open_relative("src/app/main.py", preview=False)
        second = area.open_relative("src/app/settings.py", preview=False)
        assert first is not None and second is not None
        first.editor.setPlainText("# first\n")
        second.editor.setPlainText("# second\n")
        assert area.save_all() == 2
        assert area.has_dirty() is False

    def test_saving_a_binary_file_is_refused(self, area: EditorArea) -> None:
        tab = area.open_relative("logo.png", preview=False)
        assert tab is not None
        notices: list[str] = []
        area.notice.connect(notices.append)
        assert area.save_current() is False
        assert notices
        assert "cannot be saved" in notices[-1]

    def test_save_as_into_the_workspace(self, area: EditorArea) -> None:
        tab = area.open_relative("docs/usage.md", preview=False)
        assert tab is not None
        target = area.workspace.root / "docs" / "copy.md"  # type: ignore[union-attr]
        assert area.save_current(target) is True
        assert target.is_file()
        assert area.current_relative() == "docs/copy.md"

    def test_save_as_outside_the_workspace_is_refused(
        self, area: EditorArea, tmp_path: Path
    ) -> None:
        tab = area.open_relative("docs/usage.md", preview=False)
        assert tab is not None
        notices: list[str] = []
        area.notice.connect(notices.append)
        assert area.save_current(tmp_path / "outside.md") is False
        assert notices


class TestClosing:
    def test_cancel_keeps_the_tab_open(self, area: EditorArea) -> None:
        tab = area.open_relative("docs/usage.md", preview=False)
        assert tab is not None
        tab.editor.setPlainText("changed\n")
        area._confirm_close = lambda _document: "cancel"
        assert area.close_current() is False
        assert area.current_relative() == "docs/usage.md"

    def test_discard_closes(self, area: EditorArea) -> None:
        tab = area.open_relative("docs/usage.md", preview=False)
        assert tab is not None
        tab.editor.setPlainText("changed\n")
        assert area.close_current() is True
        assert area.current_tab() is None

    def test_request_close_can_be_cancelled_for_any_tab(self, area: EditorArea) -> None:
        first = area.open_relative("src/app/main.py", preview=False)
        second = area.open_relative("src/app/settings.py", preview=False)
        assert first is not None and second is not None
        first.editor.setPlainText("one\n")
        second.editor.setPlainText("two\n")
        area._confirm_close = lambda _document: "cancel"
        assert area.request_close() is False
        assert len(area.tab_states()) == 2
        area._confirm_close = lambda _document: "discard"
        assert area.request_close() is True
        assert area.tab_states() == []

    def test_rename_repoints_open_tabs(self, area: EditorArea, sample_project: Path) -> None:
        from quantsmind_agent.services.workspace.operations import FileOperationService

        tab = area.open_relative("docs/usage.md", preview=False)
        assert tab is not None
        FileOperationService(area.workspace).rename("docs", "documentation")  # type: ignore[arg-type]
        assert area.apply_rename("docs", "documentation") == 1
        assert tab.document.relative == "documentation/usage.md"
        assert tab.document.path == sample_project / "documentation" / "usage.md"

    def test_deleting_a_folder_closes_its_tabs(self, area: EditorArea) -> None:
        area.open_relative("docs/usage.md", preview=False)
        area.open_relative("src/app/main.py", preview=False)
        assert area.close_relative("docs") == 1
        assert area.current_relative() == "src/app/main.py"
        assert area.close_relative("src") == 1
        assert area.current_tab() is None


class TestBufferCeiling:
    @staticmethod
    def _files(sample_project: Path, count: int) -> list[str]:
        names = []
        for index in range(count):
            name = f"buffer-{index}.txt"
            (sample_project / name).write_text(f"buffer {index}\n", encoding="utf-8")
            names.append(name)
        return names

    def test_open_limit_evicts_least_recently_used_clean_buffers(
        self, area: EditorArea, sample_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tabs_module, "MAX_OPEN_DOCUMENTS", 3)
        names = self._files(sample_project, 5)
        for name in names[:4]:
            area.open_relative(name, preview=False)
        assert {state.path for state in area.tab_states()} == set(names[1:4])
        assert area.current_relative() == names[3]

        area.open_relative(names[4], preview=False)
        assert {state.path for state in area.tab_states()} == set(names[2:5])
        assert area.current_relative() == names[4]

    def test_dirty_buffers_are_never_evicted(
        self, area: EditorArea, sample_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tabs_module, "MAX_OPEN_DOCUMENTS", 2)
        names = self._files(sample_project, 3)
        first = area.open_relative(names[0], preview=False)
        area.open_relative(names[1], preview=False)
        assert first is not None
        first.editor.setPlainText("unsaved work\n")

        area.open_relative(names[2], preview=False)
        paths = {state.path for state in area.tab_states()}
        assert names[0] in paths
        assert names[2] in paths
        assert names[1] not in paths
        assert area._registry.is_dirty(names[0]) is True

    def test_trim_for_memory_pressure_keeps_dirty_and_active(
        self, area: EditorArea, sample_project: Path
    ) -> None:
        names = self._files(sample_project, 5)
        dirty = area.open_relative(names[0], preview=False)
        for name in names[1:]:
            area.open_relative(name, preview=False)
        assert dirty is not None
        dirty.editor.setPlainText("keep me\n")

        released = area.trim(2)
        paths = {state.path for state in area.tab_states()}
        assert released == 3
        assert paths == {names[0], names[4]}
        assert area._registry.is_dirty(names[0]) is True

    def test_trim_cannot_close_dirty_work_even_below_the_limit(
        self, area: EditorArea, sample_project: Path
    ) -> None:
        names = self._files(sample_project, 3)
        for name in names:
            tab = area.open_relative(name, preview=False)
            assert tab is not None
            tab.editor.setPlainText(f"dirty {name}\n")
        assert area.trim(1) == 0
        assert {state.path for state in area.tab_states()} == set(names)


class TestPanes:
    def test_split_adds_panes_up_to_the_limit(self, area: EditorArea) -> None:
        notices: list[str] = []
        area.notice.connect(notices.append)
        for _ in range(MAX_PANES - 1):
            assert isinstance(area.split(), EditorPane)
        assert area.split() is None
        assert notices
        assert str(MAX_PANES) in notices[-1]

    def test_close_split_keeps_at_least_one_pane(self, area: EditorArea) -> None:
        area.open_relative("src/app/main.py", preview=False)
        assert area.close_split() is False
        area.split()
        assert area.close_split() is True

    def test_two_panes_can_show_different_files(self, area: EditorArea) -> None:
        area.open_relative("src/app/main.py", preview=False)
        pane = area.split()
        assert pane is not None
        tab = area.open_relative("src/app/settings.py", preview=False)
        assert tab is not None
        assert area.current_relative() == "src/app/settings.py"
        assert len(area._panes) == 2

    def test_tab_states_keep_pinned_and_active_flags(self, area: EditorArea) -> None:
        area.open_relative("src/app/main.py", preview=False)
        area.open_relative("src/app/settings.py", preview=True)
        states = area.tab_states()
        assert len(states) == 2
        active = [state for state in states if state.active]
        assert len(active) == 1
        assert active[0].path == "src/app/settings.py"
        assert active[0].pinned is False


class TestFindAndReplace:
    def _open(self, area: EditorArea, sample_project: Path, text: str) -> None:
        target = sample_project / "find.txt"
        target.write_bytes(text.encode("utf-8"))
        tab = area.open_relative("find.txt", preview=False)
        assert tab is not None

    def test_find_moves_the_selection(self, area: EditorArea, sample_project: Path) -> None:
        self._open(area, sample_project, "alpha beta alpha\n")
        area.show_find()
        area.find_bar.search_input.setText("alpha")
        area.find_bar.next_button.click()
        assert area.find_bar.status_label.text() == "match"
        assert area.selected_text() == "alpha"

    def test_find_with_no_match_reports_it(self, area: EditorArea, sample_project: Path) -> None:
        self._open(area, sample_project, "alpha\n")
        area.show_find()
        area.find_bar.search_input.setText("omega")
        area.find_bar.next_button.click()
        assert area.find_bar.status_label.text() == "No results"

    def test_regex_and_case_options(self, area: EditorArea, sample_project: Path) -> None:
        self._open(area, sample_project, "Alpha beta\n")
        area.show_find()
        area.find_bar.search_input.setText("alpha")
        area.find_bar.next_button.click()
        assert area.find_bar.status_label.text() == "match"
        area.find_bar.close_bar()
        area.find_bar.search_input.setText("alpha")
        area.find_bar.case_box.setChecked(True)
        area.find_bar.next_button.click()
        assert area.find_bar.status_label.text() == "No results"
        area.find_bar.close_bar()
        area.find_bar.case_box.setChecked(False)
        area.find_bar.regex_box.setChecked(True)
        area.find_bar.search_input.setText(r"Alpha\s+beta")
        area.find_bar.next_button.click()
        assert area.find_bar.status_label.text() == "match"

    def test_replace_current(self, area: EditorArea, sample_project: Path) -> None:
        self._open(area, sample_project, "alpha beta\n")
        area.show_find(replace=True)
        area.find_bar.search_input.setText("alpha")
        area.find_bar.replace_input.setText("gamma")
        area.find_bar.replace_button.click()
        assert area.find_bar.status_label.text() == "replaced"
        assert area.current_editor().plain_text().startswith("gamma")  # type: ignore[union-attr]

    def test_replace_all(self, area: EditorArea, sample_project: Path) -> None:
        self._open(area, sample_project, "x x x\n")
        area.show_find(replace=True)
        area.find_bar.search_input.setText("x")
        area.find_bar.replace_input.setText("y")
        area.find_bar.replace_all_button.click()
        assert area.find_bar.status_label.text() == "3 replaced"
        assert area.current_editor().plain_text() == "y y y\n"  # type: ignore[union-attr]

    def test_selected_line_range(self, area: EditorArea, sample_project: Path) -> None:
        self._open(area, sample_project, "one\ntwo\nthree\n")
        tab = area.current_tab()
        assert tab is not None
        _select_line(tab.editor, 2)
        assert tab.editor.selected_text() == "two"
        assert tab.editor.selected_line_range() == (2, 2)


class TestRestoration:
    def test_tab_states_roundtrip(self, area: EditorArea) -> None:
        first = area.open_relative("src/app/main.py", preview=False)
        second = area.open_relative("src/app/settings.py", preview=False)
        assert first is not None and second is not None
        first.editor.goto_line(4)
        second.editor.goto_line(3)
        states = area.tab_states()
        area.close_all(discard=True)
        assert area.tab_states() == []
        missing = area.restore_tabs(states)
        assert missing == []
        restored = {state.path: state for state in area.tab_states()}
        assert set(restored) == {"src/app/main.py", "src/app/settings.py"}
        active = [state for state in area.tab_states() if state.active]
        assert len(active) == 1

    def test_missing_tab_is_reported(self, area: EditorArea) -> None:
        missing = area.restore_tabs([EditorTabState(path="deleted.py")])
        assert missing == ["deleted.py"]
        assert area.tab_states() == []

    def test_escaping_stored_path_is_not_opened(self, area: EditorArea, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        missing = area.restore_tabs([EditorTabState(path="../outside.txt")])
        assert missing == ["../outside.txt"]
        assert area.tab_states() == []


class TestExternalChanges:
    def test_clean_tab_reloads_from_disk(self, area: EditorArea, sample_project: Path) -> None:
        target = sample_project / "watch.txt"
        target.write_bytes(b"before\n")
        tab = area.open_relative("watch.txt", preview=False)
        assert tab is not None
        notices: list[str] = []
        area.notice.connect(notices.append)
        target.write_bytes(b"after changed\n")
        area.check_external_changes()
        assert tab.editor.plain_text() == "after changed\n"
        assert notices and "reloaded from disk" in notices[-1]

    def test_dirty_tab_asks_instead_of_overwriting(
        self, area: EditorArea, sample_project: Path
    ) -> None:
        target = sample_project / "watch.txt"
        target.write_bytes(b"before\n")
        tab = area.open_relative("watch.txt", preview=False)
        assert tab is not None
        tab.editor.setPlainText("mine\n")
        keys: list[str] = []
        area.external_change_detected.connect(keys.append)
        target.write_bytes(b"theirs changed\n")
        area.check_external_changes()
        assert keys == [tab.key]
        assert tab.editor.plain_text() == "mine\n"

    def test_keep_my_version_clears_the_conflict(
        self, area: EditorArea, sample_project: Path
    ) -> None:
        target = sample_project / "watch.txt"
        target.write_bytes(b"before\n")
        tab = area.open_relative("watch.txt", preview=False)
        assert tab is not None
        tab.editor.setPlainText("mine\n")
        target.write_bytes(b"theirs changed\n")
        area.check_external_changes()
        area.resolve_external_change(tab.key, "keep")
        assert tab.editor.plain_text() == "mine\n"
        area.check_external_changes()
        assert tab.editor.plain_text() == "mine\n"

    def test_reload_decision_adopts_the_disk_version(
        self, area: EditorArea, sample_project: Path
    ) -> None:
        target = sample_project / "watch.txt"
        target.write_bytes(b"before\n")
        tab = area.open_relative("watch.txt", preview=False)
        assert tab is not None
        tab.editor.setPlainText("mine\n")
        target.write_bytes(b"theirs changed\n")
        area.check_external_changes()
        area.resolve_external_change(tab.key, "reload")
        assert tab.editor.plain_text() == "theirs changed\n"


class TestEncodingSelection:
    def test_status_reflects_the_document(self, area: EditorArea, sample_project: Path) -> None:
        target = sample_project / "crlf.py"
        target.write_bytes(b"print('x')\r\n")
        tab = area.open_relative("crlf.py", preview=False)
        assert tab is not None
        info = tab.document.info(language="Python")
        assert info.encoding == "UTF-8"
        assert info.newline == "CRLF"
        assert info.language == "Python"


def test_read_only_file_cannot_be_edited(area: EditorArea, sample_project: Path) -> None:
    target = sample_project / "locked.txt"
    target.write_text("locked\n", encoding="utf-8")
    target.chmod(0o444)
    try:
        tab = area.open_relative("locked.txt", preview=False)
        assert tab is not None
        assert tab.editor.isReadOnly() is True
    finally:
        target.chmod(0o644)


def test_find_options_are_forwarded(area: EditorArea, sample_project: Path) -> None:
    target = sample_project / "options.txt"
    target.write_text("word\n", encoding="utf-8")
    tab = area.open_relative("options.txt", preview=False)
    assert tab is not None
    seen: list[tuple[FindOptions, bool]] = []
    area.find_bar.find_requested.connect(lambda options, forward: seen.append((options, forward)))
    area.find_bar.search_input.setText("word")
    area.find_bar.case_box.setChecked(True)
    area.find_bar.next_button.click()
    assert seen
    options, forward = seen[-1]
    assert options.text == "word"
    assert options.case_sensitive is True
    assert forward is True
