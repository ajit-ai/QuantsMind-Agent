"""User-initiated file operations inside a workspace.

Every operation resolves through :meth:`Workspace.resolve`, so absolute paths,
``..`` traversal, drive references and symlink escapes are refused by the same
rule the agent's tools use. Deleting prefers the platform's recycle bin or user
trash; permanent deletion is a separate, explicit choice made by the caller
after a confirmation.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from quantsmind_agent.errors import InvalidPathError, QuantsMindError
from quantsmind_agent.persistence.atomic import atomic_write_bytes
from quantsmind_agent.services.workspace.containment import Workspace, is_within

__all__ = [
    "DeleteResult",
    "FileOperationError",
    "FileOperationService",
    "TrashUnavailable",
]

_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_WINDOWS_ILLEGAL = set('<>:"/\\|?*')


class FileOperationError(QuantsMindError):
    """A file operation was refused or failed in a user-explainable way."""


class TrashUnavailable(FileOperationError):
    """The platform trash cannot be used; permanent deletion needs consent."""

    def __init__(self, message: str, *, user_message: str) -> None:
        super().__init__(
            message,
            user_message=user_message,
            details={"requires_permanent_confirmation": True},
        )


@dataclass(frozen=True, slots=True)
class DeleteResult:
    path: str
    method: str  # "recycle_bin" | "trash" | "permanent"

    @property
    def recovered(self) -> bool:
        return self.method != "permanent"


class FileOperationService:
    """Create, rename, move and delete paths under one workspace root."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    def trash_available(self) -> bool:
        return sys.platform == "win32" or Path.home().exists()

    # -- validation --------------------------------------------------------
    def validate_name(self, name: str) -> str:
        """Return a safe single path component or raise a readable refusal."""

        cleaned = name.strip()
        if not cleaned:
            raise FileOperationError(
                "A file name cannot be empty",
                user_message="Enter a name for the new item.",
            )
        if cleaned in (".", ".."):
            raise FileOperationError(
                f"Refusing the name {cleaned!r}",
                user_message="That name is reserved. Choose another one.",
            )
        if "\x00" in cleaned:
            raise FileOperationError(
                "A file name cannot contain a null byte",
                user_message="That name contains an invalid character.",
            )
        illegal = _WINDOWS_ILLEGAL if sys.platform == "win32" else {"/"}
        bad = sorted({character for character in cleaned if character in illegal})
        if bad:
            raise FileOperationError(
                f"Invalid characters in {cleaned!r}: {bad}",
                user_message=(
                    "A name cannot contain " + ", ".join(repr(item) for item in bad) + "."
                ),
            )
        if any(ord(character) < 32 for character in cleaned):
            raise FileOperationError(
                "Control character in file name",
                user_message="That name contains an invalid control character.",
            )
        if sys.platform == "win32":
            stem = cleaned.split(".", 1)[0].lower()
            if stem in _WINDOWS_RESERVED:
                raise FileOperationError(
                    f"{cleaned!r} is a reserved Windows name",
                    user_message=f"'{cleaned}' is reserved by Windows. Choose another name.",
                )
            if cleaned.endswith((" ", ".")):
                raise FileOperationError(
                    "Windows names cannot end with a space or dot",
                    user_message="A name cannot end with a space or a full stop.",
                )
        return cleaned

    # -- creation ----------------------------------------------------------
    def create_file(self, relative: str, content: str = "") -> str:
        """Create a new empty (or pre-filled) file. Never overwrites."""

        target = self._workspace.resolve(relative)
        rel = self._workspace.relative_to_root(target)
        if target.exists():
            raise FileOperationError(
                f"Refusing to overwrite {rel}",
                user_message=f"'{rel}' already exists. Choose another name.",
            )
        if not target.parent.is_dir():
            raise FileOperationError(
                f"Parent directory for {rel} does not exist",
                user_message=f"The folder for '{rel}' does not exist.",
            )
        self._assert_contained(target)
        try:
            atomic_write_bytes(target, content.encode("utf-8"))
        except OSError as exc:
            raise FileOperationError(
                f"Cannot create {rel}: {exc}",
                user_message=f"'{rel}' could not be created.",
            ) from exc
        return rel

    def create_directory(self, relative: str) -> str:
        """Create one new directory under an existing parent."""

        target = self._workspace.resolve(relative)
        rel = self._workspace.relative_to_root(target)
        if target.exists():
            raise FileOperationError(
                f"Refusing to create existing path {rel}",
                user_message=f"'{rel}' already exists.",
            )
        if not target.parent.is_dir():
            raise FileOperationError(
                f"Parent directory for {rel} does not exist",
                user_message=f"The folder for '{rel}' does not exist.",
            )
        self._assert_contained(target)
        try:
            target.mkdir()
        except OSError as exc:
            raise FileOperationError(
                f"Cannot create directory {rel}: {exc}",
                user_message=f"The folder '{rel}' could not be created.",
            ) from exc
        return rel

    # -- rename / move -----------------------------------------------------
    def rename(self, relative: str, new_name: str) -> str:
        """Rename or move ``relative`` to ``new_name`` (a relative path)."""

        source = self._workspace.resolve(relative)
        source_rel = self._workspace.relative_to_root(source)
        if source == self._workspace.root:
            raise FileOperationError(
                "Refusing to rename the workspace root",
                user_message="The project folder itself cannot be renamed here.",
            )
        target = self._workspace.resolve(new_name)
        target_rel = self._workspace.relative_to_root(target)
        if target.exists():
            raise FileOperationError(
                f"Refusing to overwrite {target_rel}",
                user_message=f"'{target_rel}' already exists. Choose another name.",
            )
        if not target.parent.is_dir():
            raise FileOperationError(
                f"Parent directory for {target_rel} does not exist",
                user_message=f"The folder for '{target_rel}' does not exist.",
            )
        self._assert_contained(target)
        try:
            source.rename(target)
        except OSError as exc:
            raise FileOperationError(
                f"Cannot rename {source_rel} to {target_rel}: {exc}",
                user_message=f"'{source_rel}' could not be renamed.",
            ) from exc
        # A rename can traverse a pre-existing junction; prove the result is
        # still inside the root rather than assuming the pre-check held.
        try:
            final = target.resolve(strict=True)
        except OSError as exc:
            raise FileOperationError(
                f"Renamed path cannot be verified: {exc}",
                user_message=f"'{source_rel}' was renamed but could not be verified.",
            ) from exc
        if not is_within(self._workspace.root, final):
            raise FileOperationError(
                "Renamed path escaped the workspace root",
                user_message="The rename was refused because it left the project folder.",
            )
        return target_rel

    # -- deletion ----------------------------------------------------------
    def delete(self, relative: str, *, permanent: bool = False) -> DeleteResult:
        """Delete a file or directory, using the recycle bin when possible."""

        target = self._workspace.resolve(relative)
        rel = self._workspace.relative_to_root(target)
        if target == self._workspace.root:
            raise FileOperationError(
                "Refusing to delete the workspace root",
                user_message="Close the project instead of deleting its root folder.",
            )
        if not target.exists():
            raise FileOperationError(
                f"{rel} does not exist",
                user_message=f"'{rel}' no longer exists.",
            )
        self._assert_contained(target)
        if permanent:
            self._delete_permanent(target)
            return DeleteResult(rel, "permanent")
        method = self._move_to_trash(target)
        return DeleteResult(rel, method)

    def _delete_permanent(self, target: Path) -> None:
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            raise FileOperationError(
                f"Cannot delete {target}: {exc}",
                user_message=f"'{target.name}' could not be deleted.",
            ) from exc

    def _move_to_trash(self, target: Path) -> str:
        # Annotated so a type checker targeting one platform does not treat
        # the other platform's branch as unreachable.
        platform: str = sys.platform
        if platform == "win32":
            try:
                _windows_recycle(target)
            except OSError as exc:
                raise TrashUnavailable(
                    f"Recycle bin refused {target}: {exc}",
                    user_message=(
                        f"'{target.name}' could not be moved to the Recycle Bin. "
                        "Deleting it permanently cannot be undone."
                    ),
                ) from exc
            return "recycle_bin"
        try:
            _unix_trash(target)
        except OSError as exc:
            raise TrashUnavailable(
                f"User trash refused {target}: {exc}",
                user_message=(
                    f"'{target.name}' could not be moved to the Trash. "
                    "Deleting it permanently cannot be undone."
                ),
            ) from exc
        return "trash"

    # -- helpers -----------------------------------------------------------
    def _assert_contained(self, target: Path) -> None:
        if not is_within(self._workspace.root, target.resolve(strict=False)):
            raise InvalidPathError(
                f"Path escaped the workspace: {target}",
                user_message="That path is outside the project folder.",
            )


def _windows_recycle(path: Path) -> None:
    """Move ``path`` to the Recycle Bin through the shell API."""

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400

    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = f"{path}\0\0"
    operation.pTo = None
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"SHFileOperationW returned {result}")


def _unix_trash(path: Path) -> None:
    """Move ``path`` to the platform trash location.

    macOS uses ``~/.Trash``. Other Unix systems implement the freedesktop trash
    specification: the file is moved into ``files/`` with a matching
    ``.trashinfo`` record, so a file manager can restore it.
    """

    home = Path.home()
    if sys.platform == "darwin":
        trash = home / ".Trash"
        trash.mkdir(parents=True, exist_ok=True)
        destination = _unique_destination(trash / path.name)
        shutil.move(str(path), str(destination))
        return

    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    trash = data_home / "Trash"
    files = trash / "files"
    info = trash / "info"
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)
    destination = _unique_destination(files / path.name)
    info_file = info / f"{destination.name}.trashinfo"
    payload = (
        "[Trash Info]\n"
        f"Path={urllib.parse.quote(str(path.resolve()))}\n"
        f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())}\n"
    )
    info_file.write_text(payload, encoding="utf-8")
    try:
        shutil.move(str(path), str(destination))
    except BaseException:
        with contextlib.suppress(OSError):
            info_file.unlink()
        raise


def _unique_destination(candidate: Path) -> Path:
    if not candidate.exists():
        return candidate
    stem = candidate.name
    suffix = candidate.suffix
    if suffix:
        stem = stem[: -len(suffix)]
    for counter in range(1, 10_000):
        alternative = candidate.with_name(f"{stem} ({counter}){suffix}")
        if not alternative.exists():
            return alternative
    raise OSError("Could not allocate a unique trash name")
