"""Editor <-> LSP lifecycle integration tests (WP4 blocker)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.gui.editor.tabs import EditorArea
from quantsmind_agent.gui.shell.zones import BottomPanel

pytestmark = pytest.mark.gui


class _RecordingManager:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def did_open(self, uri, lang, text):
        self.calls.append(("did_open", uri, text))

    def did_change(self, uri, text):
        self.calls.append(("did_change", uri, text))

    def did_close(self, uri):
        self.calls.append(("did_close", uri))

    def hover(self, uri, line, column):
        self.calls.append(("hover", uri, line, column))
        return {"contents": {"value": "hovered"}}

    def completion(self, uri, line, column):
        self.calls.append(("completion", uri, line, column))
        return [{"label": "x"}]

    def definition(self, uri, line, column):
        self.calls.append(("definition", uri, line, column))
        return [{"uri": uri, "range": {}}]

    def references(self, uri, line, column):
        self.calls.append(("references", uri, line, column))
        return []

    def symbols_fallback(self, text):
        from quantsmind_agent.code_index import parse_symbols

        return parse_symbols(text)

    def all_diagnostics(self):
        return {
            "file:///x.py": [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1},
                    },
                    "severity": 1,
                    "message": "bad",
                }
            ]
        }

    @property
    def status(self):
        return "language server ready: fake"


def test_open_edit_close_sync(qapp: object, tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text("x = 1\n")
    from quantsmind_agent.gui.editor.document import DocumentRegistry, TextDocument

    reg = DocumentRegistry()
    doc = TextDocument(path=path, text="x = 1\n")
    reg.add(doc)
    area = EditorArea()
    mgr = _RecordingManager()
    area.set_lsp_manager(mgr)
    tab = area._active_pane.open(doc)  # type: ignore[union-attr]
    assert any(c[0] == "did_open" for c in mgr.calls)
    # Edit -> did_change
    tab.editor.setPlainText("x = 2\n")
    assert any(c[0] == "did_change" for c in mgr.calls)
    # Switch tabs does not re-open (no extra did_open for same doc).
    opens_before = sum(1 for c in mgr.calls if c[0] == "did_open")
    area._active_pane.setCurrentIndex(area._active_pane.currentIndex())  # type: ignore[union-attr]
    opens_after = sum(1 for c in mgr.calls if c[0] == "did_open")
    assert opens_after == opens_before
    # Close -> did_close
    area._active_pane.remove(tab)  # type: ignore[union-attr]
    assert any(c[0] == "did_close" for c in mgr.calls)


def test_intelligence_and_problems_panel(qapp: object, tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text("def foo():\n    pass\n")
    from quantsmind_agent.gui.editor.document import DocumentRegistry, TextDocument

    reg = DocumentRegistry()
    doc = TextDocument(path=path, text="def foo():\n    pass\n")
    reg.add(doc)
    area = EditorArea()
    mgr = _RecordingManager()
    area.set_lsp_manager(mgr)
    area._active_pane.open(doc)  # type: ignore[union-attr]
    pane = area._active_pane
    assert pane.hover_at(0, 0) == {"contents": {"value": "hovered"}}  # type: ignore[union-attr]
    assert pane.completion_at(0, 0) == [{"label": "x"}]  # type: ignore[union-attr]
    assert pane.definition_at(0, 0)  # type: ignore[union-attr]
    assert pane.references_at(0, 0) == []  # type: ignore[union-attr]
    # Tier-A fallback still works without a server.
    syms = pane.symbols_fallback("def bar():\n    pass\n")  # type: ignore[union-attr]
    assert any(getattr(s, "name", "") == "bar" for s in syms)
    # Diagnostics reach the Problems panel.
    panel = BottomPanel()
    panel.set_problems(mgr.all_diagnostics(), status=mgr.status)
    assert "bad" in panel.problems.text()
    panel.deleteLater()


def test_unavailable_manager_keeps_tier_a(qapp: object, tmp_path: Path) -> None:
    from quantsmind_agent.gui.editor.document import DocumentRegistry, TextDocument
    from quantsmind_agent.services.lsp.manager import LSPManager

    path = tmp_path / "x.py"
    path.write_text("def baz():\n    pass\n")
    reg = DocumentRegistry()
    doc = TextDocument(path=path, text="def baz():\n    pass\n")
    reg.add(doc)
    area = EditorArea()
    mgr = LSPManager(None)  # no server configured
    area.set_lsp_manager(mgr)
    pane = area._active_pane
    pane.open(doc)  # type: ignore[union-attr]
    assert pane.hover_at(0, 0) is None  # type: ignore[union-attr]
    syms = pane.symbols_fallback("def baz():\n    pass\n")  # type: ignore[union-attr]
    assert any(getattr(s, "name", "") == "baz" for s in syms)
    mgr.shutdown()
