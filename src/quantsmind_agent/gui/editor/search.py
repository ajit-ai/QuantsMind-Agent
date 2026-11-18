"""Find and replace bar for the editor surface."""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

__all__ = ["FindBar", "FindOptions"]


@dataclass(frozen=True, slots=True)
class FindOptions:
    text: str = ""
    replacement: str = ""
    case_sensitive: bool = False
    whole_word: bool = False
    use_regex: bool = False


class FindBar(QWidget):
    """A compact find/replace strip above the editor."""

    find_requested = Signal(object, bool)  # FindOptions, forward
    replace_requested = Signal(object)
    replace_all_requested = Signal(object)
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FindBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Find")
        self.search_input.setMinimumWidth(180)
        layout.addWidget(self.search_input, 1)

        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("Replace with")
        self.replace_input.setMinimumWidth(160)
        self.replace_input.setVisible(False)
        layout.addWidget(self.replace_input, 1)

        self.case_box = QCheckBox("Aa")
        self.case_box.setToolTip("Match case")
        layout.addWidget(self.case_box)
        self.word_box = QCheckBox("Word")
        self.word_box.setToolTip("Match whole word")
        layout.addWidget(self.word_box)
        self.regex_box = QCheckBox(".*")
        self.regex_box.setToolTip("Use regular expression")
        layout.addWidget(self.regex_box)

        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.replace_button = QPushButton("Replace")
        self.replace_all_button = QPushButton("Replace All")
        self.close_button = QPushButton("Close")
        for button in (
            self.previous_button,
            self.next_button,
            self.replace_button,
            self.replace_all_button,
            self.close_button,
        ):
            layout.addWidget(button)
        self.replace_button.setVisible(False)
        self.replace_all_button.setVisible(False)

        self.status_label = QLabel("")
        self.status_label.setObjectName("StatusText")
        layout.addWidget(self.status_label)

        self.next_button.clicked.connect(lambda: self._emit_find(True))
        self.previous_button.clicked.connect(lambda: self._emit_find(False))
        self.replace_button.clicked.connect(self._emit_replace)
        self.replace_all_button.clicked.connect(self._emit_replace_all)
        self.close_button.clicked.connect(self.close_bar)
        self.search_input.returnPressed.connect(lambda: self._emit_find(True))
        self.replace_input.returnPressed.connect(self._emit_replace)

        QShortcut(QKeySequence("Escape"), self, self.close_bar)

    # -- lifecycle ---------------------------------------------------------
    def open_for(self, selected: str = "", *, replace_mode: bool = False) -> None:
        self.replace_input.setVisible(replace_mode)
        self.replace_button.setVisible(replace_mode)
        self.replace_all_button.setVisible(replace_mode)
        if selected:
            self.search_input.setText(selected)
        self.status_label.setText("")
        self.show()
        self.search_input.setFocus()
        self.search_input.selectAll()

    def close_bar(self) -> None:
        self.hide()
        self.closed.emit()

    def options(self) -> FindOptions:
        return FindOptions(
            text=self.search_input.text(),
            replacement=self.replace_input.text(),
            case_sensitive=self.case_box.isChecked(),
            whole_word=self.word_box.isChecked(),
            use_regex=self.regex_box.isChecked(),
        )

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)

    # -- signals -----------------------------------------------------------
    def _emit_find(self, forward: bool) -> None:
        self.find_requested.emit(self.options(), forward)

    def _emit_replace(self) -> None:
        self.replace_requested.emit(self.options())

    def _emit_replace_all(self) -> None:
        self.replace_all_requested.emit(self.options())

    def with_replacement(self, replacement: str) -> FindOptions:
        return replace(self.options(), replacement=replacement)
