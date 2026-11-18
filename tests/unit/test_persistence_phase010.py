"""Phase 0.10 persistence and recovery tests.

Only layout, navigation and trust decisions are persisted. These tests hold the
store to its contract: tolerant reads, atomic writes, clamped values, path
sanitisation, quarantine of corrupt files and refusal to overwrite a newer
schema.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from quantsmind_agent.persistence.editor_state import (
    MAX_RESTORED_TABS,
    EditorStateStore,
    EditorTabState,
    LayoutState,
)
from quantsmind_agent.security.workspace_trust import (
    WorkspaceTrustState,
    WorkspaceTrustStore,
    canonical_root,
    root_key,
)


class TestLayoutState:
    def test_defaults(self) -> None:
        layout = LayoutState()
        assert (layout.sidebar_width, layout.agent_width, layout.bottom_height) == (300, 360, 180)
        assert layout.sidebar_visible is True
        assert layout.agent_visible is True
        assert layout.bottom_visible is False
        assert layout.activity == "explorer"

    def test_values_are_clamped(self) -> None:
        layout = LayoutState.from_dict(
            {"sidebar_width": 10, "agent_width": 10_000, "bottom_height": "wide"}
        )
        assert layout.sidebar_width == 220
        assert layout.agent_width == 560
        assert layout.bottom_height == 180

    def test_non_mapping_returns_defaults(self) -> None:
        assert LayoutState.from_dict("nonsense") == LayoutState()
        assert LayoutState.from_dict(None) == LayoutState()

    def test_roundtrip(self) -> None:
        layout = LayoutState(
            sidebar_width=420,
            agent_width=500,
            bottom_height=260,
            sidebar_visible=False,
            agent_visible=False,
            bottom_visible=True,
            activity="search",
        )
        assert LayoutState.from_dict(layout.to_dict()) == layout

    def test_activity_falls_back_when_empty(self) -> None:
        assert LayoutState.from_dict({"activity": ""}).activity == "explorer"


class TestEditorTabState:
    def test_relative_path_is_normalised(self) -> None:
        tab = EditorTabState.from_dict({"path": r"src\app\main.py"})
        assert tab is not None
        assert tab.path == "src/app/main.py"

    def test_absolute_and_traversing_paths_are_rejected(self) -> None:
        for path in ("/etc/passwd", "../secret", "src/../../secret", "C:/Windows/win.ini"):
            assert EditorTabState.from_dict({"path": path}) is None

    def test_missing_and_invalid_paths_are_rejected(self) -> None:
        assert EditorTabState.from_dict({"path": ""}) is None
        assert EditorTabState.from_dict({"path": 123}) is None
        assert EditorTabState.from_dict("not a mapping") is None

    def test_numbers_are_clamped(self) -> None:
        tab = EditorTabState.from_dict(
            {"path": "a.py", "cursor_line": -3, "cursor_column": 0, "scroll": -9}
        )
        assert tab is not None
        assert tab.cursor_line == 1
        assert tab.cursor_column == 1
        assert tab.scroll == 0

    def test_roundtrip(self) -> None:
        tab = EditorTabState(
            path="src/app/main.py",
            cursor_line=12,
            cursor_column=4,
            scroll=200,
            pinned=False,
            active=True,
        )
        assert EditorTabState.from_dict(tab.to_dict()) == tab


class TestEditorStateStore:
    def _path(self, tmp_path: Path) -> Path:
        return tmp_path / "sessions" / "editor-state.json"

    def test_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        store = EditorStateStore(self._path(tmp_path))
        assert store.layout() == LayoutState()
        assert store.state_for(tmp_path) is None

    def test_layout_persists_across_instances(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        first = EditorStateStore(path)
        first.save_layout(LayoutState(sidebar_width=333, activity="search"))
        second = EditorStateStore(path)
        layout = second.layout()
        assert layout.sidebar_width == 333
        assert layout.activity == "search"

    def test_tabs_persist_and_are_capped(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        path = self._path(tmp_path)
        store = EditorStateStore(path)
        tabs = [EditorTabState(path=f"file{index}.py") for index in range(MAX_RESTORED_TABS + 5)]
        store.save_tabs(workspace, tabs)
        state = EditorStateStore(path).state_for(workspace)
        assert state is not None
        assert len(state.tabs) == MAX_RESTORED_TABS
        assert state.root == str(canonical_root(workspace))
        assert state.updated_at

    def test_same_root_with_different_spelling_uses_one_entry(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        path = self._path(tmp_path)
        store = EditorStateStore(path)
        store.save_tabs(workspace, [EditorTabState(path="a.py")])
        store.save_tabs(str(workspace) + os.sep + ".", [EditorTabState(path="b.py")])
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert len(payload["workspaces"]) == 1

    def test_restore_tabs_reports_missing_files(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        (workspace / "present.py").write_text("x = 1\n", encoding="utf-8")
        store = EditorStateStore(self._path(tmp_path))
        store.save_tabs(
            workspace,
            [
                EditorTabState(path="present.py", cursor_line=3, active=True),
                EditorTabState(path="deleted.py"),
                EditorTabState(path="../escape.py"),
            ],
        )
        restorable, missing = store.restore_tabs(
            workspace, lambda relative: (workspace / relative).is_file()
        )
        assert [tab.path for tab in restorable] == ["present.py"]
        assert restorable[0].cursor_line == 3
        # The escaping entry is dropped when it is read, so it never reaches
        # the caller as a candidate path.
        assert missing == ["deleted.py"]

    def test_is_file_failure_counts_as_missing(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        store = EditorStateStore(self._path(tmp_path))
        store.save_tabs(workspace, [EditorTabState(path="a.py")])

        def explode(_relative: str) -> bool:
            raise OSError("disk gone")

        restorable, missing = store.restore_tabs(workspace, explode)
        assert restorable == []
        assert missing == ["a.py"]

    def test_forget_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        store = EditorStateStore(self._path(tmp_path))
        store.save_tabs(workspace, [EditorTabState(path="a.py")])
        store.forget_workspace(workspace)
        assert store.state_for(workspace) is None

    def test_corrupt_file_is_quarantined(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        store = EditorStateStore(path)
        assert store.layout() == LayoutState()
        preserved = [item.name for item in path.parent.iterdir() if ".corrupt-" in item.name]
        assert len(preserved) == 1
        assert not path.exists()

    def test_newer_schema_is_read_only(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        path.parent.mkdir(parents=True)
        original = '{"schema_version": 99, "layout": {"sidebar_width": 250}}'
        path.write_text(original, encoding="utf-8")
        store = EditorStateStore(path)
        assert store.layout() == LayoutState()
        store.save_layout(LayoutState(sidebar_width=444))
        assert path.read_text(encoding="utf-8") == original

    def test_non_integer_schema_is_read_only(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        path.parent.mkdir(parents=True)
        original = '{"schema_version": "future", "layout": {}}'
        path.write_text(original, encoding="utf-8")
        store = EditorStateStore(path)
        store.save_layout(LayoutState(sidebar_width=444))
        assert path.read_text(encoding="utf-8") == original

    def test_written_file_has_no_temporary_sibling(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        store = EditorStateStore(path)
        store.save_layout(LayoutState())
        assert not [item for item in path.parent.iterdir() if item.name.endswith(".tmp")]


class TestWorkspaceTrustStore:
    def _path(self, tmp_path: Path) -> Path:
        return tmp_path / "security" / "workspace-trust.json"

    def test_unknown_root_is_undecided(self, tmp_path: Path) -> None:
        store = WorkspaceTrustStore(self._path(tmp_path))
        assert store.state_for(tmp_path) is WorkspaceTrustState.UNDECIDED
        assert store.record_for(tmp_path) is None

    def test_trusted_and_restricted_decisions(self, tmp_path: Path) -> None:
        store = WorkspaceTrustStore(self._path(tmp_path))
        trusted = tmp_path / "trusted"
        restricted = tmp_path / "restricted"
        trusted.mkdir()
        restricted.mkdir()
        store.decide(trusted, trusted=True)
        store.decide(restricted, trusted=False)
        assert store.state_for(trusted) is WorkspaceTrustState.TRUSTED
        assert store.state_for(restricted) is WorkspaceTrustState.RESTRICTED
        record = store.record_for(trusted)
        assert record is not None
        assert record.decided_at

    def test_decisions_persist_across_instances(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        WorkspaceTrustStore(path).decide(tmp_path, trusted=False)
        reloaded = WorkspaceTrustStore(path)
        assert reloaded.state_for(tmp_path) is WorkspaceTrustState.RESTRICTED

    def test_revoke(self, tmp_path: Path) -> None:
        store = WorkspaceTrustStore(self._path(tmp_path))
        store.decide(tmp_path, trusted=True)
        assert store.revoke(tmp_path) is True
        assert store.state_for(tmp_path) is WorkspaceTrustState.UNDECIDED
        assert store.revoke(tmp_path) is False

    def test_records_are_sorted(self, tmp_path: Path) -> None:
        store = WorkspaceTrustStore(self._path(tmp_path))
        for name in ("zeta", "alpha", "middle"):
            root = tmp_path / name
            root.mkdir()
            store.decide(root, trusted=True)
        names = [Path(record.root).name for record in store.records()]
        assert names == sorted(names, key=str.lower)

    def test_canonical_key_resolves_relative_segments(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        spelling = str(workspace / "src" / "..")
        assert canonical_root(spelling) == workspace.resolve()
        assert root_key(spelling) == root_key(workspace)

    def test_key_is_case_insensitive_on_windows(self, tmp_path: Path) -> None:
        workspace = tmp_path / "CaseSensitive"
        workspace.mkdir()
        store = WorkspaceTrustStore(self._path(tmp_path))
        store.decide(workspace, trusted=True)
        if os.name == "nt":
            assert store.state_for(str(workspace).upper()) is WorkspaceTrustState.TRUSTED
        else:
            assert store.state_for(str(workspace).upper()) is WorkspaceTrustState.UNDECIDED

    def test_corrupt_file_is_quarantined(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        store = WorkspaceTrustStore(path)
        assert store.state_for(tmp_path) is WorkspaceTrustState.UNDECIDED
        preserved = [item.name for item in path.parent.iterdir() if ".corrupt-" in item.name]
        assert len(preserved) == 1
        assert not path.exists()

    def test_unknown_state_value_is_ignored(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "roots": {root_key(tmp_path): {"path": str(tmp_path), "state": "maybe"}},
                }
            ),
            encoding="utf-8",
        )
        store = WorkspaceTrustStore(path)
        assert store.state_for(tmp_path) is WorkspaceTrustState.UNDECIDED

    def test_newer_schema_refuses_writes(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        path.parent.mkdir(parents=True)
        original = '{"schema_version": 99, "roots": {}}'
        path.write_text(original, encoding="utf-8")
        store = WorkspaceTrustStore(path)
        store.decide(tmp_path, trusted=True)
        assert path.read_text(encoding="utf-8") == original

    def test_written_file_has_no_temporary_sibling(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        store = WorkspaceTrustStore(path)
        store.decide(tmp_path, trusted=True)
        assert not [item for item in path.parent.iterdir() if item.name.endswith(".tmp")]
