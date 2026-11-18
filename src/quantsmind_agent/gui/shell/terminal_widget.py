"""Terminal widget for the Development Studio (WP2 of v0.12.0).

Renders PTY output with ANSI/VT escape sequence handling, including colors,
text attributes, cursor movement, and screen clearing. Built on QPlainTextEdit
for fast, reliable text rendering.

The widget is intentionally a *display* surface: it receives text from a
PtySession (via the TerminalTool) and renders it. It does not manage the
process lifecycle itself.
"""

from __future__ import annotations

from typing import NamedTuple

from PySide6.QtGui import QColor, QFont, QTextCharFormat
from PySide6.QtWidgets import QPlainTextEdit, QWidget

__all__ = ["TerminalWidget"]

#: Maximum scrollback lines. Older lines are discarded to bound memory.
MAX_SCROLLBACK = 5000

#: ANSI 16-color palette (standard + bright).
ANSI_COLORS = [
    QColor("#000000"),  # 0: black
    QColor("#800000"),  # 1: red
    QColor("#008000"),  # 2: green
    QColor("#808000"),  # 3: yellow
    QColor("#000080"),  # 4: blue
    QColor("#800080"),  # 5: magenta
    QColor("#008080"),  # 6: cyan
    QColor("#C0C0C0"),  # 7: white
    QColor("#808080"),  # 8: bright black (gray)
    QColor("#FF0000"),  # 9: bright red
    QColor("#00FF00"),  # 10: bright green
    QColor("#FFFF00"),  # 11: bright yellow
    QColor("#0000FF"),  # 12: bright blue
    QColor("#FF00FF"),  # 13: bright magenta
    QColor("#00FFFF"),  # 14: bright cyan
    QColor("#FFFFFF"),  # 15: bright white
]


class _Char(NamedTuple):
    """A single character cell with attributes."""

    char: str
    fg: int | None = None  # ANSI color index
    bg: int | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False


class TerminalWidget(QPlainTextEdit):
    """A terminal rendering widget.

    Displays PTY output with ANSI/VT escape sequence interpretation. The widget
    grows a scrollback buffer up to :attr:`MAX_SCROLLBACK` lines.

    Public methods are called by the embedding window to feed output and
    control the terminal:

    * :meth:`feed_text` - append raw bytes from the PTY
    * :meth:`resize` - change terminal dimensions
    * :meth:`clear` - clear the screen
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TerminalWidget")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setUndoRedoEnabled(False)
        self.setMaximumBlockCount(MAX_SCROLLBACK)

        # Fixed-width font for terminal rendering.
        font = QFont("Consolas, Courier New, monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)

        # Terminal state.
        self.cursor_row = 0
        self.cursor_col = 0
        self._columns: int = 80
        self._rows: int = 24
        self.fg = 7  # default foreground: white
        self.bg = 0  # default background: black
        self.bold = False
        self.italic = False
        self.underline = False
        # Bound terminal height so it cannot expand beyond its container.
        self.setMaximumHeight(200)
        self.setMinimumHeight(100)

        # Buffer of lines: list of list of _Char.
        self._lines: list[list[_Char]] = [[]]
        self._dirty = False

        # ANSI escape sequence parser state.
        self._escape_buf = ""

    # -- public API ---------------------------------------------------------

    def feed_text(self, text: str) -> None:
        """Feed decoded text from the PTY into the terminal.

        Parses ANSI escape sequences and updates the display. Call this
        whenever :class:`PtySession.read` returns new output.
        """

        if not text:
            return

        # Append text to the current line, processing escape sequences.
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                # Start of escape sequence. A trailing ESC with no following
                # byte is incomplete: drop it safely instead of indexing past
                # the end or raising.
                self._flush()
                i += 1
                if i >= len(text):
                    break
                if text[i] == "[":
                    # CSI sequence.
                    i += 1
                    i = self._csi(text, i)
                    continue
                elif text[i] in "0123456789:":
                    # Parameter sequence without leading [ (rare).
                    i += 1
                    continue
                else:
                    # Other escape (e.g., \x1b c = reset).
                    self._handle_escape(text[i])
                    i += 1
                    continue
            elif ch == "\n":
                self._flush()
                self._newline()
                i += 1
            elif ch == "\r":
                self._flush()
                self.cursor_col = 0
                i += 1
            elif ch == "\t":
                self._flush()
                self.cursor_col = (self.cursor_col // 8 + 1) * 8
                i += 1
            else:
                self._put_char(ch)
                i += 1

        self._flush()
        self._render()

    def set_terminal_size(self, columns: int, rows: int) -> None:
        """Change terminal dimensions. Content is preserved where possible."""

        if columns <= 0 or rows <= 0:
            return
        self._columns = columns
        self._rows = rows
        self.cursor_col = min(self.cursor_col, max(0, columns - 1))
        self.cursor_row = min(self.cursor_row, max(0, rows - 1))

    def clear(self) -> None:
        """Clear the screen (like ANSI ESC [2J)."""
        self._lines = [[]]
        self.cursor_row = 0
        self.cursor_col = 0
        self.fg = 7
        self.bg = 0
        self.bold = False
        self.italic = False
        self.underline = False
        # Call the base implementation explicitly: this override shadows
        # QPlainTextEdit.clear(), so self.clear() would recurse forever.
        QPlainTextEdit.clear(self)
        self._dirty = True

    # -- internal: buffer management ---------------------------------------

    def _flush(self) -> None:
        """Flush any pending character into the buffer."""

        # Handled inline in _put_char; kept for symmetry.
        pass

    def _put_char(self, ch: str) -> None:
        """Place a character at the cursor position."""

        # Ensure the current line exists.
        while len(self._lines) <= self.cursor_row:
            self._lines.append([])

        line = self._lines[self.cursor_row]

        # Ensure the line is long enough.
        while len(line) <= self.cursor_col:
            line.append(_Char(" ", self.fg, self.bg, self.bold, self.italic, self.underline))

        line[self.cursor_col] = _Char(ch, self.fg, self.bg, self.bold, self.italic, self.underline)
        self.cursor_col += 1

    def _newline(self) -> None:
        """Move to the next line, enforcing the bounded scrollback."""
        self.cursor_row += 1
        self.cursor_col = 0
        while len(self._lines) <= self.cursor_row:
            self._lines.append([])
        # Drop oldest lines beyond the scrollback ceiling so a runaway
        # process cannot grow memory without bound.
        overflow = len(self._lines) - MAX_SCROLLBACK
        if overflow > 0:
            del self._lines[:overflow]
            self.cursor_row -= overflow

    # -- ANSI processing ---------------------------------------------------

    def _csi(self, text: str, i: int) -> int:
        """Process a CSI (Control Sequence Introducer) sequence.

        Collects parameter bytes and the final byte, then dispatches.
        Returns the index just past the sequence (or ``len(text)`` when the
        sequence is incomplete at end-of-input, in which case it is dropped
        safely without dispatching).
        """

        # Collect parameter bytes.
        start = i
        while i < len(text) and text[i] in "0123456789;:<=>?":
            i += 1
        if i >= len(text):
            # Incomplete CSI at end of input: nothing safe to dispatch.
            return i
        final = text[i]
        i += 1

        params_text = text[start : i - 1] if i > start + 1 else ""
        try:
            params = [int(p) for p in params_text.split(";") if p != ""]
        except ValueError:
            params = []
        if not params:
            params = [0]

        self._dispatch_csi(final, params)
        return i

    def _dispatch_csi(self, final: str, params: list[int]) -> None:
        """Dispatch a parsed CSI sequence."""

        if final in "ABCD":
            # Cursor movement: U=up, D=down, C=forward, B=backward.
            n = max(1, params[0])
            if final == "A":
                self.cursor_row = max(0, self.cursor_row - n)
            elif final == "B":
                self.cursor_row = min(len(self._lines) - 1, self.cursor_row + n)
            elif final == "C":
                self.cursor_col = min(self._columns, self.cursor_col + n)
            elif final == "D":
                self.cursor_col = max(0, self.cursor_col - n)
        elif final == "H":
            # Cursor position: CUP row,col.
            row = max(1, params[0]) - 1
            col = max(1, params[1] if len(params) > 1 else 1) - 1
            self.cursor_row = min(len(self._lines) - 1, row)
            self.cursor_col = col
        elif final == "J":
            # Erase display: 0=end, 1=start, 2=all.
            n = params[0] if params else 0
            if n == 2:
                self._lines = [[]]
                self.cursor_row = 0
                self.cursor_col = 0
            elif n == 1:
                # Erase start of screen through cursor: keep tail from cursor.
                for line in self._lines[: self.cursor_row]:
                    line.clear()
                self._lines[self.cursor_row] = self._lines[self.cursor_row][self.cursor_col :]
            elif n == 0:
                # Erase cursor through end of screen: keep head before cursor.
                self._lines[self.cursor_row] = self._lines[self.cursor_row][: self.cursor_col]
                for line in self._lines[self.cursor_row + 1 :]:
                    line.clear()
        elif final == "K":
            # Erase line: 0=end, 1=start, 2=all.
            n = params[0] if params else 0
            if n == 2:
                self._lines[self.cursor_row] = []
            elif n == 1:
                # Erase start of line through cursor: keep tail from cursor.
                self._lines[self.cursor_row] = self._lines[self.cursor_row][self.cursor_col :]
            elif n == 0:
                # Erase cursor through end of line: keep head before cursor.
                self._lines[self.cursor_row] = self._lines[self.cursor_row][: self.cursor_col]
        elif final == "m":
            # SGR: Select Graphic Rendition.
            self._sgr(params)
        elif final == "h":
            # DEC private mode set (e.g., ?25l = hide cursor).
            pass
        elif final == "l":
            # DEC private mode reset.
            pass
        elif final == "J":
            pass  # handled above

    def _sgr(self, params: list[int]) -> None:
        """Process SGR (Select Graphic Rendition) parameters."""

        for p in params:
            if p == 0:
                # Reset all attributes.
                self.fg = 7
                self.bg = 0
                self.bold = False
                self.italic = False
                self.underline = False
            elif p == 1:
                self.bold = True
            elif p == 3:
                self.italic = True
            elif p == 4:
                self.underline = True
            elif p == 7:
                # Reverse: swap fg/bg.
                self.fg, self.bg = self.bg, self.fg
            elif 30 <= p <= 37:
                self.fg = p - 30
            elif 40 <= p <= 47:
                self.bg = p - 40
            elif 90 <= p <= 97:
                self.fg = (p - 90) + 8
            elif 100 <= p <= 107:
                self.bg = (p - 100) + 8
            elif p == 22:
                self.bold = False
            elif p == 23:
                self.italic = False
            elif p == 24:
                self.underline = False
            elif p == 27:
                self.fg = 7
                self.bg = 0

    def _handle_escape(self, ch: str) -> None:
        """Handle a single ESC + char sequence (non-CSI)."""

        if ch == "c":
            # Reset terminal (RIS).
            self._lines = [[]]
            self.cursor_row = 0
            self.cursor_col = 0
            self.fg = 7
            self.bg = 0
            self.bold = False
            self.italic = False
            self.underline = False

    # -- rendering ----------------------------------------------------------

    def _render(self) -> None:
        """Render the buffer to the QPlainTextEdit."""

        self._dirty = False

        self.setPlainText("")
        cursor = self.textCursor()

        for line in self._lines:
            if not line:
                cursor.insertText("\n")
                continue
            for cell in line:
                fmt = QTextCharFormat()
                fmt.setForeground(self._fg_color(cell.fg))
                fmt.setBackground(self._bg_color(cell.bg))
                fmt.setFontWeight(QFont.Weight.Bold if cell.bold else QFont.Weight.Normal)
                fmt.setFontItalic(cell.italic)
                fmt.setFontUnderline(cell.underline)
                cursor.insertText(cell.char, fmt)
            cursor.insertText("\n")

        self.setTextCursor(cursor)

    def text(self) -> str:
        """Return the full text content of the terminal buffer."""

        lines = []
        for line in self._lines:
            if not line:
                lines.append("")
            else:
                lines.append("".join(cell.char for cell in line))
        return "\n".join(lines)

    def _fg_color(self, idx: int | None) -> QColor:
        if idx is None:
            return QColor("#000000")  # default foreground
        return ANSI_COLORS[min(idx, 15)]

    def _bg_color(self, idx: int | None) -> QColor:
        if idx is None:
            return QColor("#000000")  # default background
        return ANSI_COLORS[min(idx, 15)]
