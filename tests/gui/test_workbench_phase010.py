"""Phase 0.10 workbench tests: shell, explorer, search, AI context, persistence.

Everything here drives the real :class:`MainWindow` on top of a real
:class:`ApplicationContext`, with only the network provider replaced. The tests
therefore cover the wiring between the workbench widgets and the workspace
services, which is exactly the seam Phase 0.10 adds.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is required for GUI tests")

from PySide6.QtCore import QElapsedTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTreeWidgetItem

from quantsmind_agent.diagnostics.memory import MemoryPressure, MemorySnapshot
from quantsmind_agent.gui.conversation.context import ContextAttachment, ContextBar
from quantsmind_agent.gui.shell.commands import Command, CommandPalette, CommandRegistry
from quantsmind_agent.gui.shell.notifications import NotificationBar
from quantsmind_agent.gui.shell.quick_open import QuickOpenDialog
from quantsmind_agent.gui.shell.status_bar import WorkbenchStatusBar
from quantsmind_agent.gui.shell.zones import Activity, ActivityBar, BottomPanel, TrustBanner
from quantsmind_agent.persistence.editor_state import EditorTabState, LayoutState
from quantsmind_agent.security.workspace_trust import WorkspaceTrustState
from quantsmind_agent.services.workspace.explorer import SearchOutcome
from tests.fakes import text_response
from tests.gui.harness import GuiHarness

pytestmark = pytest.mark.gui


def _wait(harness: GuiHarness, predicate: Callable[[], bool], timeout_ms: int = 10_000) -> bool:
    """Pump the GUI thread until ``predicate`` holds. False on timeout."""

    timer = QElapsedTimer()
    timer.start()
    while timer.elapsed() < timeout_ms:
        harness._drain(1)
        if predicate():
            return True
    return bool(predicate())


@pytest.fixture
def workbench(gui_harness: GuiHarness, sample_project: Path) -> Iterator[GuiHarness]:
    """A window with ``sample_project`` open, left with no unsaved buffers."""

    gui_harness.open_project(sample_project)
    gui_harness.settle()
    yield gui_harness
    gui_harness.window.editor_area.close_all(discard=True)


def _requests(harness: GuiHarness) -> list[str]:
    """Every message content the provider has seen, flattened."""

    contents: list[str] = []
    for request in [*harness.provider.requests, *harness.provider.stream_requests]:
        contents.extend(message.content for message in request.messages)
    return contents


def _child_names(root: QTreeWidgetItem) -> set[str]:
    names: set[str] = set()
    for index in range(root.childCount()):
        child = root.child(index)
        assert child is not None
        names.add(child.text(0))
    return names


class TestWorkbenchShell:
    def test_activity_selection_switches_side_panel(self, workbench: GuiHarness) -> None:
        window = workbench.window
        window._show_activity("search")
        assert window._side_stack.currentWidget() is window.search_panel
        assert window.activity_bar.current() == "search"

        window._show_activity("sessions")
        assert window._side_stack.currentWidget() is window.sidebar

        window._show_activity("unknown")
        assert window._side_stack.currentWidget() is window.explorer

    def test_activity_bar_widget_emits_the_selected_id(self, qapp: object) -> None:
        bar = ActivityBar([Activity("a", "Alpha", "first"), Activity("b", "Beta", "second")])
        chosen: list[str] = []
        bar.activity_selected.connect(chosen.append)
        bar._buttons["b"].click()
        assert chosen == ["b"]
        assert bar.current() == "b"
        bar.deleteLater()

    def test_trust_banner_widget(self, qapp: object) -> None:
        banner = TrustBanner()
        assert banner.isHidden() is True
        banner.set_restricted(workspace="/tmp/project")
        assert banner.isHidden() is False
        assert "/tmp/project" in banner.label.text()
        assert "restricted mode" in banner.label.text()
        requested: list[bool] = []
        banner.trust_requested.connect(lambda: requested.append(True))
        banner.trust_button.click()
        assert requested == [True]
        banner.set_restricted(workspace=None)
        assert banner.isHidden() is True
        banner.deleteLater()

    def test_bottom_panel_shows_problems_and_output(self, qapp: object) -> None:
        panel = BottomPanel()
        assert panel.isHidden() is True
        panel.toggle()
        assert panel.isHidden() is False
        panel.show_output("build complete")
        assert panel.output.text() == "build complete"
        assert panel.tabs.currentWidget() is panel.output_page
        panel.show_problems()
        assert panel.tabs.currentWidget() is panel.problems_page
        assert "No problems" in panel.problems.text()
        # Real diagnostics replace the truthful empty state.
        panel.set_problems(
            {"file:///x.py": [{"range": {"start": {"line": 0, "character": 1}, "end": {"line": 0, "character": 2}}, "severity": 1, "message": "bad"}]},
            status="language server ready: pylsp",
        )
        assert "bad" in panel.problems.text()
        assert "x.py" in panel.problems.text()
        panel.deleteLater()

    def test_status_bar_widget_surface(self, qapp: object) -> None:
        bar = WorkbenchStatusBar()
        bar.set_model("model @ url")
        bar.set_workspace("/tmp/project")
        bar.set_trust("trusted")
        bar.set_cursor(4, 9)
        bar.set_encoding("UTF-8", "LF")
        bar.set_language("Python")
        bar.set_memory("120 MB (normal)")
        bar.set_activity("read_file: started")
        assert bar.model_label.text() == "model @ url"
        assert bar.workspace_label.text() == "/tmp/project"
        assert bar.trust_label.text() == "Workspace: trusted"
        assert bar.cursor_label.text() == "Ln 4, Col 9"
        assert bar.encoding_label.text() == "UTF-8  LF"
        assert bar.language_label.text() == "Python"
        assert bar.memory_label.text() == "Memory 120 MB (normal)"
        assert bar.activity_label.text() == "read_file: started"
        bar.deleteLater()

    def test_window_status_bar_reports_workspace_and_cursor(self, workbench: GuiHarness) -> None:
        window = workbench.window
        assert str(workbench.context.workspace.root) in window.status_bar.workspace_label.text()  # type: ignore[union-attr]
        assert window.status_bar.trust_label.text() == "Workspace: trusted"
        assert window.status_bar.memory_label.text().startswith("Memory ")

        tab = window.editor_area.open_relative("src/app/main.py", preview=False)
        assert tab is not None
        tab.editor.goto_line(4, 2)
        assert tab.editor.current_line_column() == (4, 2)
        assert window.status_bar.cursor_label.text() == "Ln 4, Col 2"
        assert "UTF-8" in window.status_bar.encoding_label.text()
        assert window.status_bar.language_label.text() == "Python"

    def test_notification_bar_shows_and_dismisses(self, qapp: object) -> None:
        bar = NotificationBar()
        bar.show_message("hello", kind="info")
        assert bar.isHidden() is False
        assert bar.current_text() == "hello"
        assert bar.kind == "info"
        bar.clear()
        assert bar.isHidden() is True
        assert bar.current_text() == ""
        bar.deleteLater()

    def test_memory_pressure_produces_one_warning(
        self, workbench: GuiHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = workbench.window
        governor = workbench.context.resources
        critical = MemorySnapshot(
            working_set_bytes=3_200 * 1024 * 1024,
            pressure=MemoryPressure.CRITICAL,
        )
        monkeypatch.setattr(governor, "snapshot", lambda *, force=False: critical)
        window._update_memory_status()
        assert "(critical)" in window.status_bar.memory_label.text()
        assert window.notification_bar.kind == "error"
        assert "Memory use is high" in window.notification_bar.current_text()

        # A second sample does not repeat the warning while the condition holds.
        window._update_memory_status()
        assert "Memory use is high" in window.notification_bar.current_text()

        normal = MemorySnapshot(working_set_bytes=100 * 1024 * 1024)
        monkeypatch.setattr(governor, "snapshot", lambda *, force=False: normal)
        window._update_memory_status()
        assert "(normal)" in window.status_bar.memory_label.text()


class TestExplorerIntegration:
    def test_tree_is_bound_and_expands_lazily(self, workbench: GuiHarness) -> None:
        explorer = workbench.window.explorer
        assert explorer.header.text() == workbench.context.workspace.root.name  # type: ignore[union-attr]
        root = explorer.tree.topLevelItem(0)
        assert root is not None
        names = _child_names(root)
        assert {"README.md", "pyproject.toml", "src", "docs", "logo.png"} <= names
        assert ".git" not in names

        assert explorer.select_path("src/app/main.py") is True
        assert explorer.selected_relative() == "src/app/main.py"
        expanded = explorer.expanded_directories()
        assert {"src", "src/app"} <= set(expanded)

    def test_hidden_toggle_reveals_hidden_entries(
        self, workbench: GuiHarness, sample_project: Path
    ) -> None:
        (sample_project / ".hidden-note.txt").write_text("hidden\n", encoding="utf-8")
        explorer = workbench.window.explorer

        def names() -> set[str]:
            # The toggle rebuilds the tree, so the root item is re-read each time.
            root = explorer.tree.topLevelItem(0)
            assert root is not None
            return _child_names(root)

        assert ".hidden-note.txt" not in names()
        explorer.hidden_button.setChecked(True)
        assert ".hidden-note.txt" in names()

    def test_activating_a_tree_item_opens_a_preview_tab(self, workbench: GuiHarness) -> None:
        explorer = workbench.window.explorer
        assert explorer.select_path("src/app/settings.py") is True
        item = explorer.current_item()
        assert item is not None
        explorer.tree.itemActivated.emit(item, 0)
        assert workbench.window.editor_area.current_relative() == "src/app/settings.py"

    def test_creating_a_file_opens_it(self, workbench: GuiHarness, sample_project: Path) -> None:
        explorer = workbench.window.explorer
        created = explorer.create_file(parent="docs", name="notes.md")
        assert created == "docs/notes.md"
        assert (sample_project / "docs" / "notes.md").is_file()
        assert workbench.window.editor_area.current_relative() == "docs/notes.md"

    def test_renaming_updates_the_open_tab(
        self, workbench: GuiHarness, sample_project: Path
    ) -> None:
        window = workbench.window
        assert window.editor_area.open_relative("src/app/main.py", preview=False) is not None
        renamed = window.explorer.rename("src/app/main.py", "entry.py")
        assert renamed == "src/app/entry.py"
        assert (sample_project / "src" / "app" / "entry.py").is_file()
        assert window.editor_area.current_relative() == "src/app/entry.py"

    def test_deleting_a_folder_closes_its_tabs(self, workbench: GuiHarness) -> None:
        window = workbench.window
        assert window.editor_area.open_relative("docs/usage.md", preview=False) is not None
        window.explorer.path_deleted.emit("docs")
        assert window.editor_area.current_tab() is None
        assert window.editor_area.open_documents() == ()

    def test_explorer_notices_reach_the_window(self, workbench: GuiHarness) -> None:
        workbench.window.explorer.notice.emit("disk is full")
        assert workbench.window.notification_bar.current_text() == "disk is full"

    def test_create_refuses_an_existing_name(self, workbench: GuiHarness) -> None:
        explorer = workbench.window.explorer
        assert explorer.create_file(parent="docs", name="usage.md") == ""
        assert "already exists" in workbench.window.notification_bar.current_text()


class TestSearchPanelIntegration:
    def test_panel_is_bound_to_the_open_workspace(self, workbench: GuiHarness) -> None:
        panel = workbench.window.search_panel
        assert panel.service is workbench.window.explorer.service
        assert panel.query_edit.isEnabled() is True
        assert panel.search_button.isEnabled() is True

    def test_search_finds_text_and_navigates_to_the_line(self, workbench: GuiHarness) -> None:
        panel = workbench.window.search_panel
        panel.query_edit.setText("DEFAULT_PORT")
        panel.search_now()
        assert _wait(workbench, lambda: not panel.is_searching and panel.result_count() > 0)

        assert panel.result_count() == 2
        assert "2 match" in panel.status_label.text()
        group = panel.results.topLevelItem(0)
        assert group is not None
        assert group.text(0) == "src/app/settings.py"
        assert group.childCount() == 2
        child = group.child(0)
        assert child is not None
        assert "Ln 3" in child.text(0)

        panel._on_result_activated(child, 0)
        assert workbench.window.editor_area.current_relative() == "src/app/settings.py"
        assert workbench.window.editor_area.current_editor().current_line_column()[0] == 3  # type: ignore[union-attr]

    def test_empty_query_is_refused_without_starting_work(self, workbench: GuiHarness) -> None:
        panel = workbench.window.search_panel
        panel.query_edit.setText("")
        panel.search_now()
        assert panel.status_label.text() == "Enter text to search for."
        assert panel.is_searching is False

    def test_invalid_regex_is_reported(self, workbench: GuiHarness) -> None:
        panel = workbench.window.search_panel
        panel.query_edit.setText("[")
        panel.regex_box.setChecked(True)
        panel.search_now()
        assert _wait(workbench, lambda: not panel.is_searching)
        assert "not valid" in panel.status_label.text()
        assert "not valid" in workbench.window.notification_bar.current_text()

    def test_cache_options_are_forwarded_to_the_query(self, workbench: GuiHarness) -> None:
        panel = workbench.window.search_panel
        panel.query_edit.setText("needle")
        panel.include_edit.setText("*.py")
        panel.exclude_edit.setText("*_test.py")
        panel.case_box.setChecked(True)
        panel.word_box.setChecked(True)
        panel.regex_box.setChecked(True)
        query = panel.query()
        assert query.text == "needle"
        assert query.include == "*.py"
        assert query.exclude == "*_test.py"
        assert query.case_sensitive is True
        assert query.whole_word is True
        assert query.use_regex is True

    def test_cancel_stops_a_slow_search(
        self, workbench: GuiHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = workbench.window.search_panel
        service = panel.service
        assert service is not None

        def slow_search(query: object, cancel: threading.Event | None = None) -> SearchOutcome:
            for _ in range(2000):
                if cancel is not None and cancel.is_set():
                    return SearchOutcome(cancelled=True)
                time.sleep(0.002)
            return SearchOutcome()

        monkeypatch.setattr(service, "search_text", slow_search)
        panel.query_edit.setText("anything")
        panel.search_now()
        assert panel.is_searching is True
        panel.cancel()
        assert _wait(workbench, lambda: not panel.is_searching)
        assert panel.status_label.text() == "Search cancelled."


class TestQuickOpenIntegration:
    def test_title_search_ranks_recent_files_first(self, workbench: GuiHarness) -> None:
        service = workbench.window.explorer.service
        assert service is not None
        dialog = QuickOpenDialog()
        try:
            dialog.set_workspace(service, recent=["src/app/settings.py"])
            assert _wait(workbench, lambda: len(dialog.result_paths()) > 0)
            assert dialog.result_paths()[0] == "src/app/settings.py"

            dialog.search_now("main")
            assert _wait(workbench, lambda: "src/app/main.py" in dialog.result_paths())
            row = dialog.result_paths().index("src/app/main.py")
            dialog.results.setCurrentRow(row)
            chosen: list[str] = []
            dialog.file_selected.connect(chosen.append)
            dialog.open_current()
            assert chosen == ["src/app/main.py"]
        finally:
            dialog.shutdown()
            dialog.deleteLater()

    def test_no_match_is_reported(self, workbench: GuiHarness) -> None:
        service = workbench.window.explorer.service
        assert service is not None
        dialog = QuickOpenDialog()
        try:
            dialog.set_workspace(service)
            dialog.search_now("zzzz-no-such-file")
            assert _wait(workbench, lambda: dialog.status.text().startswith("No matching file"))
            assert dialog.result_paths() == []
        finally:
            dialog.shutdown()
            dialog.deleteLater()

    def test_no_workspace_is_explained(self, qapp: object) -> None:
        dialog = QuickOpenDialog()
        dialog.set_workspace(None)
        assert "Open a folder" in dialog.status.text()
        dialog.shutdown()
        dialog.deleteLater()

    def test_shutdown_cancels_an_in_flight_walk(
        self, workbench: GuiHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = workbench.window.explorer.service
        assert service is not None

        def slow_open(
            query: str, *, limit: int = 100, cancel: object = None, recent: object = ()
        ) -> list[object]:
            event = cancel if isinstance(cancel, threading.Event) else None
            for _ in range(2000):
                if event is not None and event.is_set():
                    return []
                time.sleep(0.002)
            return []

        monkeypatch.setattr(service, "quick_open", slow_open)
        dialog = QuickOpenDialog()
        dialog.set_workspace(service)
        dialog.search_now("anything")
        assert dialog._workers
        dialog.shutdown()
        dialog.shutdown()
        assert dialog._workers == set()
        dialog.deleteLater()


class TestCommandPaletteIntegration:
    def test_registry_matching_is_deterministic(self) -> None:
        registry = CommandRegistry()
        registry.register_all(
            [
                Command(id="file.save", title="Save", callback=lambda: None),
                Command(id="file.save_all", title="Save All", callback=lambda: None),
                Command(id="workspace.open", title="Open Folder", callback=lambda: None),
                Command(
                    id="only.disabled",
                    title="Save Disabled",
                    callback=lambda: None,
                    enabled=lambda: False,
                ),
            ]
        )
        assert registry.search("save")[0].id == "file.save"
        assert registry.get("file.save_all") is not None
        assert registry.get("nope") is None
        assert registry.search("disabled") == []
        assert registry.search("disabled", include_disabled=True)[0].id == "only.disabled"
        assert registry.search("svall")[0].id == "file.save_all"

    def test_palette_requires_confirmation_for_dangerous_commands(self, qapp: object) -> None:
        registry = CommandRegistry()
        ran: list[str] = []
        registry.register(
            Command(
                id="safe",
                title="Safe Action",
                callback=lambda: ran.append("safe"),
                dangerous=False,
            )
        )
        registry.register(
            Command(
                id="risky",
                title="Risky Action",
                callback=lambda: ran.append("risky"),
                dangerous=True,
            )
        )
        palette = CommandPalette(registry, confirm_dangerous=lambda _command: False)
        palette.input.setText("risky")
        palette.refresh()
        palette.activate_current()
        assert ran == []

        palette2 = CommandPalette(registry, confirm_dangerous=lambda _command: True)
        palette2.input.setText("risky")
        palette2.refresh()
        palette2.activate_current()
        assert ran == ["risky"]
        palette.deleteLater()
        palette2.deleteLater()

    def test_command_enablement_follows_workspace_and_tabs(self, workbench: GuiHarness) -> None:
        window = workbench.window
        registry = window._command_registry

        def enabled(command_id: str) -> bool:
            command = registry.get(command_id)
            assert command is not None
            return bool(command.is_enabled())

        assert enabled("workspace.close") is True
        assert enabled("workspace.refresh") is True
        assert enabled("workspace.trust") is False
        assert enabled("workspace.restrict") is True
        assert enabled("file.quick_open") is True
        assert enabled("file.save") is False
        assert enabled("assistant.send") is True

        assert window.editor_area.open_relative("README.md", preview=False) is not None
        assert enabled("file.save") is True
        assert enabled("view.find") is True
        assert enabled("view.split") is True

    def test_window_palette_is_created_and_released(self, workbench: GuiHarness) -> None:
        window = workbench.window
        window._show_command_palette()
        palette = window._command_palette
        assert palette is not None
        palette.input.setText("quick open")
        palette.refresh()
        assert palette.commands()
        assert palette.commands()[0].id == "file.quick_open"
        palette.reject()
        assert window._command_palette is None


class TestAssistantContext:
    def test_nothing_is_attached_automatically(self, workbench: GuiHarness) -> None:
        window = workbench.window
        assert window.editor_area.open_relative("src/app/main.py", preview=False) is not None
        assert window.context_bar.attachments() == ()
        assert "Attached context" not in window.context_bar.prompt_suffix()

        workbench.provider.script(text_response("Received."))
        workbench.ask("What does this do?")
        assert not any("Attached context" in content for content in _requests(workbench))
        assert "def main" not in workbench.transcript

    def test_attached_selection_is_sent_only_with_the_next_message(
        self, workbench: GuiHarness
    ) -> None:
        window = workbench.window
        tab = window.editor_area.open_relative("src/app/main.py", preview=False)
        assert tab is not None
        tab.editor.selectAll()

        window._attach_selection()
        attachments = window.context_bar.attachments()
        assert len(attachments) == 1
        assert attachments[0].kind == "selection"
        assert attachments[0].relative == "src/app/main.py"
        assert "def main" in attachments[0].content
        assert attachments[0].label.startswith("selection ")
        assert "sent only when you send" in window.notification_bar.current_text()
        # Attaching alone transmits nothing.
        assert _requests(workbench) == []

        workbench.provider.script(text_response("Received."))
        workbench.ask("Explain this.")
        contents = _requests(workbench)
        assert any("Attached context" in content and "def main" in content for content in contents)
        assert "def main" not in workbench.transcript
        assert window.context_bar.attachments() == ()

    def test_dirty_file_attachment_is_marked_as_unsaved(self, workbench: GuiHarness) -> None:
        window = workbench.window
        tab = window.editor_area.open_relative("src/app/main.py", preview=False)
        assert tab is not None
        tab.editor.appendPlainText("# local edit")
        assert tab.document.dirty is True

        window._attach_current_file()
        attachments = window.context_bar.attachments()
        assert len(attachments) == 1
        assert attachments[0].kind == "file"
        assert "unsaved editor buffer" in attachments[0].note
        assert "# local edit" in attachments[0].content

    def test_duplicate_attachment_is_refused(self, workbench: GuiHarness) -> None:
        window = workbench.window
        assert window.editor_area.open_relative("src/app/main.py", preview=False) is not None
        window._attach_current_file()
        window._attach_current_file()
        assert len(window.context_bar.attachments()) == 1
        assert "already attached" in window.notification_bar.current_text()

    def test_empty_selection_is_explained(self, workbench: GuiHarness) -> None:
        window = workbench.window
        tab = window.editor_area.open_relative("src/app/main.py", preview=False)
        assert tab is not None
        tab.editor.moveCursor(QTextCursor.MoveOperation.Start)
        window._attach_selection()
        assert window.context_bar.attachments() == ()
        assert "Select some code" in window.notification_bar.current_text()

    def test_binary_files_cannot_be_attached(self, workbench: GuiHarness) -> None:
        window = workbench.window
        assert window.editor_area.open_relative("logo.png", preview=False) is not None
        window._attach_current_file()
        assert window.context_bar.attachments() == ()
        assert "binary or unsupported" in window.notification_bar.current_text()

    def test_no_workspace_is_explained(self, workbench: GuiHarness) -> None:
        window = workbench.window
        window._on_close_workspace()
        window._attach_current_file()
        assert window.context_bar.attachments() == ()
        assert "Open a folder" in window.notification_bar.current_text()

    def test_context_bar_limits_and_formats_attachments(self, qapp: object) -> None:
        bar = ContextBar()
        try:
            assert (
                bar.add_attachment(
                    ContextAttachment(kind="file", relative="a.py", content="print(1)\n")
                )
                is False
            )
            bar.set_available(True)
            assert (
                bar.add_attachment(
                    ContextAttachment(kind="file", relative="a.py", content="print(1)\n")
                )
                is True
            )
            assert (
                bar.add_attachment(
                    ContextAttachment(kind="file", relative="a.py", content="print(1)\n")
                )
                is False
            )
            huge = ContextAttachment(kind="file", relative="b.py", content="x" * 50_000)
            assert bar.add_attachment(huge) is True
            stored = bar.attachments()[1]
            assert len(stored.content) == 40_000
            assert "truncated" in stored.note
            assert "Attached context" in bar.prompt_suffix()
            bar.clear()
            assert bar.attachments() == ()
            assert bar.prompt_suffix() == ""
        finally:
            bar.deleteLater()

    def test_the_window_uses_the_existing_gateway(self, workbench: GuiHarness) -> None:
        assert workbench.context.gateway.provider is workbench.provider


class TestTrustAndRestrictedMode:
    def test_trusted_workspace_shows_no_banner(self, workbench: GuiHarness) -> None:
        window = workbench.window
        assert window.trust_banner.isHidden() is True
        assert window.status_bar.trust_label.text() == "Workspace: trusted"
        assert window.input.isEnabled() is True
        assert window.context_bar.available is True

    def test_restricting_disables_the_agent_but_keeps_editing(self, workbench: GuiHarness) -> None:
        window = workbench.window
        workspace = workbench.context.workspace
        assert workspace is not None
        workbench.context.set_workspace_trust(False)
        window._refresh_from_context()

        assert window.trust_banner.isHidden() is False
        assert "restricted mode" in window.trust_banner.label.text()
        assert window.status_bar.trust_label.text() == "Workspace: restricted"
        assert window.input.isEnabled() is False
        assert window.send_button.isEnabled() is False
        assert window.context_bar.available is False
        assert (
            workbench.context.trust_store.state_for(workspace.root)
            is WorkspaceTrustState.RESTRICTED
        )

        registry = window._command_registry
        assert registry.get("workspace.trust").is_enabled() is True  # type: ignore[union-attr]
        assert registry.get("workspace.restrict").is_enabled() is False  # type: ignore[union-attr]
        assert registry.get("workspace.refresh").is_enabled() is False  # type: ignore[union-attr]
        assert registry.get("assistant.send").is_enabled() is False  # type: ignore[union-attr]
        assert registry.get("assistant.attach_file").is_enabled() is False  # type: ignore[union-attr]
        assert registry.get("file.quick_open").is_enabled() is True  # type: ignore[union-attr]

        # Local browsing, editing and saving remain available.
        tab = window.editor_area.open_relative("src/app/settings.py", preview=False)
        assert tab is not None
        tab.editor.appendPlainText("# edited while restricted")
        assert window.editor_area.save_current() is True
        assert "edited while restricted" in (
            workspace.root / "src" / "app" / "settings.py"
        ).read_text(encoding="utf-8")
        assert tab.document.dirty is False

    def test_restricted_mode_refuses_attachments_and_sends(self, workbench: GuiHarness) -> None:
        window = workbench.window
        workbench.context.set_workspace_trust(False)
        window._refresh_from_context()
        tab = window.editor_area.open_relative("src/app/main.py", preview=False)
        assert tab is not None
        tab.editor.selectAll()

        window._attach_selection()
        assert window.context_bar.attachments() == ()
        assert "restricted" in window.notification_bar.current_text()

        window.input.setPlainText("hello")
        window._on_send_clicked()
        assert _requests(workbench) == []
        assert "restricted" in window.notification_bar.current_text()
        assert "hello" not in workbench.transcript

    def test_trusting_again_restores_the_agent(self, workbench: GuiHarness) -> None:
        window = workbench.window
        workbench.context.set_workspace_trust(False)
        window._refresh_from_context()
        workbench.context.set_workspace_trust(True)
        window._refresh_from_context()

        assert window.trust_banner.isHidden() is True
        assert window.status_bar.trust_label.text() == "Workspace: trusted"
        assert window.input.isEnabled() is True
        assert window.context_bar.available is True
        assert window._command_registry.get("assistant.send").is_enabled() is True  # type: ignore[union-attr]


class TestLayoutAndEditorPersistence:
    def test_bottom_panel_visibility_is_persisted(self, workbench: GuiHarness) -> None:
        window = workbench.window
        assert window.bottom_panel.isHidden() is True
        window._toggle_bottom_panel()
        assert window.bottom_panel.isHidden() is False
        window._save_layout()
        assert workbench.context.editor_state.layout().bottom_visible is True

    def test_selected_activity_is_persisted(self, workbench: GuiHarness) -> None:
        window = workbench.window
        window._show_activity("search")
        window._save_layout()
        assert workbench.context.editor_state.layout().activity == "search"

    def test_layout_restores_visibility_and_activity(self, workbench: GuiHarness) -> None:
        window = workbench.window
        window._apply_layout(
            LayoutState(
                sidebar_visible=False,
                agent_visible=False,
                bottom_visible=True,
                activity="sessions",
            )
        )
        assert window._side_stack.isHidden() is True
        assert window._agent_panel.isHidden() is True
        assert window.bottom_panel.isHidden() is False
        assert window._activity == "sessions"
        assert window.activity_bar.current() == "sessions"

    def test_saved_tabs_are_restored(self, workbench: GuiHarness) -> None:
        window = workbench.window
        assert window.editor_area.open_relative("src/app/main.py", preview=False) is not None
        assert window.editor_area.open_relative("src/app/settings.py", preview=False) is not None
        window._save_editor_state()
        window.editor_area.close_all(discard=True)

        workspace = workbench.context.workspace
        assert workspace is not None
        window._restore_editor_tabs(workspace)
        restored = {tab.path for tab in window.editor_area.tab_states()}
        assert restored == {"src/app/main.py", "src/app/settings.py"}

    def test_missing_restored_file_is_reported(self, workbench: GuiHarness) -> None:
        window = workbench.window
        workspace = workbench.context.workspace
        assert workspace is not None
        window.editor_area.close_all(discard=True)
        workbench.context.editor_state.save_tabs(
            workspace.root,
            [
                EditorTabState(path="src/app/main.py"),
                EditorTabState(path="src/app/removed.py"),
            ],
        )
        window._restore_editor_tabs(workspace)
        assert {tab.path for tab in window.editor_area.tab_states()} == {"src/app/main.py"}
        message = window.notification_bar.current_text()
        assert "could not be reopened" in message
        assert "src/app/removed.py" in message

    def test_tab_save_survives_a_new_store_instance(self, workbench: GuiHarness) -> None:
        window = workbench.window
        assert window.editor_area.open_relative("README.md", preview=False) is not None
        window._save_editor_state()
        path = workbench.context.paths.editor_state_file
        assert path.is_file()
        from quantsmind_agent.persistence.editor_state import EditorStateStore

        reloaded = EditorStateStore(path)
        workspace = workbench.context.workspace
        assert workspace is not None
        state = reloaded.state_for(workspace.root)
        assert state is not None
        assert [tab.path for tab in state.tabs] == ["README.md"]
