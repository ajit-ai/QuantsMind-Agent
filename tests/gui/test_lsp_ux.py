"""LSP inline UX tests (v1.1 WP2.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.gui.editor.document import DocumentRegistry, TextDocument
from quantsmind_agent.gui.editor.tabs import EditorArea

pytestmark = pytest.mark.gui


class _Mgr:
    def hover(self, uri, line, column):
        return {"contents": {"value": "hov"}}

    def completion(self, uri, line, column):
        return [{"label": "alpha"}, {"label": "beta"}]

    def definition(self, uri, line, column):
        return [{"uri": "file:///x.py", "range": {"start": {"line": 4, "character": 0}}}]

    def references(self, uri, line, column):
        return []

    def symbols_fallback(self, text):
        from quantsmind_agent.code_index import parse_symbols

        return parse_symbols(text)

    def did_open(self, *a):
        pass

    def did_change(self, *a):
        pass

    def did_close(self, *a):
        pass


def _area(tmp_path: Path):
    path = tmp_path / "x.py"
    path.write_text("def foo():\n    pass\n")
    reg = DocumentRegistry()
    doc = TextDocument(path=path, text="def foo():\n    pass\n")
    reg.add(doc)
    area = EditorArea()
    area.set_lsp_manager(_Mgr())
    area._active_pane.open(doc)  # type: ignore[union-attr]
    return area


def test_completion_request_does_not_crash(qapp: object, tmp_path: Path) -> None:
    # Menu.exec() is modal; verify the data path instead of popping a menu.
    area = _area(tmp_path)
    assert area._active_pane.completion_at(0, 0) == [{"label": "alpha"}, {"label": "beta"}]  # type: ignore[union-attr]


def test_definition_request_does_not_crash(qapp: object, tmp_path: Path) -> None:
    area = _area(tmp_path)
    pane = area._active_pane
    pane._on_lsp_definition()  # type: ignore[union-attr]
    assert "Definition" in pane.active_tab().editor.toolTip()  # type: ignore[union-attr]


def test_hover_available(qapp: object, tmp_path: Path) -> None:
    area = _area(tmp_path)
    assert area._active_pane.hover_at(0, 0) == {"contents": {"value": "hov"}}  # type: ignore[union-attr]
