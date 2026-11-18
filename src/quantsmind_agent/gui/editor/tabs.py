"""Editor tabs, panes and the save/close lifecycle.

The area owns one :class:`~quantsmind_agent.gui.editor.document.DocumentRegistry`
shared by every pane, so the same file never has two divergent buffers. Each
pane is a normal :class:`QTabWidget`; Ctrl+\\ adds a pane up to the four the
blueprint allows.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.code_intelligence import CodeIntelligence
from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import InvalidPathError, QuantsMindError
from quantsmind_agent.gui.editor.document import (
    DocumentInfo,
    DocumentRegistry,
    TextDocument,
)
from quantsmind_agent.gui.editor.editor_widget import CodeEditor
from quantsmind_agent.gui.editor.search import FindBar, FindOptions
from quantsmind_agent.gui.editor.syntax import language_for_path, language_label
from quantsmind_agent.persistence.editor_state import EditorTabState
from quantsmind_agent.services.workspace.containment import Workspace

__all__ = ["CloseDecision", "EditorArea", "EditorPane", "OpenTab"]

logger = get_logger(__name__)

CloseDecision = Literal["save", "discard", "cancel"]
MAX_PANES = 4
REPLACE_CONFIRM_THRESHOLD = 100
#: Keep the open-buffer ceiling aligned with the number of tabs the editor
#: state store will restore, so reopening a session cannot evict what was saved.
MAX_OPEN_DOCUMENTS = 50
#: Under critical memory pressure only this many buffers stay open.
DEGRADED_OPEN_DOCUMENTS = 20


@dataclass(slots=True)
class OpenTab:
    key: str
    document: TextDocument
    editor: CodeEditor
    page: QWidget
    notice: QLabel
    edit_anyway: QPushButton | None = None
    preview: bool = False
    last_used: int = 0

    @property
    def name(self) -> str:
        return self.document.name


@dataclass(slots=True)
class _PageParts:
    page: QWidget
    notice: QLabel
    button: QPushButton | None


class EditorPane(QTabWidget):
    """One tab strip and its editors."""

    tab_activated = Signal(object)  # OpenTab | None
    tab_dirty_changed = Signal(object)  # OpenTab
    close_requested = Signal(int)
    cursor_moved = Signal(int, int)
    tabs_changed = Signal()

    def __init__(
        self,
        registry: DocumentRegistry,
        intelligence: CodeIntelligence | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._intelligence = intelligence
        self._lsp_manager: Any = None
        self._tabs: dict[str, OpenTab] = {}
        self.preview_key: str | None = None
        self.title_provider: Callable[[TextDocument], str] = lambda document: document.name
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)
        self.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.setUsesScrollButtons(True)
        self.tabCloseRequested.connect(self.close_requested.emit)
        self.currentChanged.connect(self._on_current_changed)

    # -- LSP lifecycle ------------------------------------------------------
    def set_lsp_manager(self, manager: Any) -> None:
        """Attach an LSPManager; existing tabs are (re)synced as didOpen."""
        self._lsp_manager = manager
        for tab in list(self._tabs.values()):
            self._lsp_open(tab)

    def _lsp_uri(self, document: TextDocument) -> str:
        return f"file:///{str(document.path).replace(chr(92), '/')}"

    def _lsp_language(self, document: TextDocument) -> str:
        return language_for_path(document.path) or "plaintext"

    def _lsp_open(self, tab: OpenTab) -> None:
        if self._lsp_manager is None:
            return
        with contextlib.suppress(Exception):
            self._lsp_manager.did_open(
                self._lsp_uri(tab.document),
                self._lsp_language(tab.document),
                tab.editor.plain_text(),
            )

    def _lsp_change(self, tab: OpenTab) -> None:
        if self._lsp_manager is None:
            return
        with contextlib.suppress(Exception):
            self._lsp_manager.did_change(self._lsp_uri(tab.document), tab.editor.plain_text())

    def _lsp_close(self, tab: OpenTab) -> None:
        if self._lsp_manager is None:
            return
        with contextlib.suppress(Exception):
            self._lsp_manager.did_close(self._lsp_uri(tab.document))

    # -- editor intelligence (LSP, with Tier-A fallback) ---------------------
    def hover_at(self, line: int, column: int) -> dict[str, Any] | None:
        tab = self.active_tab()
        if tab is None or self._lsp_manager is None:
            return None
        try:
            result = self._lsp_manager.hover(self._lsp_uri(tab.document), line, column)
            return result if result is not None else None
        except Exception:
            return None

    def completion_at(self, line: int, column: int) -> list[dict[str, Any]]:
        tab = self.active_tab()
        if tab is None or self._lsp_manager is None:
            return []
        try:
            result = self._lsp_manager.completion(self._lsp_uri(tab.document), line, column)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    def definition_at(self, line: int, column: int) -> list[dict[str, Any]]:
        tab = self.active_tab()
        if tab is None or self._lsp_manager is None:
            return []
        try:
            result = self._lsp_manager.definition(self._lsp_uri(tab.document), line, column)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    def references_at(self, line: int, column: int) -> list[dict[str, Any]]:
        tab = self.active_tab()
        if tab is None or self._lsp_manager is None:
            return []
        try:
            result = self._lsp_manager.references(self._lsp_uri(tab.document), line, column)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    def symbols_fallback(self, text: str) -> Any:
        """Tier-A AST symbols regardless of LSP availability."""
        if self._lsp_manager is not None:
            try:
                return self._lsp_manager.symbols_fallback(text)
            except Exception:
                pass
        from quantsmind_agent.code_index import parse_symbols

        try:
            return parse_symbols(text)
        except Exception:
            return []

    # -- contents ----------------------------------------------------------
    def tabs(self) -> tuple[OpenTab, ...]:
        return tuple(self._tabs.values())

    def tab_for(self, path: Path | str) -> OpenTab | None:
        return self._tabs.get(DocumentRegistry.key(path))

    def active_tab(self) -> OpenTab | None:
        return self.tab_at(self.currentIndex())

    def tab_at(self, index: int) -> OpenTab | None:
        widget = self.widget(index)
        for tab in self._tabs.values():
            if tab.page is widget:
                return tab
        return None

    def open(self, document: TextDocument) -> OpenTab:
        existing = self.tab_for(document.path)
        if existing is not None:
            self.setCurrentWidget(existing.page)
            return existing

        editor = CodeEditor()
        configured = self._configure_editor(editor, document)
        editor.symbol_hovered.connect(self._on_symbol_hovered)
        editor.lsp_completion_requested.connect(self._on_lsp_completion)
        editor.lsp_definition_requested.connect(self._on_lsp_definition)
        parts = self._build_page(configured, document)
        tab = OpenTab(
            key=DocumentRegistry.key(document.path),
            document=document,
            editor=editor,
            page=parts.page,
            notice=parts.notice,
            edit_anyway=parts.button,
        )
        self._tabs[tab.key] = tab
        index = self.addTab(tab.page, self.title_provider(document))
        self.setTabToolTip(index, str(document.path))
        editor.textChanged.connect(lambda bound=tab: self._mark_modified(bound))
        editor.cursorPositionChanged.connect(lambda bound=tab: self._on_cursor(bound))
        if parts.button is not None:
            parts.button.clicked.connect(
                lambda _checked=False, bound=tab: self.make_editable(bound)
            )
        self.setCurrentIndex(index)
        self.tabs_changed.emit()
        self._lsp_open(tab)
        return tab

    def _on_lsp_completion(self) -> None:
        """Show LSP completions (or Tier-A symbol list) at the cursor."""
        tab = self.active_tab()
        if tab is None:
            return
        line, column = tab.editor.current_line_column()
        items: list[dict[str, object]] = self.completion_at(line, column)
        labels = [str(i.get("label", "")) for i in items if isinstance(i, dict) and i.get("label")][
            :25
        ]
        if not labels:
            # Tier-A fallback: symbols in this buffer.
            symbols = self.symbols_fallback(tab.editor.plain_text())
            labels = [str(getattr(s, "name", "")) for s in symbols if getattr(s, "name", "")][:25]
        if not labels:
            tab.editor.setToolTip(
                "No completions available (no language server; Tier-A fallback empty)."
            )
            return
        from PySide6.QtWidgets import QMenu

        menu = QMenu(tab.editor)
        for label in labels:
            menu.addAction(label)
        menu.exec(tab.editor.mapToGlobal(tab.editor.cursorRect().bottomLeft()))

    def _on_lsp_definition(self) -> None:
        """Go to definition via LSP (tooltip fallback when unavailable)."""
        tab = self.active_tab()
        if tab is None:
            return
        line, column = tab.editor.current_line_column()
        defs = self.definition_at(line, column)
        if not defs:
            tab.editor.setToolTip(
                "No definition found (no language server; Tier-A fallback active)."
            )
            return
        first = defs[0] if isinstance(defs[0], dict) else {}
        uri = str(first.get("uri", ""))
        rng = first.get("range", {}) if isinstance(first, dict) else {}
        start = rng.get("start", {}) if isinstance(rng, dict) else {}
        target_line = int(start.get("line", 0)) + 1
        target_col = int(start.get("character", 0)) + 1
        label = f"{uri.split('/')[-1]}:{target_line}:{target_col}" if uri else f"line {target_line}"
        tab.editor.setToolTip(f"Definition: {label}")

    def _on_symbol_hovered(self, name: str, kind: str) -> None:
        if self._intelligence is None:
            return
        tab = self.active_tab()
        if tab is None:
            return
        info = self._intelligence.intelligence_for(str(tab.document.path))
        for sym in info.symbols:
            if sym.name == name:
                tooltip = f"{sym.kind}: {sym.name}"
                if sym.doc:
                    tooltip += f"\n{sym.doc}"
                editor = tab.editor
                editor.setToolTip(tooltip)
                return

    def _configure_editor(self, editor: CodeEditor, document: TextDocument) -> CodeEditor:
        if document.binary or document.unsupported:
            editor.load_text(document.summary(), readonly=True)
        else:
            editor.load_text(document.text, readonly=document.readonly)
        editor.set_large_file(document.large)
        editor.set_language(language_for_path(document.path))
        if document.readonly and document.reason:
            editor.setToolTip(document.reason)
        return editor

    def _build_page(self, editor: CodeEditor, document: TextDocument) -> _PageParts:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        notice = QLabel(document.reason)
        notice.setObjectName("EditorNotice")
        notice.setWordWrap(True)
        notice.setVisible(bool(document.reason))
        button: QPushButton | None = None
        if document.reason:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 4, 8, 4)
            row_layout.addWidget(notice, 1)
            if document.preview_only:
                button = QPushButton("Open editable anyway")
                button.setObjectName("PrimaryButton")
                row_layout.addWidget(button)
            layout.addWidget(row)
        layout.addWidget(editor, 1)
        return _PageParts(page=page, notice=notice, button=button)

    def make_editable(self, tab: OpenTab) -> None:
        """Lift the read-only preview after an explicit user action."""

        if not tab.document.preview_only:
            return
        old_path = tab.document.path
        try:
            fresh = TextDocument.load(
                tab.document.path,
                relative=tab.document.relative,
                force_editable=True,
            )
        except QuantsMindError as exc:
            tab.notice.setText(exc.user_message)
            return
        old_key = tab.key
        tab.document = fresh
        tab.edit_anyway = None
        tab.key = DocumentRegistry.key(fresh.path)
        self._tabs.pop(old_key, None)
        self._tabs[tab.key] = tab
        self._registry.remove(old_path)
        self._registry.add(fresh)
        if self.preview_key == old_key:
            self.preview_key = tab.key
        tab.editor.load_text(fresh.text, readonly=fresh.readonly)
        tab.editor.set_large_file(fresh.large)
        tab.editor.set_language(language_for_path(fresh.path))
        tab.notice.setText(fresh.reason)
        tab.notice.setVisible(bool(fresh.reason))
        self.refresh_titles()

    # -- state -------------------------------------------------------------
    def _mark_modified(self, tab: OpenTab) -> None:
        # Sync every real edit to LSP (versions tracked in the manager).
        # Tab switches do not re-open: didChange/didClose maintain state.
        self._lsp_change(tab)
        if tab.document.dirty:
            return
        if tab.editor.plain_text() == tab.document.text:
            # Qt re-emits textChanged the first time a freshly loaded buffer is
            # laid out. Treating that as an edit would mark a file the user never
            # touched as unsaved, then ask them to save it on close.
            return
        tab.document.dirty = True
        if tab.preview:
            tab.preview = False
            self.preview_key = None
        self.refresh_titles()
        self.tab_dirty_changed.emit(tab)

    def _on_cursor(self, tab: OpenTab) -> None:
        if self.active_tab() is tab:
            line, column = tab.editor.current_line_column()
            self.cursor_moved.emit(line, column)

    def _on_current_changed(self, _index: int) -> None:
        tab = self.active_tab()
        self.tab_activated.emit(tab)
        if tab is not None:
            line, column = tab.editor.current_line_column()
            self.cursor_moved.emit(line, column)

    def refresh_titles(self) -> None:
        for tab in self._tabs.values():
            index = self.indexOf(tab.page)
            if index < 0:
                continue
            title = self.title_provider(tab.document)
            if tab.document.dirty:
                title += " \u25cf"
            self.setTabText(index, title)
            self.setTabToolTip(index, str(tab.document.path))

    def state(self) -> list[EditorTabState]:
        result: list[EditorTabState] = []
        active = self.active_tab()
        for tab in self._tabs.values():
            line, column = tab.editor.current_line_column()
            result.append(
                EditorTabState(
                    path=tab.document.relative.replace("\\", "/"),
                    cursor_line=line,
                    cursor_column=column,
                    scroll=tab.editor.verticalScrollBar().value(),
                    pinned=not tab.preview,
                    active=tab is active,
                )
            )
        return result

    def remove(self, tab: OpenTab) -> None:
        self._lsp_close(tab)
        index = self.indexOf(tab.page)
        if index < 0:
            return
        self.removeTab(index)
        self._tabs.pop(tab.key, None)
        if self.preview_key == tab.key:
            self.preview_key = None
        tab.editor.deleteLater()
        tab.page.deleteLater()
        self.refresh_titles()
        self.tabs_changed.emit()

    def save(self, tab: OpenTab, target: Path | None = None, *, overwrite: bool = False) -> Path:
        old_path = tab.document.path
        tab.document.text = tab.editor.plain_text()
        written = tab.document.save(target, overwrite=overwrite)
        tab.editor.document().setModified(False)
        tab.document.dirty = False
        old_key = tab.key
        tab.key = DocumentRegistry.key(written)
        if tab.key != old_key:
            self._tabs.pop(old_key, None)
            self._tabs[tab.key] = tab
            self._registry.remove(old_path)
            self._registry.add(tab.document)
        self.refresh_titles()
        return written

    def reload(self, tab: OpenTab) -> None:
        tab.document.reload(force=True)
        tab.editor.load_text(tab.document.text, readonly=tab.document.readonly)
        tab.editor.set_large_file(tab.document.large)
        tab.editor.set_language(language_for_path(tab.document.path))
        tab.notice.setText(tab.document.reason)
        tab.notice.setVisible(bool(tab.document.reason))
        self.refresh_titles()

    def keep_my_version(self, tab: OpenTab) -> None:
        try:
            info = tab.document.path.stat()
        except OSError:
            return
        tab.document.mtime_ns = info.st_mtime_ns
        tab.document.size_bytes = info.st_size


class EditorArea(QWidget):
    """Tabs, panes, save/close and external-change handling."""

    document_saved = Signal(str)
    dirty_changed = Signal(bool)
    active_document_changed = Signal(object)  # DocumentInfo | None
    cursor_changed = Signal(int, int)
    external_change_detected = Signal(str)
    notice = Signal(str)
    tabs_changed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        intelligence: CodeIntelligence | None = None,
        confirm_close: Callable[[TextDocument], CloseDecision] | None = None,
        confirm_overwrite: Callable[[Path], bool] | None = None,
        confirm_replace_all: Callable[[int], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace: Workspace | None = None
        self._intelligence: CodeIntelligence | None = intelligence
        self._registry = DocumentRegistry()
        self._panes: list[EditorPane] = []
        self._active_pane: EditorPane | None = None
        self._pending_external: set[str] = set()
        self._use_counter = 0
        self._confirm_close = confirm_close or self._ask_close
        self._confirm_overwrite = confirm_overwrite or self._ask_overwrite
        self._confirm_replace_all = confirm_replace_all or self._ask_replace_all

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.find_bar = FindBar()
        self.find_bar.hide()
        layout.addWidget(self.find_bar)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        self._welcome = self._build_welcome()
        self._stack.addWidget(self._welcome)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._stack.addWidget(self._splitter)

        self._add_pane()
        self.find_bar.find_requested.connect(self._on_find)
        self.find_bar.replace_requested.connect(self._on_replace)
        self.find_bar.replace_all_requested.connect(self._on_replace_all)

    def _current_editor(self) -> CodeEditor | None:
        pane = self._active_pane
        if pane is None:
            return None
        tab = pane.active_tab()
        if tab is None:
            return None
        return tab.editor

    def set_lsp_manager(self, manager: Any) -> None:
        """Attach an LSPManager to every pane (WP4)."""
        for pane in self._panes:
            try:
                if hasattr(pane, "set_lsp_manager"):
                    pane.set_lsp_manager(manager)
            except Exception:
                pass

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        label = QLabel(
            "Open a folder to browse and edit its files.\n\n"
            "Ctrl+P  quick open     Ctrl+Shift+F  search     Ctrl+Shift+P  commands"
        )
        label.setObjectName("WelcomeText")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    def _add_pane(self) -> EditorPane:
        pane = EditorPane(self._registry, self._intelligence)
        pane.title_provider = self._title_for
        pane.tab_activated.connect(lambda tab, source=pane: self._on_tab_activated(source, tab))
        pane.tab_dirty_changed.connect(self._on_dirty_changed)
        pane.close_requested.connect(lambda index, source=pane: self._close_index(source, index))
        pane.cursor_moved.connect(self._relay_cursor)
        pane.tabs_changed.connect(self._on_tabs_changed)
        self._panes.append(pane)
        self._splitter.addWidget(pane)
        self._active_pane = pane
        return pane

    # -- workspace ---------------------------------------------------------
    @property
    def workspace(self) -> Workspace | None:
        return self._workspace

    def set_workspace(self, workspace: Workspace | None) -> None:
        self._workspace = workspace
        self._intelligence = CodeIntelligence(workspace) if workspace is not None else None

    def relative_of(self, path: Path | str) -> str:
        if self._workspace is None:
            return ""
        try:
            relative = self._workspace.relative_to_root(Path(path))
        except InvalidPathError as exc:
            raise QuantsMindError(
                "The file is outside the workspace.",
                user_message=exc.user_message,
            ) from exc
        return relative.replace("\\", "/")

    def _resolve(self, relative: str) -> Path:
        if self._workspace is None:
            return Path(relative)
        return self._workspace.resolve(relative)

    # -- opening -----------------------------------------------------------
    def open_relative(
        self,
        relative: str,
        *,
        preview: bool = True,
        line: int | None = None,
        force_editable: bool = False,
    ) -> OpenTab | None:
        path = self._resolve(relative)
        return self.open_file(
            path, relative=relative, preview=preview, line=line, force_editable=force_editable
        )

    def open_file(
        self,
        path: Path | str,
        *,
        relative: str | None = None,
        preview: bool = True,
        line: int | None = None,
        force_editable: bool = False,
    ) -> OpenTab | None:
        path = Path(path)
        if relative is None:
            relative = self.relative_of(path)
        existing = self._tab_for(path)
        if existing is not None:
            pane = self._pane_for(existing)
            if pane is not None:
                self._activate(pane, existing)
                if line is not None:
                    existing.editor.goto_line(line)
            self._touch(existing)
            return existing

        if preview:
            self._drop_preview(except_key=DocumentRegistry.key(path))
        try:
            document = self._registry.open(path, relative=relative, force_editable=force_editable)
        except QuantsMindError as exc:
            self.notice.emit(exc.user_message)
            return None
        pane = self._active_pane or self._add_pane()
        tab = pane.open(document)
        tab.preview = preview
        self._touch(tab)
        if preview:
            pane.preview_key = tab.key
        if line is not None:
            tab.editor.goto_line(line)
        self._stack.setCurrentWidget(self._splitter)
        self._active_pane = pane
        self._relay_active()
        self._enforce_limit()
        self.tabs_changed.emit()
        return tab

    def open_new_file(self, path: Path | str, *, relative: str | None = None) -> OpenTab | None:
        path = Path(path)
        if relative is None:
            relative = self.relative_of(path)
        if path.exists():
            return self.open_file(path, relative=relative, preview=False)
        document = self._registry.create(path, relative=relative)
        pane = self._active_pane or self._add_pane()
        tab = pane.open(document)
        tab.preview = False
        self._touch(tab)
        self._stack.setCurrentWidget(self._splitter)
        self._relay_active()
        self._enforce_limit()
        self.tabs_changed.emit()
        return tab

    def _tab_for(self, path: Path | str) -> OpenTab | None:
        key = DocumentRegistry.key(path)
        for pane in self._panes:
            tab = pane._tabs.get(key)
            if tab is not None:
                return tab
        return None

    def _pane_for(self, tab: OpenTab) -> EditorPane | None:
        for pane in self._panes:
            if pane.tab_for(tab.document.path) is tab:
                return pane
        return None

    def _drop_preview(self, *, except_key: str) -> None:
        for pane in self._panes:
            key = pane.preview_key
            if key is None or key == except_key:
                continue
            tab = pane._tabs.get(key)
            if tab is not None and not tab.document.dirty:
                self._forget_document(tab)
                pane.remove(tab)

    def _activate(self, pane: EditorPane, tab: OpenTab) -> None:
        self._active_pane = pane
        pane.setCurrentWidget(tab.page)
        pane.setFocus()
        self._relay_active()

    # -- buffer ceiling ----------------------------------------------------
    def _touch(self, tab: OpenTab) -> None:
        self._use_counter += 1
        tab.last_used = self._use_counter

    def _open_tabs(self) -> list[tuple[EditorPane, OpenTab]]:
        return [(pane, tab) for pane in self._panes for tab in pane.tabs()]

    def _enforce_limit(self, limit: int | None = None) -> int:
        """Close least-recently-used clean background buffers over ``limit``.

        Dirty buffers are user work and are never closed. The active tab of each
        pane is protected too, so degradation cannot take the editor out from
        under a keystroke.
        """

        ceiling = MAX_OPEN_DOCUMENTS if limit is None else max(1, limit)
        open_tabs = self._open_tabs()
        excess = len(open_tabs) - ceiling
        if excess <= 0:
            return 0
        protected = {
            id(active) for pane in self._panes if (active := pane.active_tab()) is not None
        }
        candidates = [
            (pane, tab)
            for pane, tab in open_tabs
            if not tab.document.dirty and id(tab) not in protected
        ]
        candidates.sort(key=lambda item: item[1].last_used)
        removed = 0
        for pane, tab in candidates[:excess]:
            self._forget_document(tab)
            pane.remove(tab)
            removed += 1
        if removed:
            self._update_welcome()
            self._relay_active()
        return removed

    def trim(self, limit: int) -> int:
        """Ordered-degradation hook: reduce open clean buffers to ``limit``."""

        return self._enforce_limit(limit)

    # -- splitting ---------------------------------------------------------
    def split(self) -> EditorPane | None:
        if len(self._panes) >= MAX_PANES:
            self.notice.emit(f"At most {MAX_PANES} editor panes are supported.")
            return None
        return self._add_pane()

    def close_split(self) -> bool:
        if len(self._panes) <= 1:
            return False
        pane = self._active_pane
        if pane is None:
            return False
        return self._close_pane(pane)

    def _close_pane(self, pane: EditorPane) -> bool:
        for tab in list(pane.tabs()):
            if not self._confirm_and_remove(pane, tab):
                return False
        self._panes.remove(pane)
        pane.deleteLater()
        self._active_pane = self._panes[-1] if self._panes else None
        self._update_welcome()
        return True

    # -- saving ------------------------------------------------------------
    def current_tab(self) -> OpenTab | None:
        if self._active_pane is None:
            return None
        return self._active_pane.active_tab()

    def current_editor(self) -> CodeEditor | None:
        tab = self.current_tab()
        return tab.editor if tab is not None else None

    def current_relative(self) -> str | None:
        tab = self.current_tab()
        return tab.document.relative if tab is not None else None

    def current_path(self) -> Path | None:
        tab = self.current_tab()
        return tab.document.path if tab is not None else None

    def selected_text(self) -> str:
        editor = self.current_editor()
        return editor.selected_text() if editor is not None else ""

    def save_current(self, target: Path | str | None = None, *, overwrite: bool = False) -> bool:
        tab = self.current_tab()
        pane = self._active_pane
        if tab is None or pane is None:
            return False
        path = Path(target) if target is not None else None
        if path is not None:
            if (
                path.exists()
                and path != tab.document.path
                and not overwrite
                and not self._confirm_overwrite(path)
            ):
                return False
            try:
                relative = self.relative_of(path)
            except QuantsMindError as exc:
                self.notice.emit(exc.user_message)
                return False
        else:
            relative = None
        try:
            written = pane.save(tab, path, overwrite=overwrite)
        except (QuantsMindError, FileExistsError, OSError) as exc:
            message = getattr(exc, "user_message", None) or str(exc)
            self.notice.emit(message)
            return False
        if relative is not None:
            tab.document.relative = relative
        self.document_saved.emit(str(written))
        self._relay_active()
        return True

    def save_all(self) -> int:
        saved = 0
        for pane in self._panes:
            for tab in pane.tabs():
                if not tab.document.dirty:
                    continue
                try:
                    written = pane.save(tab)
                except (QuantsMindError, OSError) as exc:
                    self.notice.emit(getattr(exc, "user_message", None) or str(exc))
                    continue
                saved += 1
                self.document_saved.emit(str(written))
        self._relay_active()
        return saved

    def save_as_current(self) -> bool:
        tab = self.current_tab()
        if tab is None:
            return False
        start = str(
            self._workspace.root if self._workspace is not None else tab.document.path.parent
        )
        chosen, _filter = QFileDialog.getSaveFileName(self, "Save As", start, "All files (*)")
        if not chosen:
            return False
        target = Path(chosen)
        if target.exists() and not self._confirm_overwrite(target):
            return False
        return self.save_current(target, overwrite=True)

    # -- closing -----------------------------------------------------------
    def close_current(self) -> bool:
        if self._active_pane is None:
            return True
        tab = self._active_pane.active_tab()
        if tab is None:
            return True
        return self._confirm_and_remove(self._active_pane, tab)

    def _close_index(self, pane: EditorPane, index: int) -> bool:
        tab = pane.tab_at(index)
        if tab is None:
            return True
        return self._confirm_and_remove(pane, tab)

    def _confirm_and_remove(self, pane: EditorPane, tab: OpenTab) -> bool:
        if tab.document.dirty:
            decision = self._confirm_close(tab.document)
            if decision == "cancel":
                return False
            if decision == "save":
                try:
                    pane.save(tab)
                except (QuantsMindError, OSError) as exc:
                    self.notice.emit(getattr(exc, "user_message", None) or str(exc))
                    return False
                self.document_saved.emit(str(tab.document.path))
        self._forget_document(tab)
        pane.remove(tab)
        self._update_welcome()
        self._relay_active()
        return True

    def request_close(self) -> bool:
        """Ask about every dirty buffer. ``False`` means the user cancelled."""

        for pane in self._panes:
            for tab in list(pane.tabs()):
                if not self._confirm_and_remove(pane, tab):
                    return False
        return True

    def close_all(self, *, discard: bool = False) -> None:
        for pane in self._panes:
            for tab in list(pane.tabs()):
                if tab.document.dirty and not discard:
                    continue
                self._forget_document(tab)
                pane.remove(tab)
        self._update_welcome()

    def _forget_document(self, tab: OpenTab) -> None:
        self._registry.remove(tab.document.path)
        self._pending_external.discard(tab.key)

    def _on_tabs_changed(self) -> None:
        self._update_welcome()
        self._relay_active()
        self.tabs_changed.emit()

    def _update_welcome(self) -> None:
        empty = all(not pane.tabs() for pane in self._panes)
        self._stack.setCurrentWidget(self._welcome if empty else self._splitter)
        if empty:
            self.find_bar.hide()

    # -- external changes --------------------------------------------------
    def check_external_changes(self) -> None:
        for pane in self._panes:
            for tab in pane.tabs():
                if not tab.document.has_external_change():
                    self._pending_external.discard(tab.key)
                    continue
                if tab.document.dirty:
                    if tab.key in self._pending_external:
                        continue
                    self._pending_external.add(tab.key)
                    self.external_change_detected.emit(tab.key)
                    continue
                try:
                    pane.reload(tab)
                except (QuantsMindError, OSError) as exc:
                    self.notice.emit(getattr(exc, "user_message", None) or str(exc))
                    continue
                self.notice.emit(f"{tab.name} was reloaded from disk.")
                self._relay_active()

    def resolve_external_change(self, key: str, decision: str) -> None:
        tab = self._tab_for_key(key)
        if tab is None:
            return
        pane = self._pane_for(tab)
        if pane is None:
            return
        self._pending_external.discard(key)
        if decision == "reload":
            try:
                pane.reload(tab)
                self.notice.emit(f"{tab.name} was reloaded from disk.")
            except (QuantsMindError, OSError) as exc:
                self.notice.emit(getattr(exc, "user_message", None) or str(exc))
        elif decision == "keep":
            pane.keep_my_version(tab)
            self.notice.emit(f"Kept your version of {tab.name}.")
        elif decision == "compare":
            self._show_compare(tab)
            self._pending_external.add(key)
        self._relay_active()

    def _tab_for_key(self, key: str) -> OpenTab | None:
        for pane in self._panes:
            tab = pane._tabs.get(key)
            if tab is not None:
                return tab
        return None

    def show_external_change_dialog(self, key: str) -> None:
        tab = self._tab_for_key(key)
        if tab is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("File changed on disk")
        box.setText(f"{tab.document.name} changed on disk and has unsaved changes in the editor.")
        compare = box.addButton("Compare", QMessageBox.ButtonRole.ActionRole)
        reload_button = box.addButton("Reload", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Keep My Version", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is compare:
            self._show_compare(tab)
            self._pending_external.add(key)
        elif clicked is reload_button:
            self.resolve_external_change(key, "reload")
        elif clicked is keep:
            self.resolve_external_change(key, "keep")

    def _show_compare(self, tab: OpenTab) -> None:
        try:
            disk = Path(tab.document.path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.notice.emit(f"The file could not be read for comparison: {exc}")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Compare: {tab.document.name}")
        dialog.resize(900, 600)
        layout = QVBoxLayout(dialog)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        on_disk = QPlainTextEdit()
        on_disk.setReadOnly(True)
        on_disk.setPlainText(disk)
        in_editor = QPlainTextEdit()
        in_editor.setReadOnly(True)
        in_editor.setPlainText(tab.editor.plain_text())
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("On disk"))
        left_layout.addWidget(on_disk)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("In the editor"))
        right_layout.addWidget(in_editor)
        splitter.addWidget(left)
        splitter.addWidget(right)
        layout.addWidget(splitter)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    # -- external filesystem changes --------------------------------------
    def has_dirty(self) -> bool:
        return any(tab.document.dirty for pane in self._panes for tab in pane.tabs())

    def close_relative(self, relative: str) -> int:
        """Drop every buffer for ``relative`` or a path beneath it.

        Used after the explorer deleted a file or folder. The deletion already
        asked the user; keeping a buffer for a path that no longer exists would
        offer a save that cannot succeed.
        """

        normalized = relative.replace("\\", "/").strip("/")
        removed = 0
        for pane in self._panes:
            for tab in list(pane.tabs()):
                current = tab.document.relative.replace("\\", "/").strip("/")
                if current != normalized and not current.startswith(f"{normalized}/"):
                    continue
                self._forget_document(tab)
                pane.remove(tab)
                removed += 1
        if removed:
            self._update_welcome()
            self._relay_active()
        return removed

    def apply_rename(self, old_relative: str, new_relative: str) -> int:
        """Re-point open buffers after the explorer renamed a file or folder."""

        if self._workspace is None:
            return 0
        old = old_relative.replace("\\", "/").strip("/")
        new = new_relative.replace("\\", "/").strip("/")
        moved = 0
        for pane in self._panes:
            for tab in list(pane.tabs()):
                current = tab.document.relative.replace("\\", "/").strip("/")
                if current != old and not current.startswith(f"{old}/"):
                    continue
                suffix = current[len(old) :].lstrip("/")
                target_relative = new if not suffix else f"{new}/{suffix}"
                try:
                    target_path = self._resolve(target_relative)
                except (QuantsMindError, InvalidPathError):
                    continue
                old_key = tab.key
                old_path = tab.document.path
                tab.document.path = target_path
                tab.document.relative = target_relative
                tab.document.exists = target_path.exists()
                self._registry.remove(old_path)
                self._registry.add(tab.document)
                tab.key = DocumentRegistry.key(target_path)
                if tab.key != old_key:
                    pane._tabs.pop(old_key, None)
                    pane._tabs[tab.key] = tab
                moved += 1
            pane.refresh_titles()
        return moved

    # -- find --------------------------------------------------------------
    def show_find(self, *, replace: bool = False) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        self.find_bar.open_for(tab.editor.selected_text(), replace_mode=replace)

    def _on_find(self, options: FindOptions, forward: bool) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        found = editor.find(
            options.text,
            forward=forward,
            case_sensitive=options.case_sensitive,
            whole_word=options.whole_word,
            use_regex=options.use_regex,
        )
        self.find_bar.set_status("match" if found else "No results")

    def _on_replace(self, options: FindOptions) -> None:
        editor = self.current_editor()
        if editor is None:
            return
        replaced = editor.replace_current(
            options.text,
            options.replacement,
            case_sensitive=options.case_sensitive,
            whole_word=options.whole_word,
            use_regex=options.use_regex,
        )
        self.find_bar.set_status("replaced" if replaced else "No results")

    def _on_replace_all(self, options: FindOptions) -> None:
        editor = self.current_editor()
        if editor is None or not options.text:
            return
        count = self._count_matches(editor, options)
        if count == 0:
            self.find_bar.set_status("No results")
            return
        if count > REPLACE_CONFIRM_THRESHOLD and not self._confirm_replace_all(count):
            self.find_bar.set_status("Replace all cancelled")
            return
        replaced = editor.replace_all(
            options.text,
            options.replacement,
            case_sensitive=options.case_sensitive,
            whole_word=options.whole_word,
            use_regex=options.use_regex,
        )
        self.find_bar.set_status(f"{replaced} replaced")

    @staticmethod
    def _count_matches(editor: CodeEditor, options: FindOptions) -> int:
        from PySide6.QtGui import QTextCursor

        flags = CodeEditor._find_flags(True, options.case_sensitive, options.whole_word)
        expression = (
            CodeEditor._expression(options.text, options.case_sensitive)
            if options.use_regex
            else None
        )
        cursor = QTextCursor(editor.document())
        count = 0
        while count <= REPLACE_CONFIRM_THRESHOLD:
            found = (
                editor.document().find(expression, cursor, flags)
                if expression is not None
                else editor.document().find(options.text, cursor, flags)
            )
            if found.isNull():
                break
            count += 1
            cursor = found
        return count

    # -- titles and state --------------------------------------------------
    def _title_for(self, document: TextDocument) -> str:
        names = Counter(tab.document.name for pane in self._panes for tab in pane.tabs())
        if names[document.name] <= 1:
            return document.name
        parent = Path(document.relative).parent
        if str(parent) not in ("", "."):
            return f"{parent.as_posix()}/{document.name}"
        return f"{document.path.parent.name}/{document.name}"

    def _relay_active(self) -> None:
        self._relay_dirty()
        tab = self.current_tab()
        if tab is None:
            self.active_document_changed.emit(None)
            self.cursor_changed.emit(1, 1)
            return
        editor = tab.editor
        line, column = editor.current_line_column()
        language = language_label(language_for_path(tab.document.path))
        self.active_document_changed.emit(tab.document.info(language=language))
        self.cursor_changed.emit(line, column)

    def _relay_dirty(self) -> None:
        self.dirty_changed.emit(
            any(tab.document.dirty for pane in self._panes for tab in pane.tabs())
        )

    def _relay_cursor(self, line: int, column: int) -> None:
        if self.sender() is self._active_pane:
            self.cursor_changed.emit(line, column)

    def _on_dirty_changed(self, tab: OpenTab) -> None:
        self._touch(tab)
        self._relay_dirty()
        self.tabs_changed.emit()

    def _on_tab_activated(self, pane: EditorPane, tab: OpenTab | None) -> None:
        self._active_pane = pane
        if tab is not None:
            self._touch(tab)
        self._relay_active()

    # -- persistence -------------------------------------------------------
    def tab_states(self) -> list[EditorTabState]:
        result: list[EditorTabState] = []
        active_pane = self._active_pane
        for pane in self._panes:
            for tab in pane.state():
                tab.active = bool(pane is active_pane and tab.active)
                result.append(tab)
        return result

    def restore_tabs(self, tabs: Iterable[EditorTabState]) -> list[str]:
        """Reopen stored tabs and return the paths that are no longer available."""

        missing: list[str] = []
        active_rel: str | None = None
        for state in tabs:
            try:
                path = self._resolve(state.path)
            except (QuantsMindError, InvalidPathError):
                missing.append(state.path)
                continue
            if not path.is_file():
                missing.append(state.path)
                continue
            tab = self.open_file(
                path,
                relative=state.path,
                preview=not state.pinned,
                line=state.cursor_line,
            )
            if tab is None:
                missing.append(state.path)
                continue
            tab.editor.verticalScrollBar().setValue(state.scroll)
            if state.active:
                active_rel = state.path
        if active_rel is not None:
            for pane in self._panes:
                for tab in pane.tabs():
                    if tab.document.relative == active_rel:
                        self._activate(pane, tab)
        return missing

    # -- EditorStateService surface ---------------------------------------
    def is_dirty(self, relative: str) -> bool:
        return any(
            tab.document.relative == relative and tab.document.dirty
            for pane in self._panes
            for tab in pane.tabs()
        )

    def dirty_paths(self) -> frozenset[str]:
        return frozenset(
            tab.document.relative
            for pane in self._panes
            for tab in pane.tabs()
            if tab.document.dirty
        )

    def open_documents(self) -> tuple[DocumentInfo, ...]:
        return tuple(
            tab.document.info(language=language_label(language_for_path(tab.document.path)))
            for pane in self._panes
            for tab in pane.tabs()
        )

    # -- dialogs -----------------------------------------------------------
    def _ask_close(self, document: TextDocument) -> CloseDecision:
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"{document.name} has unsaved changes.")
        box.setInformativeText("Save before closing?")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Don't Save", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save:
            return "save"
        if clicked is discard:
            return "discard"
        return "cancel"

    def _ask_overwrite(self, path: Path) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Replace file?")
        box.setText(f"{path.name} already exists. Replace it?")
        replace = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is replace

    def _ask_replace_all(self, count: int) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Replace all?")
        box.setText(f"Replace {count} occurrences?")
        yes = box.addButton("Replace All", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is yes
