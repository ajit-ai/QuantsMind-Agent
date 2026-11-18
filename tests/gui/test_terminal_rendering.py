"""Automated rendering tests for TerminalWidget (WP2)."""

from __future__ import annotations

import pytest

from quantsmind_agent.gui.shell.terminal_widget import MAX_SCROLLBACK, TerminalWidget

pytestmark = pytest.mark.gui


def _widget(qapp: object) -> TerminalWidget:
    return TerminalWidget()


class TestAnsiSequences:
    def test_sgr_colour_does_not_leak_parameter_bytes(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("\x1b[31mRED\x1b[0m")
        assert w.text() == "RED"

    def test_plain_text(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("hello")
        assert w.text() == "hello"

    def test_trailing_escape_is_safe(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("tail\x1b")
        assert w.text() == "tail"

    def test_incomplete_csi_is_safe(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("x\x1b[31")
        assert w.text() == "x"

    def test_malformed_csi_params_are_safe(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("a\x1b[;m b")
        assert "a" in w.text()

    def test_cursor_backward_overwrites(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("ab\x1b[1DXY")
        assert w.text() == "aXY"

    def test_erase_display(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("one\ntwo\x1b[2J")
        assert w.text() == ""

    def test_erase_line(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("hello\r\x1b[KX")
        assert w.text() == "X"

    def test_unicode(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("é✓")
        assert w.text() == "é✓"

    def test_clear_resets_buffer(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("hi")
        w.clear()
        assert w.text() == ""
        w.feed_text("new")
        assert w.text() == "new"

    def test_scrollback_is_bounded(self, qapp: object) -> None:
        w = _widget(qapp)
        w.feed_text("line\n" * (MAX_SCROLLBACK + 50))
        assert len(w._lines) <= MAX_SCROLLBACK
