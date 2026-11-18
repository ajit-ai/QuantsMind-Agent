"""Editor and workbench state persistence (schema 1).

Only layout and navigation state is stored: zone sizes, the selected activity,
open tab paths, cursor positions and scroll offsets. File contents and secrets
are never written. Tab paths are relative to the workspace root, so a corrupt or
stale entry cannot name a file outside the project once it is reopened.

A missing file is not an error; a corrupt file is moved aside and the workbench
starts with defaults. A file written by a newer schema is left untouched and
this version refuses to overwrite it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.persistence.atomic import (
    atomic_write_json,
    quarantine_file,
    read_json_object,
)
from quantsmind_agent.security.workspace_trust import canonical_root, root_key

__all__ = [
    "EditorStateStore",
    "EditorTabState",
    "LayoutState",
    "WorkspaceEditorState",
]

logger = get_logger(__name__)

SCHEMA_VERSION = 1
MAX_RESTORED_TABS = 50


@dataclass(slots=True)
class EditorTabState:
    """One restored editor tab. Paths are workspace-relative."""

    path: str
    cursor_line: int = 1
    cursor_column: int = 1
    scroll: int = 0
    pinned: bool = True
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "cursor_line": self.cursor_line,
            "cursor_column": self.cursor_column,
            "scroll": self.scroll,
            "pinned": self.pinned,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> EditorTabState | None:
        if not isinstance(payload, dict):
            return None
        path = payload.get("path")
        if not isinstance(path, str) or not path:
            return None
        pure = PurePosixPath(path.replace("\\", "/"))
        windows = PureWindowsPath(path)
        if (
            pure.is_absolute()
            or windows.drive
            or windows.root
            or any(part == ".." for part in pure.parts)
        ):
            return None
        return cls(
            path=pure.as_posix(),
            cursor_line=max(1, _int(payload.get("cursor_line"), 1)),
            cursor_column=max(1, _int(payload.get("cursor_column"), 1)),
            scroll=max(0, _int(payload.get("scroll"), 0)),
            pinned=bool(payload.get("pinned", True)),
            active=bool(payload.get("active", False)),
        )


@dataclass(slots=True)
class LayoutState:
    """Zone sizes and visibility for one workbench window."""

    sidebar_width: int = 300
    agent_width: int = 360
    bottom_height: int = 180
    sidebar_visible: bool = True
    agent_visible: bool = True
    bottom_visible: bool = False
    activity: str = "explorer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sidebar_width": self.sidebar_width,
            "agent_width": self.agent_width,
            "bottom_height": self.bottom_height,
            "sidebar_visible": self.sidebar_visible,
            "agent_visible": self.agent_visible,
            "bottom_visible": self.bottom_visible,
            "activity": self.activity,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> LayoutState:
        if not isinstance(payload, dict):
            return cls()
        return cls(
            sidebar_width=_clamp(payload.get("sidebar_width"), 220, 500, 300),
            agent_width=_clamp(payload.get("agent_width"), 280, 560, 360),
            bottom_height=_clamp(payload.get("bottom_height"), 120, 600, 180),
            sidebar_visible=bool(payload.get("sidebar_visible", True)),
            agent_visible=bool(payload.get("agent_visible", True)),
            bottom_visible=bool(payload.get("bottom_visible", False)),
            activity=str(payload.get("activity") or "explorer"),
        )


@dataclass(slots=True)
class WorkspaceEditorState:
    root: str
    tabs: list[EditorTabState] = field(default_factory=list)
    updated_at: str = ""


class EditorStateStore:
    """Reads and writes ``sessions/editor-state.json``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._payload: dict[str, Any] | None = None
        self._read_only = False

    # -- loading -----------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if self._payload is not None:
            return self._payload
        payload = read_json_object(self.path)
        if payload is None:
            if self.path.exists():
                moved = quarantine_file(self.path, reason="corrupt")
                logger.warning("Editor state was corrupt; moved to %s", moved)
            self._payload = {"schema_version": SCHEMA_VERSION, "layout": {}, "workspaces": {}}
            return self._payload
        version = payload.get("schema_version")
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            logger.warning(
                "Editor state uses schema %r; this version reads schema %d",
                version,
                SCHEMA_VERSION,
            )
            self._read_only = True
            self._payload = {"schema_version": SCHEMA_VERSION, "layout": {}, "workspaces": {}}
            return self._payload
        if not isinstance(payload.get("layout"), dict):
            payload["layout"] = {}
        if not isinstance(payload.get("workspaces"), dict):
            payload["workspaces"] = {}
        payload["schema_version"] = SCHEMA_VERSION
        self._payload = payload
        return payload

    def layout(self) -> LayoutState:
        return LayoutState.from_dict(self._load().get("layout"))

    def state_for(self, workspace: str | os.PathLike[str]) -> WorkspaceEditorState | None:
        entry = self._load()["workspaces"].get(root_key(workspace))
        if not isinstance(entry, dict):
            return None
        tabs: list[EditorTabState] = []
        raw_tabs = entry.get("tabs")
        if isinstance(raw_tabs, list):
            for raw in raw_tabs[:MAX_RESTORED_TABS]:
                tab = EditorTabState.from_dict(raw)
                if tab is not None:
                    tabs.append(tab)
        root = entry.get("root")
        return WorkspaceEditorState(
            root=str(root or canonical_root(workspace)),
            tabs=tabs,
            updated_at=str(entry.get("updated_at") or ""),
        )

    # -- saving ------------------------------------------------------------
    def save_layout(self, layout: LayoutState) -> None:
        payload = self._load()
        payload["layout"] = layout.to_dict()
        self._write()

    def save_tabs(
        self,
        workspace: str | os.PathLike[str],
        tabs: list[EditorTabState],
    ) -> None:
        payload = self._load()
        key = root_key(workspace)
        payload["workspaces"][key] = {
            "root": str(canonical_root(workspace)),
            "tabs": [tab.to_dict() for tab in tabs[:MAX_RESTORED_TABS]],
            "updated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        }
        self._write()

    def forget_workspace(self, workspace: str | os.PathLike[str]) -> None:
        payload = self._load()
        if payload["workspaces"].pop(root_key(workspace), None) is not None:
            self._write()

    def _write(self) -> None:
        if self._read_only:
            logger.warning("Refusing to overwrite a newer editor-state file")
            return
        payload = self._load()
        payload["schema_version"] = SCHEMA_VERSION
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("Could not persist editor state: %s", exc)

    # -- restoration helpers ----------------------------------------------
    def restore_tabs(
        self,
        workspace: str | os.PathLike[str],
        is_file: Callable[[str], bool],
    ) -> tuple[list[EditorTabState], list[str]]:
        """Return restorable tabs and the paths that no longer exist.

        ``is_file`` is injected by the caller (normally ``Workspace.is_file``),
        so this module stays free of filesystem policy decisions.
        """

        state = self.state_for(workspace)
        if state is None:
            return [], []
        restorable: list[EditorTabState] = []
        missing: list[str] = []
        for tab in state.tabs:
            try:
                exists = bool(is_file(tab.path))
            except Exception:
                exists = False
            if exists:
                restorable.append(tab)
            else:
                missing.append(tab.path)
        return restorable, missing


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    return max(low, min(high, _int(value, default)))
