"""Multi-root GUI tests (Correction A)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.gui.shell.zones import BottomPanel
from quantsmind_agent.services.workspace.multi_root import MultiRootWorkspace

pytestmark = pytest.mark.gui


def test_add_root_flow(qapp: object, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    panel = BottomPanel()
    multi = MultiRootWorkspace(tmp_path / "a", trusted=True)
    panel.set_roots(list(multi.root_ids()), multi.primary_id(), primary=multi.primary_id())
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getExistingDirectory", lambda *a, **k: str(tmp_path / "b")
    )
    # Simulate Add Root handler logic through the panel + manager.
    multi.add_root("b", tmp_path / "b")
    panel.set_roots(list(multi.root_ids()), multi.primary_id(), primary=multi.primary_id())
    assert panel.root_selector.count() == 2
    assert multi.root_ids() == ("b", "primary")
    assert "primary" in panel.root_status.text()
    assert panel.remove_root_button.isEnabled()


def test_duplicate_rejected(qapp: object, tmp_path: Path) -> None:
    multi = MultiRootWorkspace(tmp_path)
    from quantsmind_agent.errors import InvalidWorkspaceError

    with pytest.raises(InvalidWorkspaceError):
        multi.add_root("primary", tmp_path)


def test_remove_and_promotion(qapp: object, tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    multi = MultiRootWorkspace(tmp_path / "a")
    multi.add_root("b", tmp_path / "b")
    assert multi.remove_root("b")
    assert multi.root_ids() == ("primary",)
    multi.add_root("b", tmp_path / "b")
    assert multi.remove_root("primary")
    assert multi.primary_id() == "b"


def test_missing_and_traversal(qapp: object, tmp_path: Path) -> None:
    from quantsmind_agent.errors import InvalidPathError, InvalidWorkspaceError

    multi = MultiRootWorkspace(tmp_path)
    with pytest.raises(InvalidWorkspaceError):
        multi.add_root("missing", tmp_path / "nope")
    with pytest.raises(InvalidPathError):
        multi.resolve("../escape")


def test_refresh_after_change(qapp: object, tmp_path: Path) -> None:
    panel = BottomPanel()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    multi = MultiRootWorkspace(tmp_path / "a", trusted=True)
    multi.add_root("b", tmp_path / "b")
    panel.set_roots(list(multi.root_ids()), multi.primary_id(), primary=multi.primary_id())
    assert panel.root_selector.count() == 2
    multi.remove_root("b")
    panel.set_roots(list(multi.root_ids()), multi.primary_id(), primary=multi.primary_id())
    assert panel.root_selector.count() == 1
    assert not panel.remove_root_button.isEnabled()
