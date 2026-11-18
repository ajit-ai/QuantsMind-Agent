"""Workspace explorer tree.

Directories load lazily, so opening a large repository costs one listing, not a
full walk. Every path the tree shows is workspace-relative and every mutation
goes through :class:`~quantsmind_agent.services.workspace.operations.FileOperationService`,
which re-applies the same containment rule the agent's tools use.

Deletion prefers the platform trash. Permanent deletion is offered only after
the trash refuses, and only after a separate, explicit confirmation.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QStyle,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.services.workspace.explorer import ExplorerEntry, ExplorerService
from quantsmind_agent.services.workspace.operations import (
    FileOperationService,
    TrashUnavailable,
)

__all__ = ["WorkspaceExplorer"]

logger = get_logger(__name__)

_PATH_ROLE = int(Qt.ItemDataRole.UserRole)
_DIR_ROLE = _PATH_ROLE + 1
_LOADED_ROLE = _PATH_ROLE + 2


class WorkspaceExplorer(QWidget):
    """A lazy project tree with user-initiated file operations."""

    file_activated = Signal(str)
    file_created = Signal(str)
    path_deleted = Signal(str)
    path_renamed = Signal(str, str)
    notice = Signal(str)
    expanded_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace: Workspace | None = None
        self._service: ExplorerService | None = None
        self._operations: FileOperationService | None = None
        self._items: dict[str, QTreeWidgetItem] = {}
        self._root_item: QTreeWidgetItem | None = None
        self._show_hidden = False
        self._refreshing = False
        self._build()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.header = QLabel("No folder open")
        self.header.setObjectName("SectionLabel")
        self.header.setWordWrap(True)
        layout.addWidget(self.header)

        buttons = QHBoxLayout()
        buttons.setSpacing(2)
        self.new_file_button = self._tool_button("New File", "Create a file in the selected folder")
        self.new_file_button.clicked.connect(lambda: self.create_file())
        self.new_folder_button = self._tool_button(
            "New Folder", "Create a folder in the selected folder"
        )
        self.new_folder_button.clicked.connect(lambda: self.create_folder())
        self.refresh_button = self._tool_button("Refresh", "Re-read the visible folders")
        self.refresh_button.clicked.connect(self.refresh)
        self.hidden_button = self._tool_button(
            "Hidden", "Show hidden files and folders", checkable=True
        )
        self.hidden_button.toggled.connect(self._on_hidden_toggled)
        for button in (
            self.new_file_button,
            self.new_folder_button,
            self.refresh_button,
            self.hidden_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemCollapsed.connect(lambda _item: self._emit_expanded())
        self.tree.itemActivated.connect(self._on_item_activated)
        # Activation is handled once, here, for both Enter and double click.
        self.tree.setExpandsOnDoubleClick(False)
        layout.addWidget(self.tree, 1)
        self._set_enabled(False)

    @staticmethod
    def _tool_button(text: str, tooltip: str, *, checkable: bool = False) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCheckable(checkable)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setAutoRaise(True)
        return button

    # -- workspace ---------------------------------------------------------
    @property
    def workspace(self) -> Workspace | None:
        return self._workspace

    def set_workspace(self, workspace: Workspace | None) -> None:
        self._workspace = workspace
        self._service = ExplorerService(workspace) if workspace is not None else None
        self._operations = FileOperationService(workspace) if workspace is not None else None
        self._set_enabled(workspace is not None)
        if workspace is None:
            self.tree.clear()
            self._items.clear()
            self._root_item = None
            self.header.setText("No folder open")
            return
        self.header.setText(workspace.root.name or str(workspace.root))
        self.header.setToolTip(str(workspace.root))
        self.refresh()

    @property
    def service(self) -> ExplorerService | None:
        return self._service

    @property
    def operations(self) -> FileOperationService | None:
        return self._operations

    def _set_enabled(self, enabled: bool) -> None:
        for button in (
            self.new_file_button,
            self.new_folder_button,
            self.refresh_button,
            self.hidden_button,
        ):
            button.setEnabled(enabled)

    # -- rendering ---------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the visible branches, preserving what was expanded."""

        if self._service is None or self._workspace is None:
            return
        expanded = self.expanded_directories()
        selected = self.selected_relative()
        self._refreshing = True
        try:
            self.tree.clear()
            self._items.clear()
            root = QTreeWidgetItem(
                self.tree, [self._workspace.root.name or str(self._workspace.root)]
            )
            root.setData(0, _PATH_ROLE, ".")
            root.setData(0, _DIR_ROLE, True)
            root.setToolTip(0, str(self._workspace.root))
            root.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
            self._root_item = root
            self._items["."] = root
            self._load_children(root)
            root.setExpanded(True)
            for path in sorted(expanded, key=lambda value: value.count("/")):
                if path == ".":
                    continue
                item = self._items.get(path)
                if item is None or not self._is_dir(item):
                    continue
                item.setExpanded(True)
        finally:
            self._refreshing = False
        if selected:
            self.select_path(selected)
        self._emit_expanded()

    def refresh_directories(self, directories: list[str]) -> None:
        """Reload after a watcher report. The tree is lazy, so this is cheap."""

        if not directories or self._service is None:
            return
        self.refresh()

    def _load_children(self, item: QTreeWidgetItem) -> None:
        if self._service is None or item.data(0, _LOADED_ROLE):
            return
        path = self._path(item)
        if not path:
            return
        item.takeChildren()
        try:
            entries = self._service.list_directory(path, include_hidden=self._show_hidden)
        except (QuantsMindError, OSError) as exc:
            self.notice.emit(getattr(exc, "user_message", None) or str(exc))
            item.setData(0, _LOADED_ROLE, True)
            return
        for entry in entries:
            self._add_entry(item, entry)
        item.setData(0, _LOADED_ROLE, True)

    def _add_entry(self, parent: QTreeWidgetItem, entry: ExplorerEntry) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [entry.name])
        item.setData(0, _PATH_ROLE, entry.path)
        item.setData(0, _DIR_ROLE, entry.is_dir)
        item.setToolTip(0, entry.path)
        style = self.style()
        icon = (
            QStyle.StandardPixmap.SP_DirIcon if entry.is_dir else QStyle.StandardPixmap.SP_FileIcon
        )
        item.setIcon(0, style.standardIcon(icon))
        if entry.is_dir:
            item.addChild(QTreeWidgetItem([""]))
        self._items[entry.path] = item
        return item

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if not self._is_dir(item):
            return
        self._load_children(item)
        self._emit_expanded()

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        if self._is_dir(item):
            item.setExpanded(not item.isExpanded())
            return
        path = self._path(item)
        if path:
            self.file_activated.emit(path)

    def _emit_expanded(self) -> None:
        if not self._refreshing:
            self.expanded_changed.emit()

    # -- state -------------------------------------------------------------
    def expanded_directories(self) -> list[str]:
        directories = {"."}
        for path, item in self._items.items():
            if self._is_dir(item) and item.isExpanded():
                directories.add(path)
        return sorted(directories)

    def selected_relative(self) -> str:
        item = self.tree.currentItem()
        return self._path(item) if item is not None else ""

    def current_item(self) -> QTreeWidgetItem | None:
        return self.tree.currentItem()

    def select_path(self, relative: str) -> bool:
        """Expand the parents of ``relative`` and select it."""

        normalized = relative.replace("\\", "/").strip("/")
        if not normalized or normalized == ".":
            if self._root_item is not None:
                self.tree.setCurrentItem(self._root_item)
                return True
            return False
        current = self._root_item
        if current is None:
            return False
        for part in PurePosixPath(normalized).parts:
            current.setExpanded(True)
            self._load_children(current)
            child = None
            for index in range(current.childCount()):
                candidate = current.child(index)
                if candidate is not None and candidate.text(0) == part:
                    child = candidate
                    break
            if child is None:
                return False
            current = child
        self.tree.setCurrentItem(current)
        self.tree.scrollToItem(current)
        return True

    def describe(self) -> dict[str, object]:
        return {
            "root": str(self._workspace.root) if self._workspace else "",
            "expanded": self.expanded_directories(),
            "top_level_items": self._root_item.childCount() if self._root_item else 0,
        }

    # -- context menu ------------------------------------------------------
    def _on_context_menu(self, position: QPoint) -> None:
        if self._service is None:
            return
        item = self.tree.itemAt(position)
        if item is None:
            item = self._root_item
        if item is None:
            return
        self.tree.setCurrentItem(item)
        is_root = item is self._root_item
        menu = QMenu(self)
        new_file = menu.addAction("New File\u2026")
        new_folder = menu.addAction("New Folder\u2026")
        menu.addSeparator()
        rename = menu.addAction("Rename\u2026")
        delete = menu.addAction("Move to Recycle Bin")
        menu.addSeparator()
        refresh = menu.addAction("Refresh")
        hidden = menu.addAction("Show hidden files")
        hidden.setCheckable(True)
        hidden.setChecked(self._show_hidden)
        rename.setEnabled(not is_root)
        delete.setEnabled(not is_root)
        chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
        if chosen is new_file:
            self.create_file(self._parent_path(item))
        elif chosen is new_folder:
            self.create_folder(self._parent_path(item))
        elif chosen is rename:
            self.rename(self._path(item))
        elif chosen is delete:
            self.delete(self._path(item))
        elif chosen is refresh:
            self.refresh()
        elif chosen is hidden:
            self.hidden_button.setChecked(not self._show_hidden)

    def _on_hidden_toggled(self, checked: bool) -> None:
        self._show_hidden = bool(checked)
        self.refresh()

    # -- operations --------------------------------------------------------
    def create_file(self, parent: str | None = None, name: str | None = None) -> str:
        return self._create(parent, name, directory=False)

    def create_folder(self, parent: str | None = None, name: str | None = None) -> str:
        return self._create(parent, name, directory=True)

    def _create(self, parent: str | None, name: str | None, *, directory: bool) -> str:
        operations = self._operations
        if operations is None:
            return ""
        parent = parent or self._selected_parent()
        title = "New folder" if directory else "New file"
        if name is None:
            name, accepted = QInputDialog.getText(
                self,
                title,
                "Name:",
                text="" if directory else "untitled.txt",
            )
            if not accepted:
                return ""
        try:
            cleaned = operations.validate_name(name)
            target = self._join(parent, cleaned)
            if directory:
                created = operations.create_directory(target)
            else:
                created = operations.create_file(target)
        except (QuantsMindError, OSError) as exc:
            self.notice.emit(getattr(exc, "user_message", None) or str(exc))
            return ""
        self._reload_directory(parent)
        self.select_path(created)
        if not directory:
            self.file_created.emit(created)
        return created

    def rename(self, relative: str, new_name: str | None = None) -> str:
        operations = self._operations
        if operations is None or not relative or relative == ".":
            return ""
        current = PurePosixPath(relative).name
        if new_name is None:
            new_name, accepted = QInputDialog.getText(self, "Rename", "New name:", text=current)
            if not accepted:
                return ""
        if new_name == current:
            return relative
        parent = str(PurePosixPath(relative).parent)
        if parent in ("", "."):
            parent = "."
        try:
            cleaned = operations.validate_name(new_name)
            target = self._join(parent, cleaned)
            renamed = operations.rename(relative, target)
        except (QuantsMindError, OSError) as exc:
            self.notice.emit(getattr(exc, "user_message", None) or str(exc))
            return ""
        if renamed:
            self._reload_directory(parent)
            self.select_path(renamed)
            self.path_renamed.emit(relative, renamed)
            self.notice.emit(f"Renamed to {renamed}.")
        return renamed

    def delete(self, relative: str, *, confirm: bool = True) -> bool:
        operations = self._operations
        if operations is None or not relative or relative == ".":
            return False
        if confirm and not self._ask_delete(relative):
            return False
        try:
            result = operations.delete(relative)
        except TrashUnavailable:
            if not self._ask_permanent(relative):
                self.notice.emit("Not deleted.")
                return False
            try:
                result = operations.delete(relative, permanent=True)
            except (QuantsMindError, OSError) as error:
                self.notice.emit(getattr(error, "user_message", None) or str(error))
                return False
        except (QuantsMindError, OSError) as exc:
            self.notice.emit(getattr(exc, "user_message", None) or str(exc))
            return False
        parent = str(PurePosixPath(relative).parent)
        if parent in ("", "."):
            parent = "."
        self._reload_directory(parent)
        self.path_deleted.emit(relative)
        self.notice.emit(
            f"Moved {relative} to the Recycle Bin."
            if result.recovered
            else f"Deleted {relative} permanently."
        )
        return True

    # -- helpers -----------------------------------------------------------
    def _reload_directory(self, relative: str) -> None:
        item = self._items.get(relative or ".")
        if item is None:
            self.refresh()
            return
        item.setData(0, _LOADED_ROLE, False)
        item.takeChildren()
        if item.isExpanded():
            self._load_children(item)
        else:
            item.setExpanded(True)

    def _selected_parent(self) -> str:
        item = self.tree.currentItem()
        if item is None or item is self._root_item:
            return "."
        return self._parent_path(item)

    def _parent_path(self, item: QTreeWidgetItem) -> str:
        if self._is_dir(item):
            return self._path(item) or "."
        parent = item.parent()
        return self._path(parent) if parent is not None else "."

    @staticmethod
    def _join(parent: str, name: str) -> str:
        parent = (parent or ".").replace("\\", "/").strip("/")
        return name if parent in ("", ".") else f"{parent}/{name}"

    @staticmethod
    def _path(item: QTreeWidgetItem | None) -> str:
        if item is None:
            return ""
        value = item.data(0, _PATH_ROLE)
        return str(value) if value else ""

    @staticmethod
    def _is_dir(item: QTreeWidgetItem | None) -> bool:
        return bool(item is not None and item.data(0, _DIR_ROLE))

    # -- dialogs -----------------------------------------------------------
    def _ask_delete(self, relative: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Move to Recycle Bin?")
        box.setText(f"Move {relative} to the Recycle Bin?")
        box.setInformativeText(
            "Any unsaved editor changes to this item are discarded when it is deleted."
        )
        move = box.addButton("Move to Recycle Bin", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is move

    def _ask_permanent(self, relative: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Delete permanently?")
        box.setText(f"The Recycle Bin is unavailable. Delete {relative} permanently?")
        box.setInformativeText("This cannot be undone.")
        delete = box.addButton("Delete Permanently", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is delete
