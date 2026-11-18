"""The Qt text editor widget: line numbers, current line, indentation.

The widget owns only presentation and editing behaviour. File classification
and persistence live in :mod:`quantsmind_agent.gui.editor.document`.
"""

from __future__ import annotations

import re

from PySide6.QtCore import QRect, QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFontDatabase,
    QFontMetricsF,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from quantsmind_agent.code_index import parse_symbols
from quantsmind_agent.gui.editor.syntax import (
    Language,
    PySideSyntaxHighlighter,
)

__all__ = ["CodeEditor", "LineNumberArea"]

_OPENERS = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}
_CLOSERS = {value: key for key, value in _OPENERS.items()}
_INDENT = "    "


class LineNumberArea(QWidget):
    def __init__(self, editor: CodeEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event: QPaintEvent) -> None:
        self._editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    """A plain-text editor with the workbench affordances the phase requires."""

    #: Emitted when the cursor moves to a symbol; carries (symbol_name, kind).
    symbol_hovered = Signal(str, str)
    #: LSP inline-UX requests (v1.1): completion at cursor, definition at cursor.
    lsp_completion_requested = Signal()
    lsp_definition_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._line_numbers = LineNumberArea(self)
        self._highlighter: PySideSyntaxHighlighter | None = None
        self._language = Language.PLAIN
        self._large_file = False

        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(11)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(QFontMetricsF(font).horizontalAdvance(" ") * len(_INDENT))
        self.setCenterOnScroll(True)

        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.cursorPositionChanged.connect(self._refresh_extra_selections)
        self.cursorPositionChanged.connect(self._emit_symbol_hover)
        self._update_line_number_area_width(0)
        self._refresh_extra_selections()

    # -- content -----------------------------------------------------------
    @property
    def language(self) -> Language:
        return self._language

    def set_language(self, language: Language) -> None:
        self._language = language
        if self._highlighter is not None:
            self._highlighter.setDocument(None)
            self._highlighter = None
        if self._large_file or language is Language.PLAIN:
            return
        self._highlighter = PySideSyntaxHighlighter(self.document(), language)

    def set_large_file(self, large: bool) -> None:
        self._large_file = large
        if large and self._highlighter is not None:
            self._highlighter.setDocument(None)
            self._highlighter = None

    def set_highlighting(self, enabled: bool) -> None:
        if enabled:
            self.set_language(self._language)
        elif self._highlighter is not None:
            self._highlighter.setDocument(None)
            self._highlighter = None

    def load_text(self, text: str, *, readonly: bool = False) -> None:
        self.setReadOnly(readonly)
        blocked = self.blockSignals(True)
        try:
            self.setPlainText(text)
        finally:
            self.blockSignals(blocked)
        self.document().setModified(False)
        self.moveCursor(QTextCursor.MoveOperation.Start)
        self._refresh_extra_selections()

    def plain_text(self) -> str:
        return self.toPlainText()

    def selected_text(self) -> str:
        return self.textCursor().selectedText().replace("\u2029", "\n")

    def selected_line_range(self) -> tuple[int, int]:
        """The first and last line touched by the selection."""

        cursor = self.textCursor()
        if not cursor.hasSelection():
            line = self.current_line_column()[0]
            return line, line
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        document = self.document()
        first = document.findBlock(start).blockNumber() + 1
        last_block = document.findBlock(end)
        last = last_block.blockNumber() + 1
        if end > start and last_block.position() == end:
            last = max(first, last - 1)
        return first, last

    def line_count(self) -> int:
        return self.blockCount()

    # -- cursor ------------------------------------------------------------
    def current_line_column(self) -> tuple[int, int]:
        cursor = self.textCursor()
        return cursor.blockNumber() + 1, cursor.positionInBlock() + 1

    def _symbol_at_cursor(self) -> tuple[str, str] | None:
        """Return (name, kind) of the symbol at the current cursor position, or None."""
        line, col = self.current_line_column()
        document = self.document()
        block = document.findBlockByNumber(line - 1)
        if not block.isValid():
            return None
        text = block.text()
        if col - 1 >= len(text):
            return None
        # Find the word under the cursor.
        start = col - 1
        while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
            start -= 1
        end = col - 1
        while end < len(text) and (text[end].isalnum() or text[end] == "_"):
            end += 1
        name = text[start:end]
        if not name:
            return None
        # Look up the symbol by name in the document text.
        try:
            source = self.toPlainText()
        except Exception:
            return None
        try:
            symbols = parse_symbols(source)
        except SyntaxError:
            return None
        for symbol in symbols:
            if symbol.name == name:
                return symbol.name, symbol.kind
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.lsp_definition_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def _emit_symbol_hover(self) -> None:
        """Emit ``symbol_hovered`` when the cursor lands on a symbol."""
        result = self._symbol_at_cursor()
        if result is not None:
            self.symbol_hovered.emit(result[0], result[1])
        else:
            self.symbol_hovered.emit("", "")

    def goto_line(self, line: int, column: int = 1) -> None:
        block = self.document().findBlockByNumber(max(0, line - 1))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        # Clamp to the line's length: moving past the end of an empty or short
        # line would step across the paragraph separator into the next block.
        offset = max(0, min(column - 1, len(block.text())))
        cursor.movePosition(
            QTextCursor.MoveOperation.Right,
            QTextCursor.MoveMode.MoveAnchor,
            offset,
        )
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def find(  # type: ignore[override]
        self,
        needle: str,
        *,
        forward: bool = True,
        case_sensitive: bool = False,
        whole_word: bool = False,
        use_regex: bool = False,
    ) -> bool:
        if not needle:
            return False
        flags = self._find_flags(forward, case_sensitive, whole_word)
        expression = self._expression(needle, case_sensitive) if use_regex else None
        if expression is not None:
            found = super().find(expression, flags)
        else:
            found = super().find(needle, flags)
        if not found:
            # Wrap around rather than silently doing nothing at the end.
            cursor = self.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.Start if forward else QTextCursor.MoveOperation.End
            )
            self.setTextCursor(cursor)
            found = (
                super().find(expression, flags)
                if expression is not None
                else super().find(needle, flags)
            )
        return found

    @staticmethod
    def _find_flags(
        forward: bool, case_sensitive: bool, whole_word: bool
    ) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags |= QTextDocument.FindFlag.FindBackward
        if case_sensitive:
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if whole_word:
            flags |= QTextDocument.FindFlag.FindWholeWords
        return flags

    @staticmethod
    def _expression(needle: str, case_sensitive: bool) -> QRegularExpression:
        expression = QRegularExpression(needle)
        if not case_sensitive:
            expression.setPatternOptions(QRegularExpression.PatternOption.CaseInsensitiveOption)
        return expression

    def replace_current(
        self,
        needle: str,
        replacement: str,
        *,
        case_sensitive: bool = False,
        whole_word: bool = False,
        use_regex: bool = False,
    ) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection() and self._matches(cursor, needle, case_sensitive, use_regex):
            cursor.insertText(replacement)
            self.setTextCursor(cursor)
            return True
        if not self.find(
            needle,
            case_sensitive=case_sensitive,
            whole_word=whole_word,
            use_regex=use_regex,
        ):
            return False
        self.textCursor().insertText(replacement)
        return True

    def replace_all(
        self,
        needle: str,
        replacement: str,
        *,
        case_sensitive: bool = False,
        whole_word: bool = False,
        use_regex: bool = False,
    ) -> int:
        if not needle:
            return 0
        flags = self._find_flags(True, case_sensitive, whole_word)
        expression = self._expression(needle, case_sensitive) if use_regex else None
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        count = 0
        try:
            while count < 100_000:
                found = (
                    self.document().find(expression, cursor, flags)
                    if expression is not None
                    else self.document().find(needle, cursor, flags)
                )
                if found.isNull():
                    break
                found.insertText(replacement)
                cursor = found
                count += 1
        finally:
            cursor.endEditBlock()
        return count

    @staticmethod
    def _matches(cursor: QTextCursor, needle: str, case_sensitive: bool, use_regex: bool) -> bool:
        selected = cursor.selectedText()
        if use_regex:
            import re

            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                return re.fullmatch(needle, selected, flags) is not None
            except re.error:
                return False
        return selected == needle if case_sensitive else selected.casefold() == needle.casefold()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """LSP inline UX + editing behaviour."""
        if self.isReadOnly():
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_Space and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.lsp_completion_requested.emit()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._insert_newline_with_indent()
            return
        if key == Qt.Key.Key_Tab:
            self.textCursor().insertText(_INDENT)
            return
        text = event.text()
        if text in _OPENERS:
            self._insert_pair(text)
            return
        if text in _CLOSERS and self._skip_closer(text):
            return
        super().keyPressEvent(event)

    def _insert_newline_with_indent(self) -> None:
        cursor = self.textCursor()
        prefix = cursor.block().text()[: cursor.positionInBlock()]
        leading = re.match(r"[ \t]*", prefix)
        indent = leading.group(0) if leading is not None else ""
        if prefix.rstrip().endswith(":"):
            indent += _INDENT
        cursor.insertText("\n" + indent)
        self.setTextCursor(cursor)

    def _insert_pair(self, opener: str) -> None:
        cursor = self.textCursor()
        closer = _OPENERS[opener]
        if cursor.hasSelection():
            selected = cursor.selectedText()
            cursor.insertText(f"{opener}{selected}{closer}")
            self.setTextCursor(cursor)
            return
        after = cursor.block().text()[cursor.positionInBlock() :]
        if opener in "\"'" and after[:1].isalnum():
            cursor.insertText(opener)
            return
        cursor.insertText(opener + closer)
        cursor.movePosition(QTextCursor.MoveOperation.Left)
        self.setTextCursor(cursor)

    def _skip_closer(self, closer: str) -> bool:
        cursor = self.textCursor()
        after = cursor.block().text()[cursor.positionInBlock() :]
        if after.startswith(closer) and not cursor.hasSelection():
            cursor.movePosition(QTextCursor.MoveOperation.Right)
            self.setTextCursor(cursor)
            return True
        return False

    # -- line numbers ------------------------------------------------------
    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 14 + int(self.fontMetrics().horizontalAdvance("9")) * digits

    def _update_line_number_area_width(self, _count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self._line_numbers.scroll(0, dy)
        else:
            self._line_numbers.update(0, rect.y(), self._line_numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width(0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        area = self.contentsRect()
        self._line_numbers.setGeometry(
            QRect(area.left(), area.top(), self.line_number_area_width(), area.height())
        )

    def paint_line_numbers(self, event: QPaintEvent) -> None:
        painter = QPainter(self._line_numbers)
        painter.fillRect(event.rect(), QColor("#0B1120"))
        painter.setPen(QColor("#64748B"))
        block = self.firstVisibleBlock()
        number = block.blockNumber() + 1
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        width = self._line_numbers.width() - 8
        line_height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0, top, width, line_height, Qt.AlignmentFlag.AlignRight, str(number)
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    # -- highlighting ------------------------------------------------------
    def _refresh_extra_selections(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []
        if not self.isReadOnly():
            current = QTextEdit.ExtraSelection()
            current.format.setBackground(QColor("#1E293B"))
            current.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            current.cursor = self.textCursor()
            current.cursor.clearSelection()
            selections.append(current)
        match = self._bracket_match()
        if match is not None:
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(QColor("#1E3A8A"))
            selection.cursor = self.textCursor()
            selection.cursor.setPosition(match)
            selection.cursor.movePosition(
                QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor
            )
            selections.append(selection)
        self.setExtraSelections(selections)

    def _bracket_match(self) -> int | None:
        cursor = self.textCursor()
        position = cursor.position()
        document = self.document()
        limit = min(document.characterCount(), 200_000)
        pairs = {"(": ")", "[": "]", "{": "}"}
        reverse = {value: key for key, value in pairs.items()}
        current = document.characterAt(position) if position < limit else ""
        if current in pairs:
            closer = pairs[current]
            depth = 0
            for index in range(position, limit):
                character = document.characterAt(index)
                if character == current:
                    depth += 1
                elif character == closer:
                    depth -= 1
                    if depth == 0:
                        return index
        elif position > 0:
            before = document.characterAt(position - 1)
            if before in reverse:
                opener = reverse[before]
                depth = 0
                for index in range(position - 1, max(-1, position - 200_000), -1):
                    character = document.characterAt(index)
                    if character == before:
                        depth += 1
                    elif character == opener:
                        depth -= 1
                        if depth == 0:
                            return index
        return None
