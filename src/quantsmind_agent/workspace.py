"""Workspace abstraction with strict path containment.

A :class:`Workspace` is the only sanctioned way to touch user files. It
resolves the project root to a canonical absolute path, refuses to escape that
root (no ``..`` traversal, no absolute paths, no symlink escapes), and exposes
file metadata and safe reads.

Everything the Agent Core does goes through this class; the GUI never touches
``os`` directly.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from quantsmind_agent.errors import InvalidPathError, InvalidWorkspaceError

__all__ = [
    "DEFAULT_BINARY_SUFFIXES",
    "DEFAULT_IGNORED_DIRECTORIES",
    "FileEntry",
    "Workspace",
    "is_within",
]

#: Directory names never traversed or shown to the model.
DEFAULT_IGNORED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
        ".eggs",
        ".next",
        ".nuxt",
        "target",
        "vendor",
        ".gradle",
        ".terraform",
        "site-packages",
    }
)

#: Binary/heavy suffixes skipped by search and structure listing.
DEFAULT_BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".tiff",
        ".pdf",
        ".zip",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".tar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".pyd",
        ".bin",
        ".obj",
        ".class",
        ".mp3",
        ".mp4",
        ".avi",
        ".mkv",
        ".mov",
        ".wav",
        ".flac",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pkl",
        ".pickle",
        ".npy",
        ".npz",
        ".pt",
        ".pth",
        ".onnx",
        ".safetensors",
        ".parquet",
    }
)


def is_within(root: Path, candidate: Path) -> bool:
    """Return ``True`` when ``candidate`` is ``root`` or lives under it.

    Both arguments must already be resolved; this helper never touches disk.
    """

    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _iso(mtime: float | None) -> str:
    if mtime is None:
        return ""
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class FileEntry:
    """Metadata for one file or directory inside the workspace."""

    path: str
    name: str
    is_dir: bool
    size: int
    modified: str

    @property
    def kind(self) -> str:
        return "directory" if self.is_dir else "file"

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "name": self.name,
            "type": self.kind,
            "size": self.size,
            "modified": self.modified,
        }


class Workspace:
    """A read-mostly view over a single project directory.

    Parameters
    ----------
    root:
        Directory the user selected. Must exist and be a directory.
    ignored_directories:
        Directory *names* skipped while walking (compared by name, not path).
    binary_suffixes:
        Suffixes treated as binary by the read/search tools.
    max_read_bytes:
        Upper bound for :meth:`read_text`.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        ignored_directories: Iterable[str] | None = None,
        binary_suffixes: Iterable[str] | None = None,
        max_read_bytes: int = 2_000_000,
    ) -> None:
        candidate = Path(root).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidWorkspaceError(
                f"Workspace path does not exist: {candidate}",
                user_message=(
                    f"The folder '{candidate}' could not be opened. Check that the path "
                    "exists and that you have permission to read it."
                ),
                details={"path": str(candidate)},
                cause=exc,
            ) from exc
        if not resolved.is_dir():
            raise InvalidWorkspaceError(
                f"Workspace path is not a directory: {resolved}",
                user_message=(
                    f"'{resolved.name}' is a file. Please select a project folder instead."
                ),
                details={"path": str(resolved)},
            )
        self._root = resolved
        self._ignored = frozenset(ignored_directories or DEFAULT_IGNORED_DIRECTORIES)
        self._binary = frozenset(s.lower() for s in (binary_suffixes or DEFAULT_BINARY_SUFFIXES))
        self._max_read_bytes = max_read_bytes

    # -- identity ---------------------------------------------------------
    @property
    def root(self) -> Path:
        """Canonical absolute workspace root."""

        return self._root

    @property
    def ignored_directories(self) -> frozenset[str]:
        return self._ignored

    @property
    def max_read_bytes(self) -> int:
        return self._max_read_bytes

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Workspace(root={str(self._root)!r})"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self._root)

    def __fspath__(self) -> str:  # pragma: no cover - convenience
        return str(self._root)

    # -- path handling ----------------------------------------------------
    def _deny(self, raw: str, why: str) -> InvalidPathError:
        return InvalidPathError(
            f"Path rejected: {raw!r} ({why})",
            user_message=(
                f"'{raw}' is outside the current workspace. QuantsMind-Agent can only "
                "read files inside the project folder you opened."
            ),
            details={"path": raw, "reason": why, "root": str(self._root)},
        )

    def resolve(self, relative: str | os.PathLike[str]) -> Path:
        """Resolve ``relative`` to an absolute path inside the workspace.

        Raises
        ------
        InvalidPathError
            When the path is absolute, empty, contains ``..`` segments, carries
            a drive/UNC reference, or resolves (after following links) outside
            the workspace root.
        """

        raw = str(relative).strip().strip('"')
        if not raw:
            raise self._deny(str(relative), "empty path")
        if "\x00" in raw:
            raise self._deny(raw, "path contains a null byte")

        # Accept both separator styles so model output is never rejected merely
        # for using "/" on Windows (or "\" out of shell habit).
        unified = raw.replace("\\", "/")
        if unified.startswith("/"):
            raise self._deny(raw, "absolute POSIX path")

        windows = PureWindowsPath(raw)
        if windows.is_absolute() or windows.drive or windows.root:
            raise self._deny(raw, "absolute Windows path or drive reference")
        if unified.startswith("~"):
            raise self._deny(raw, "home-directory reference")

        pure = PurePosixPath(unified)
        if pure.is_absolute():
            raise self._deny(raw, "absolute path")
        for part in pure.parts:
            if part == "..":
                raise self._deny(raw, "parent-directory traversal")

        try:
            candidate = (self._root / pure).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise self._deny(raw, "path could not be resolved") from exc

        if not is_within(self._root, candidate):
            raise self._deny(raw, "resolves outside the workspace root")
        return candidate

    def relative(self, path: str | os.PathLike[str]) -> str:
        """Return the POSIX-style path of ``path`` relative to the root.

        ``path`` must be workspace-relative; use :meth:`_relative_of` for
        absolute paths the workspace itself produced.
        """

        return self._relative_of(self.resolve(path))

    def _relative_of(self, absolute: Path) -> str:
        """Convert an already-resolved absolute path to its workspace form."""

        resolved = absolute.resolve(strict=False)
        if not is_within(self._root, resolved):
            raise self._deny(str(absolute), "path is outside the workspace root")
        rel = PurePosixPath(resolved.relative_to(self._root).as_posix()).as_posix()
        return rel or "."

    def exists(self, relative: str | os.PathLike[str]) -> bool:
        try:
            return self.resolve(relative).exists()
        except InvalidPathError:
            return False

    def is_dir(self, relative: str | os.PathLike[str]) -> bool:
        try:
            return self.resolve(relative).is_dir()
        except InvalidPathError:
            return False

    def is_file(self, relative: str | os.PathLike[str]) -> bool:
        try:
            return self.resolve(relative).is_file()
        except InvalidPathError:
            return False

    def is_binary(self, relative: str | os.PathLike[str]) -> bool:
        return PurePosixPath(str(relative)).suffix.lower() in self._binary

    # -- inspection -------------------------------------------------------
    def iter_entries(self, relative: str | os.PathLike[str] = ".") -> Iterator[FileEntry]:
        """Yield direct children of ``relative``; directories first."""

        base = self.resolve(relative)
        if not base.is_dir():
            raise self._deny(str(relative), "not a directory")

        try:
            with os.scandir(base) as scan:
                children = list(scan)
        except OSError as exc:
            raise InvalidPathError(
                f"Cannot list {self._relative_of(base)}: {exc}",
                user_message="That folder could not be listed.",
                details={"path": self._relative_of(base)},
                cause=exc,
            ) from exc

        entries: list[FileEntry] = []
        for item in children:
            if item.is_dir(follow_symlinks=False) and item.name in self._ignored:
                continue
            rel = self._relative_of(Path(item.path))
            try:
                info = item.stat(follow_symlinks=False)
                size, mtime = info.st_size, info.st_mtime
            except OSError:
                size, mtime = 0, None
            entries.append(
                FileEntry(
                    path=rel,
                    name=item.name,
                    is_dir=item.is_dir(follow_symlinks=False),
                    size=size,
                    modified=_iso(mtime),
                )
            )

        entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))
        yield from entries

    def list_dir(self, relative: str | os.PathLike[str] = ".") -> list[FileEntry]:
        """Return the direct children of ``relative`` as a sorted list."""

        return list(self.iter_entries(relative))

    def walk(
        self,
        relative: str | os.PathLike[str] = ".",
        *,
        max_entries: int = 5000,
        include_binary: bool = False,
    ) -> Iterator[FileEntry]:
        """Depth-first walk yielding every reachable *file* under ``relative``."""

        base = self.resolve(relative)
        if not base.is_dir():
            raise self._deny(str(relative), "not a directory")

        count = 0
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in self._ignored)
            current = Path(dirpath)
            for name in sorted(filenames):
                full = current / name
                if not include_binary and full.suffix.lower() in self._binary:
                    continue
                try:
                    info = full.stat()
                    size, mtime = info.st_size, info.st_mtime
                except OSError:
                    size, mtime = 0, None
                yield FileEntry(self._relative_of(full), name, False, size, _iso(mtime))
                count += 1
                if count >= max_entries:
                    return

    def read_text(
        self,
        relative: str | os.PathLike[str],
        *,
        max_bytes: int | None = None,
        encoding: str = "utf-8",
    ) -> str:
        """Read a text file from inside the workspace.

        Raises
        ------
        InvalidPathError
            The path escapes the workspace or does not point at a readable
            text file.
        """

        target = self.resolve(relative)
        rel = self._relative_of(target)
        if not target.is_file():
            raise InvalidPathError(
                f"Not a file: {rel}",
                user_message=f"'{rel}' does not exist in this workspace.",
                details={"path": rel},
            )
        if target.suffix.lower() in self._binary:
            raise InvalidPathError(
                f"Refusing to read binary file: {rel}",
                user_message=(f"'{rel}' looks like a binary file and cannot be read as text."),
                details={"path": rel},
            )

        limit = max_bytes if max_bytes is not None else self._max_read_bytes
        try:
            with open(target, encoding=encoding, errors="replace") as handle:
                return handle.read(limit)
        except OSError as exc:
            raise InvalidPathError(
                f"Cannot read {rel}: {exc}",
                user_message=f"'{rel}' could not be read.",
                details={"path": rel},
                cause=exc,
            ) from exc

    def file_size(self, relative: str | os.PathLike[str]) -> int:
        return self.resolve(relative).stat().st_size

    def count_children(self, relative: str | os.PathLike[str] = ".") -> int:
        try:
            with os.scandir(self.resolve(relative)) as scan:
                return sum(1 for _ in scan)
        except OSError:
            return 0

    def glob(
        self,
        pattern: str,
        *,
        relative: str | os.PathLike[str] = ".",
        max_results: int = 200,
    ) -> list[str]:
        """Return workspace-relative paths matching a glob ``pattern``."""

        base = self.resolve(relative)
        if not base.is_dir():
            raise self._deny(str(relative), "not a directory")
        try:
            with os.scandir(base) as scan:
                names = sorted(item.name for item in scan)
        except OSError as exc:
            raise self._deny(str(relative), "directory could not be listed") from exc

        matched = [
            self._relative_of(base / name)
            for name in names
            if name not in self._ignored and fnmatch.fnmatch(name, pattern)
        ]
        return matched[:max_results]
