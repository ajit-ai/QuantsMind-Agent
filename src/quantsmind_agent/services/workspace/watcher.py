"""Debounced, bounded polling watcher for expanded explorer directories.

A native platform watcher would add a dependency and platform-specific failure
modes just to report "this folder changed". Instead the workbench registers the
directories it is currently showing, and this monitor compares their entry names
and modification times on a background thread. That is deliberately narrow: it
never walks a project recursively, never runs on the GUI thread, and a watcher
that misses a change costs only a manual refresh.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.services.workspace.containment import Workspace

__all__ = ["WorkspaceWatcher"]

logger = get_logger(__name__)


class WorkspaceWatcher:
    """Poll a set of workspace directories and report which ones changed."""

    def __init__(
        self,
        workspace: Workspace,
        on_change: Callable[[list[str]], None],
        *,
        interval: float = 1.0,
        settle: float = 0.25,
    ) -> None:
        self._workspace = workspace
        self._on_change = on_change
        self._interval = max(0.2, interval)
        self._settle = max(0.0, settle)
        self._directories: set[str] = {"."}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshots: dict[str, tuple[tuple[str, int, int], ...]] = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._snapshots = self._snapshot_all()
        self._thread = threading.Thread(target=self._loop, name="workspace-watcher", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- registration ------------------------------------------------------
    def set_directories(self, directories: Iterable[str]) -> None:
        """Replace the watched set with the explorer's expanded directories."""

        cleaned = {"."}
        for directory in directories:
            try:
                self._workspace.resolve(directory)
            except Exception:
                continue
            cleaned.add(directory.replace("\\", "/") or ".")
        with self._lock:
            self._directories = cleaned
        self._snapshots = self._snapshot_all()

    # -- polling -----------------------------------------------------------
    def poll_once(self) -> list[str]:
        """Compare current directory state with the previous snapshot."""

        current = self._snapshot_all()
        changed = [
            directory
            for directory, snapshot in current.items()
            if self._snapshots.get(directory) != snapshot
        ]
        self._snapshots = current
        return sorted(changed)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                changed = self.poll_once()
            except Exception:  # pragma: no cover - a watcher must never crash
                logger.exception("Workspace watcher failed to poll")
                continue
            if not changed:
                continue
            # Let a burst of writes settle before the UI refreshes once.
            if self._stop.wait(self._settle):
                return
            try:
                self._on_change(changed)
            except Exception:  # pragma: no cover - callback belongs to the GUI
                logger.exception("Workspace watcher callback failed")

    def _snapshot_all(self) -> dict[str, tuple[tuple[str, int, int], ...]]:
        with self._lock:
            directories = sorted(self._directories)
        snapshots: dict[str, tuple[tuple[str, int, int], ...]] = {}
        for directory in directories:
            snapshots[directory] = self._snapshot(directory)
        return snapshots

    def _snapshot(self, relative: str) -> tuple[tuple[str, int, int], ...]:
        try:
            target = self._workspace.resolve(relative)
        except Exception:
            return ()
        if not target.is_dir():
            return ()
        rows: list[tuple[str, int, int]] = []
        try:
            with os.scandir(target) as scan:
                for item in scan:
                    try:
                        info = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    rows.append((item.name, info.st_mtime_ns, info.st_size))
        except OSError:
            return ()
        rows.sort()
        return tuple(rows)

    def absolute(self, relative: str) -> Path:
        """Resolve a watched directory for a consumer that needs the path."""

        return self._workspace.resolve(relative)
