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
import hashlib
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
    "FileFingerprint",
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


def _ends_with_newline(path: Path) -> bool:
    """Whether ``path``'s last byte is a newline (used for line counting)."""

    try:
        with open(path, "rb") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                return True
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"
    except OSError:  # pragma: no cover - unreadable file
        return True


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


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    """Content identity of a file, used to detect concurrent/stale edits.

    ``sha256`` alone is enough to decide whether the file still matches what the
    model read. ``line_count`` is carried because edit operations address content
    by line, so a caller can reject a request whose line numbering no longer
    makes sense without reading the file twice.
    """

    path: str
    size: int
    sha256: str
    line_count: int
    exists: bool = True

    def matches(self, expected_sha256: str | None) -> bool:
        """Whether this fingerprint still equals a previously observed hash."""

        return expected_sha256 is None or expected_sha256 == self.sha256

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "line_count": self.line_count,
            "exists": self.exists,
        }


class Workspace:
    """A bounded view over a single project directory.

    Reads (:meth:`read_text`, :meth:`walk`) and v0.2 mutations
    (:meth:`write_text`, :meth:`delete_file`) share one containment rule: every
    path goes through :meth:`resolve` first, so absolute paths, drive and UNC
    references, ``..`` traversal, null bytes and symlink/junction escapes are
    rejected identically no matter which tool asked.

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
        preserve_newlines: bool = False,
    ) -> str:
        """Read a text file from inside the workspace.

        By default line endings are translated to ``\\n`` (universal newlines),
        which is what the read tool shows the model. Pass
        ``preserve_newlines=True`` to get the bytes exactly as stored -- the
        edit path needs that, otherwise a CRLF file read on Windows and written
        back would silently become LF and produce a whole-file diff.

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
            with open(
                target,
                encoding=encoding,
                errors="replace",
                newline="" if preserve_newlines else None,
            ) as handle:
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

    # -- mutation (v0.2) ---------------------------------------------------
    def fingerprint(self, relative: str | os.PathLike[str]) -> FileFingerprint:
        """Content identity of ``relative``, for stale-edit detection.

        A missing file yields ``exists=False`` with empty hashes, which is how
        the edit tool distinguishes "create this file" from "this file already
        exists and does not match".
        """

        target = self.resolve(relative)
        rel = self._relative_of(target)
        if not target.is_file():
            return FileFingerprint(rel, 0, "", 0, exists=False)
        digest = hashlib.sha256()
        line_count = 0
        size = 0
        try:
            with open(target, "rb") as handle:
                while True:
                    chunk = handle.read(65_536)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    line_count += chunk.count(b"\n")
            if size and not _ends_with_newline(target):
                # A final line without a trailing newline still counts.
                line_count += 1
        except OSError as exc:
            raise InvalidPathError(
                f"Cannot inspect {rel}: {exc}",
                user_message=f"'{rel}' could not be inspected.",
                details={"path": rel},
                cause=exc,
            ) from exc
        return FileFingerprint(rel, size, digest.hexdigest(), line_count)

    def ensure_parent(
        self,
        relative: str | os.PathLike[str],
        *,
        create_parents: bool = False,
    ) -> Path:
        """Validate the parent directory of ``relative`` before a write.

        When ``create_parents`` is false and the directory does not exist, the
        write is refused: creating folders is a separate, visible act rather
        than a side effect of saving a file. Containment is re-checked after
        resolution, so a junction in the path cannot redirect the write.
        """

        target = self.resolve(relative)
        parent = target.parent
        if parent.is_dir():
            self._assert_contained(parent, relative)
            return parent
        if not create_parents:
            rel = self._relative_of(target)
            raise InvalidPathError(
                f"Parent directory does not exist for {rel!r}",
                user_message=(
                    f"The folder for '{rel}' does not exist. Create it first with "
                    "create_parents=true if you are sure."
                ),
                details={"path": rel, "reason": "missing parent directory"},
            )
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            rel = self._relative_of(target)
            raise InvalidPathError(
                f"Cannot create the parent directory for {rel!r}: {exc}",
                user_message=f"The folder for '{rel}' could not be created.",
                details={"path": rel},
                cause=exc,
            ) from exc
        # Re-resolve after creating: on Windows a new component could sit behind
        # a pre-existing junction, and containment must be proven, not assumed.
        self._assert_contained(parent.resolve(strict=False), relative)
        return parent

    def write_text(
        self,
        relative: str | os.PathLike[str],
        content: str,
        *,
        create_parents: bool = False,
        encoding: str = "utf-8",
    ) -> FileFingerprint:
        """Create or replace a text file inside the workspace.

        Containment, traversal and symlink rules come from :meth:`resolve`, so
        there is exactly one implementation of "inside the workspace" in the
        codebase. Replacing an existing file is allowed -- that is what an
        explicit write means -- but the *caller* is responsible for having
        obtained approval and for having checked staleness; see
        :func:`quantsmind_agent.edit_ops.apply_edit`.
        """

        target = self.resolve(relative)
        rel = self._relative_of(target)
        if target.is_dir():
            raise InvalidPathError(
                f"Refusing to write over a directory: {rel}",
                user_message=f"'{rel}' is a folder. Write a file inside it instead.",
                details={"path": rel},
            )
        self.ensure_parent(relative, create_parents=create_parents)
        if "\x00" in content:
            raise InvalidPathError(
                f"Refusing to write a null byte into {rel}",
                user_message="The content contains a null byte, which cannot be written.",
                details={"path": rel, "reason": "null byte in content"},
            )
        try:
            # Write via a sibling temp file and replace, so a failure part-way
            # through cannot leave the user with a truncated file.
            temporary = target.with_name(f".{target.name}.qm-tmp")
            temporary.write_text(content, encoding=encoding, newline="")
            os.replace(temporary, target)
        except OSError as exc:
            raise InvalidPathError(
                f"Cannot write {rel}: {exc}",
                user_message=f"'{rel}' could not be written.",
                details={"path": rel},
                cause=exc,
            ) from exc
        return self.fingerprint(rel)

    def delete_file(self, relative: str | os.PathLike[str]) -> FileFingerprint:
        """Delete one regular file inside the workspace.

        v0.2 deletes files only. Directory trees are never removed recursively:
        a recursive delete is irreversible at a scale the user cannot review in
        an approval dialog, so it is out of scope until the UI can show exactly
        what would be removed.
        """

        target = self.resolve(relative)
        rel = self._relative_of(target)
        if target.is_dir() and not target.is_symlink():
            raise InvalidPathError(
                f"Refusing to delete a directory: {rel}",
                user_message=(f"'{rel}' is a folder. Delete the files inside it one at a time."),
                details={"path": rel, "reason": "directory deletion is not supported"},
            )
        if not target.exists():
            raise InvalidPathError(
                f"Cannot delete {rel}: it does not exist",
                user_message=f"'{rel}' does not exist in this workspace.",
                details={"path": rel},
            )
        try:
            before = self.fingerprint(rel)
            target.unlink()
        except OSError as exc:
            raise InvalidPathError(
                f"Cannot delete {rel}: {exc}",
                user_message=f"'{rel}' could not be deleted.",
                details={"path": rel},
                cause=exc,
            ) from exc
        return before

    def _assert_contained(self, candidate: Path, original: str | os.PathLike[str]) -> None:
        """Raise unless ``candidate`` is still inside the root."""

        if not is_within(self._root, candidate):
            raise self._deny(str(original), "resolves outside the workspace root")

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
