"""Multi-root workspace (WP1 of v0.13.0).

Registers several project roots in one engineering workspace. Each root keeps
its own :class:`Workspace` (containment, trust, permissions) and identity.
Paths are addressed as ``<root_id>:<relative>`` so cross-root confusion is
structurally impossible; bare paths resolve against the primary root only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from quantsmind_agent.errors import InvalidPathError, InvalidWorkspaceError
from quantsmind_agent.services.workspace.containment import Workspace

__all__ = ["MultiRootWorkspace", "ProjectRoot"]


@dataclass(frozen=True, slots=True)
class ProjectRoot:
    root_id: str
    path: str
    config: dict[str, object] = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.root_id}: {self.path}"


class MultiRootWorkspace:
    """One manager, many contained workspaces.

    Traversal guard: every resolution goes through the owning root's
    :class:`Workspace`, so ``..``/absolute/symlink escapes are refused per
    root exactly as in single-root mode.
    """

    def __init__(self, primary: str | Path, *, trusted: bool = False) -> None:
        self._roots: dict[str, Workspace] = {}
        self._primary: str = ""
        self._trusted = trusted
        self.add_root("primary", primary)

    def add_root(self, root_id: str, path: str | Path, *, config: dict[str, object] | None = None) -> Workspace:
        rid = self._clean_id(root_id)
        if rid in self._roots:
            raise InvalidWorkspaceError(f"duplicate root id: {rid!r}")
        try:
            ws = Workspace(path)
        except Exception as exc:
            raise InvalidWorkspaceError(f"root {rid!r} is not accessible: {exc}") from exc
        if not ws.root.is_dir():
            raise InvalidWorkspaceError(f"root {rid!r} is not a directory: {path}")
        self._roots[rid] = ws
        if not self._primary:
            self._primary = rid
        return ws

    def remove_root(self, root_id: str) -> bool:
        rid = self._clean_id(root_id)
        if rid not in self._roots:
            return False
        if rid == self._primary and len(self._roots) > 1:
            # Removing primary promotes the lexicographically first remaining root.
            candidates = sorted(r for r in self._roots if r != rid)
            self._primary = candidates[0]
        del self._roots[rid]
        if self._primary == rid:
            self._primary = next(iter(self._roots), "")
        return True

    def root_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._roots))

    def primary_id(self) -> str:
        return self._primary

    def workspace(self, root_id: str) -> Workspace:
        rid = self._clean_id(root_id)
        try:
            return self._roots[rid]
        except KeyError:
            raise InvalidWorkspaceError(f"unknown root: {rid!r}") from None

    def describe(self) -> list[str]:
        return [
            ProjectRoot(rid, str(self._roots[rid].root), {}).describe()
            for rid in sorted(self._roots)
        ]

    def resolve(self, address: str) -> tuple[str, Path]:
        """Resolve ``<root>:<relative>`` or bare ``<relative>`` (primary)."""
        if not address or not isinstance(address, str):
            raise InvalidPathError("empty path")
        if ":" in address:
            root_id, _, rel = address.partition(":")
            if root_id not in self._roots:
                raise InvalidPathError(f"unknown root in path: {address!r}")
        else:
            root_id, rel = self._primary, address
        ws = self._roots[root_id]
        try:
            resolved = ws.resolve(rel) if hasattr(ws, "resolve") else ws.root / rel
        except Exception as exc:
            raise InvalidPathError(f"path rejected in root {root_id!r}: {address!r}") from exc
        # Containment re-check (defence in depth against symlink escapes).
        try:
            root_resolved = ws.root.resolve()
        except OSError:
            root_resolved = ws.root
        try:
            target = (
                Path(resolved).resolve()
                if Path(resolved).exists()
                else (root_resolved / rel).resolve()
            )
        except OSError:
            target = root_resolved / rel
        if root_resolved not in target.parents and target != root_resolved:
            raise InvalidPathError(f"path escapes root {root_id!r}: {address!r}")
        return root_id, target

    def set_trusted(self, trusted: bool) -> None:
        self._trusted = trusted

    def is_trusted(self) -> bool:
        return self._trusted

    @staticmethod
    def _clean_id(root_id: str) -> str:
        rid = (root_id or "").strip()
        if not rid or "/" in rid or "\\" in rid or ":" in rid or any(c.isspace() for c in rid):
            raise InvalidWorkspaceError(f"invalid root id: {root_id!r}")
        return rid
