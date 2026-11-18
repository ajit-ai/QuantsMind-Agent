"""Command registry and palette.

A command is a named, user-visible action. The registry owns the catalogue and
the matching rules; the palette is a thin view over it. Keybindings and menu
entries call the same command object, so an action that is disabled in one
place is disabled everywhere.

Nothing here decides whether an action is *authorised*. A command's callback
is the same code path a button would use, which means the existing permission
and approval machinery still applies.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

__all__ = ["Command", "CommandPalette", "CommandRegistry"]


@dataclass(slots=True)
class Command:
    """One action the user can trigger by name."""

    id: str
    title: str
    callback: Callable[[], object]
    category: str = ""
    shortcut: str = ""
    enabled: Callable[[], bool] | None = None
    dangerous: bool = False
    keywords: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        if self.category:
            return f"{self.category}: {self.title}"
        return self.title

    @property
    def detail(self) -> str:
        return self.shortcut or self.category

    def is_enabled(self) -> bool:
        return self.enabled is None or bool(self.enabled())

    def haystack(self) -> str:
        parts = (self.title, self.id, self.category, *self.keywords)
        return " ".join(part for part in parts if part).casefold()


class CommandRegistry:
    """The catalogue, with deterministic fuzzy matching."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> Command:
        self._commands[command.id] = command
        return command

    def register_all(self, commands: Iterable[Command]) -> None:
        for command in commands:
            self.register(command)

    def get(self, command_id: str) -> Command | None:
        return self._commands.get(command_id)

    def all(self) -> list[Command]:
        return list(self._commands.values())

    def search(
        self, query: str, *, limit: int = 60, include_disabled: bool = False
    ) -> list[Command]:
        needle = query.strip().casefold()
        candidates = [
            command
            for command in self._commands.values()
            if include_disabled or command.is_enabled()
        ]
        if not needle:
            return sorted(candidates, key=lambda command: (command.category, command.title))[:limit]
        scored: list[tuple[int, Command]] = []
        for command in candidates:
            score = self._score(needle, command)
            if score is not None:
                scored.append((score, command))
        scored.sort(key=lambda item: (-item[0], item[1].label))
        return [command for _score, command in scored[:limit]]

    @staticmethod
    def _score(needle: str, command: Command) -> int | None:
        title = command.title.casefold()
        identifier = command.id.casefold()
        haystack = command.haystack()
        if title == needle or identifier == needle:
            return 1000
        if title.startswith(needle):
            return 800 - len(title)
        if identifier.startswith(needle):
            return 700 - len(identifier)
        if needle in title:
            return 600 - title.index(needle)
        if needle in haystack:
            return 400 - haystack.index(needle)
        # Subsequence match: "svall" finds "Save All".
        position = 0
        gaps = 0
        for character in needle:
            found = haystack.find(character, position)
            if found < 0:
                return None
            gaps += found - position
            position = found + 1
        return max(1, 200 - gaps)


class CommandPalette(QDialog):
    """A modal, keyboard-first command chooser."""

    def __init__(
        self,
        registry: CommandRegistry,
        parent: QWidget | None = None,
        *,
        confirm_dangerous: Callable[[Command], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._commands: list[Command] = []
        self._confirm_dangerous = confirm_dangerous or self._ask_dangerous
        self.setWindowTitle("Command palette")
        self.setModal(True)
        self.resize(640, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a command name\u2026")
        self.input.textChanged.connect(self.refresh)
        self.input.returnPressed.connect(self.activate_current)
        layout.addWidget(self.input)

        self.list = QListWidget()
        self.list.itemActivated.connect(lambda _item: self.activate_current())
        layout.addWidget(self.list, 1)

        self.status = QLabel("Enter to run \u00b7 Esc to close")
        self.status.setObjectName("StatusText")
        layout.addWidget(self.status)

        self.refresh()

    # -- contents ----------------------------------------------------------
    def refresh(self) -> None:
        self._commands = self._registry.search(self.input.text())
        self.list.clear()
        for command in self._commands:
            item = QListWidgetItem(command.label)
            detail = command.detail
            if detail:
                item.setToolTip(f"{command.label}  ({detail})")
            item.setData(Qt.ItemDataRole.UserRole, command.id)
            self.list.addItem(item)
        if self._commands:
            self.list.setCurrentRow(0)
        self.status.setText(
            f"{len(self._commands)} command(s) \u00b7 Enter to run \u00b7 Esc to close"
            if self._commands
            else "No matching command"
        )

    def commands(self) -> list[Command]:
        """The currently filtered commands, for tests and for callers."""

        return list(self._commands)

    # -- activation --------------------------------------------------------
    def activate_current(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self.activate(row)

    def activate(self, index: int) -> None:
        if not 0 <= index < len(self._commands):
            return
        command = self._commands[index]
        if command.dangerous and not self._confirm_dangerous(command):
            return
        self.accept()
        command.callback()

    def _ask_dangerous(self, command: Command) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Run this command?")
        box.setText(f"{command.label} cannot be undone.")
        run = box.addButton("Run", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is run
