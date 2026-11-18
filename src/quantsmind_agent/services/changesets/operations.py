"""ChangeSet operations: what a transaction is allowed to do to the workspace.

QM-P09-CS-001 §6.2 and §8. This module is where an operation stops being a
suggestion from a model and becomes something the engine can be held to. It does
no I/O of its own beyond what a supplied resolver does: an operation that cannot
be validated without touching the disk would validate against whatever the disk
happened to look like at the time.

Two rules from the specification shape everything below.

- **Fails closed.** Anything the accepted semantics do not describe -- a directory
  rename, a chained rename, a second writer to one path, a mode change, an
  unsupported type -- is refused rather than approximated.
- **Refusal says why.** Every rejection carries the operation id and a stable
  reason code, because a conflict the user cannot name is a conflict the user
  cannot resolve.
"""

from __future__ import annotations

import fnmatch
import ntpath
import os
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

__all__ = [
    "DEFAULT_MAX_CONTENT_BYTES",
    "FILE_OPERATION_TYPES",
    "MODE_POLICY_PRESERVE",
    "CommandSpec",
    "Fingerprint",
    "Hunk",
    "Operation",
    "OperationError",
    "OperationStatus",
    "OperationType",
    "OperationValidationError",
    "Risk",
    "StagedContent",
    "WorkspaceResolver",
    "validate_operations",
]

#: The only mode policy v1.0.0 supports (§8.1). Permission-bit changes are a
#: separate, reviewed operation, not a side effect of editing a file.
MODE_POLICY_PRESERVE = "PRESERVE"

#: Per-file content cap from §8.1. It is a product limit, not a filesystem one: a
#: 400 MB "modification" is refused before any pre-image is staged.
DEFAULT_MAX_CONTENT_BYTES = 20 * 1024 * 1024

_OP_ID_RE = re.compile(r"^op-(\d{4})$")
_APPROVAL_BOUND_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Windows refuses these as file names in any directory. Checking the final part
#: only would miss ``src/NUL/x.py``, so every part is tested.
_RESERVED_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "CLOCK$",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: Characters Windows reserves inside a name. ``\`` is absent because it is a
#: separator the workspace resolver accepts; ``:`` is present because an alternate
#: data stream is addressed as ``file.txt:stream``.
_WINDOWS_FORBIDDEN = set('<>:"|?*')


class OperationType(StrEnum):
    """The five operations the accepted specification authorises (§6.2).

    No others. A sixth type would have no apply semantics, no recovery
    classification and no rollback rule, and the engine would be inventing all
    three at commit time -- which is the worst possible moment to improvise.
    """

    CREATE_FILE = "CREATE_FILE"
    MODIFY_FILE = "MODIFY_FILE"
    DELETE_FILE = "DELETE_FILE"
    RENAME_FILE = "RENAME_FILE"
    RUN_COMMAND = "RUN_COMMAND"


#: Operations that change the filesystem. ``RUN_COMMAND`` is deliberately absent:
#: §8.6 says a command affects verification, never file state.
FILE_OPERATION_TYPES: frozenset[OperationType] = frozenset(
    {
        OperationType.CREATE_FILE,
        OperationType.MODIFY_FILE,
        OperationType.DELETE_FILE,
        OperationType.RENAME_FILE,
    }
)


class OperationStatus(StrEnum):
    """Per-operation execution state (§6.2)."""

    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    #: The engine cannot tell whether the effect landed. It exists because a crash
    #: between a write and its journal record is a real state, and calling it
    #: FAILED would let recovery overwrite something the user may now own.
    UNKNOWN = "UNKNOWN"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """The state an operation expects, before or after (§6.2).

    ``mtime_ns`` is recorded for evidence and deliberately excluded from
    comparison. A timestamp changes when a user opens a file in an editor or when
    a backup tool touches it; treating that as "the user changed the content"
    would block legitimate work, and treating it as "nothing changed" would
    overwrite their edit. Existence, size and content hash decide.
    """

    exists: bool
    sha256: str = ""
    size: int = 0
    mode: int = 0o644
    mtime_ns: int = 0

    def __post_init__(self) -> None:
        if self.exists and not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError(
                f"a fingerprint of an existing file needs a sha256, got {self.sha256!r}"
            )
        if not self.exists and self.sha256:
            raise ValueError("a fingerprint of an absent file cannot carry a sha256")

    def matches(self, other: Fingerprint) -> bool:
        """Content comparison only: existence, hash and size (§6.2)."""

        return (
            self.exists == other.exists and self.sha256 == other.sha256 and self.size == other.size
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "sha256": self.sha256,
            "size": self.size,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Fingerprint:
        return cls(
            exists=bool(payload.get("exists", False)),
            sha256=str(payload.get("sha256") or ""),
            size=int(payload.get("size") or 0),
            mode=int(payload.get("mode") or 0o644),
            mtime_ns=int(payload.get("mtime_ns") or 0),
        )


@dataclass(frozen=True, slots=True)
class Hunk:
    """A rendered region of change. Advisory at v0.11.0; selectable from v0.13.0."""

    hunk_id: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    patch: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hunk_id": self.hunk_id,
            "old_start": self.old_start,
            "old_lines": self.old_lines,
            "new_start": self.new_start,
            "new_lines": self.new_lines,
            "patch": self.patch,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Hunk:
        return cls(
            hunk_id=str(payload.get("hunk_id", "")),
            old_start=int(payload.get("old_start", 0)),
            old_lines=int(payload.get("old_lines", 0)),
            new_start=int(payload.get("new_start", 0)),
            new_lines=int(payload.get("new_lines", 0)),
            patch=str(payload.get("patch", "")),
        )


@dataclass(frozen=True, slots=True)
class StagedContent:
    """What the operation will write, held as a content-addressed blob (§6.2).

    The blob is the authority. ``diff.patch`` is a rendering for the reviewer, and
    the engine never applies from it -- a patch can be mis-split, a blob cannot.
    """

    staged_blob: str
    sha256: str
    size: int
    encoding: str = "utf-8"
    eol: str = "LF"
    binary: bool = False
    hunks: tuple[Hunk, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "staged_blob": self.staged_blob,
            "sha256": self.sha256,
            "size": self.size,
            "encoding": self.encoding,
            "eol": self.eol,
            "binary": self.binary,
            "hunks": [hunk.to_dict() for hunk in self.hunks],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> StagedContent:
        return cls(
            staged_blob=str(payload.get("staged_blob", "")),
            sha256=str(payload.get("sha256", "")),
            size=int(payload.get("size", 0)),
            encoding=str(payload.get("encoding", "utf-8")),
            eol=str(payload.get("eol", "LF")),
            binary=bool(payload.get("binary", False)),
            hunks=tuple(Hunk.from_dict(item) for item in payload.get("hunks") or ()),
        )


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A command operation (§8.6): fixed argv, no shell, bounded output.

    ``argv`` is a list because a string would have to be parsed, and parsing means
    quoting rules, and quoting rules are how ``; rm -rf`` gets in. Metacharacters
    are refused at validation so the refusal is visible in the review, not only in
    a policy engine at run time.
    """

    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int = 120
    output_limit_bytes: int = 262_144
    expected_exit_codes: tuple[int, ...] = (0,)

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "output_limit_bytes": self.output_limit_bytes,
            "expected_exit_codes": list(self.expected_exit_codes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CommandSpec:
        return cls(
            argv=tuple(str(item) for item in payload.get("argv") or ()),
            cwd=str(payload.get("cwd", ".")),
            timeout_seconds=int(payload.get("timeout_seconds", 120)),
            output_limit_bytes=int(payload.get("output_limit_bytes", 262_144)),
            expected_exit_codes=tuple(
                int(item) for item in payload.get("expected_exit_codes") or (0,)
            ),
        )


@dataclass(frozen=True, slots=True)
class OperationError:
    code: str
    message: str
    recoverable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "recoverable": self.recoverable}


@dataclass(frozen=True, slots=True)
class Operation:
    """One governed change, with everything needed to verify its effect (WP1-B).

    The pair ``base``/``result`` is the whole point: an operation states what it
    expects to find and what it expects to leave behind. That is what makes a
    compare-and-swap commit, a digest-guarded rollback and a crash scan possible --
    without them the engine could only hope nothing else had touched the file.
    """

    op_id: str
    order: int
    type: OperationType
    root_id: str = ""
    path: str = ""
    destination_path: str = ""
    base: Fingerprint = field(default_factory=lambda: Fingerprint(exists=False))
    result: Fingerprint = field(default_factory=lambda: Fingerprint(exists=False))
    content: StagedContent | None = None
    mode_policy: str = MODE_POLICY_PRESERVE
    risk: Risk = Risk.LOW
    status: OperationStatus = OperationStatus.PENDING
    applied_at: str = ""
    actual_sha256: str = ""
    error: OperationError | None = None
    command: CommandSpec | None = None

    @property
    def is_file_operation(self) -> bool:
        return self.type in FILE_OPERATION_TYPES

    @property
    def target_path(self) -> str:
        """The path the operation leaves behind -- a rename's destination, else its path."""

        return self.destination_path or self.path

    def with_status(self, status: OperationStatus, **updates: Any) -> Operation:
        """A copy carrying a new execution state.

        Operations are frozen so that a manifest cannot be mutated underneath a
        digest computed over it; progress is recorded by replacing the entry.
        """

        return replace(self, status=status, **updates)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op_id": self.op_id,
            "order": self.order,
            "type": self.type.value,
            "root_id": self.root_id,
            "path": self.path,
            "mode_policy": self.mode_policy,
            "risk": self.risk.value,
            "status": self.status.value,
            "base": self.base.to_dict(),
            "result": self.result.to_dict(),
        }
        if self.destination_path:
            payload["destination_path"] = self.destination_path
        if self.content is not None:
            payload["content"] = self.content.to_dict()
        if self.command is not None:
            payload["command"] = self.command.to_dict()
        if self.applied_at:
            payload["applied_at"] = self.applied_at
        if self.actual_sha256:
            payload["actual_sha256"] = self.actual_sha256
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Operation:
        content = payload.get("content")
        command = payload.get("command")
        error = payload.get("error")
        raw_type = str(payload.get("type", ""))
        if raw_type not in set(OperationType):
            raise ValueError(f"unsupported operation type {raw_type!r}")
        return cls(
            op_id=str(payload.get("op_id", "")),
            order=int(payload.get("order", 0)),
            type=OperationType(raw_type),
            root_id=str(payload.get("root_id", "")),
            path=str(payload.get("path", "")),
            destination_path=str(payload.get("destination_path", "")),
            base=Fingerprint.from_dict(payload.get("base") or {}),
            result=Fingerprint.from_dict(payload.get("result") or {}),
            content=StagedContent.from_dict(content) if isinstance(content, Mapping) else None,
            mode_policy=str(payload.get("mode_policy", MODE_POLICY_PRESERVE)),
            risk=Risk(str(payload.get("risk", "low"))),
            status=OperationStatus(str(payload.get("status", "PENDING"))),
            applied_at=str(payload.get("applied_at", "")),
            actual_sha256=str(payload.get("actual_sha256", "")),
            error=(
                OperationError(
                    code=str(error.get("code", "")),
                    message=str(error.get("message", "")),
                    recoverable=bool(error.get("recoverable", True)),
                )
                if isinstance(error, Mapping)
                else None
            ),
            command=CommandSpec.from_dict(command) if isinstance(command, Mapping) else None,
        )


class OperationValidationError(ValueError):
    """An operation that the accepted semantics do not describe.

    Carries ``op_id`` and a stable ``reason_code`` rather than a prose sentence:
    the UI and the audit trail both have to say *which* operation and *why*, and a
    reason code is the only form that survives translation.
    """

    def __init__(self, reason_code: str, message: str, *, op_id: str = "") -> None:
        self.reason_code = reason_code
        self.op_id = op_id
        super().__init__(f"{reason_code}: {message}")


class WorkspaceResolver(Protocol):
    """The piece of the workspace this module needs: root-scoped, safe resolution.

    :class:`~quantsmind_agent.services.workspace.containment.Workspace` satisfies
    it. Naming the seam keeps validation testable without a filesystem and keeps a
    single containment rule in force: the same ``resolve`` the apply engine uses.
    """

    def resolve_for(self, root_id: str, relative: str) -> object: ...


def normalize_path(relative: str) -> str:
    """Canonicalise a workspace-relative path to POSIX form, once (§8.1).

    Model output mixes ``src\\a.py`` and ``src/a.py``; comparing them as strings
    would let two operations look unrelated while naming one file. Dotfiles keep
    their leading dot -- only a ``./`` segment means "here".
    """

    unified = relative.replace("\\", "/")
    if not unified:
        return ""
    normalized = posixpath.normpath(unified)
    return "" if normalized == "." else normalized


def _path_parts(relative: str) -> list[str]:
    return [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]


def check_path_syntax(relative: str, *, op_id: str, what: str) -> str:
    """Reject the path forms §8.1 and §14.1 refuse, before any resolution.

    The workspace resolver already refuses absolute, traversal, null byte and
    escape. These are the Windows-specific shapes it accepts because POSIX does
    not object to them: a stream separator, a reserved device name, a trailing dot
    or space that the shell strips afterwards.
    """

    if not relative:
        raise OperationValidationError("path_empty", f"{what} is empty", op_id=op_id)
    if "\x00" in relative:
        raise OperationValidationError(
            "path_null_byte", f"{what} contains a null byte", op_id=op_id
        )
    # Refused here as well as by the resolver: a manifest read on another machine
    # must not depend on which containment helper happens to be wired up, and an
    # absolute path is never a workspace-relative one.
    drive = ntpath.splitdrive(relative)[0]
    if relative.startswith(("/", "\\")) or drive:
        # Refused structurally as well as by the resolver: a manifest read on
        # another machine must not depend on which containment helper is wired up,
        # and an absolute path is never a workspace-relative one. The drive test
        # comes before the character scan so "C:/x" is reported as the absolute path
        # it is rather than as a stray colon that sends the user hunting for a
        # stream.
        reason = "path_absolute_drive" if drive else "path_absolute"
        raise OperationValidationError(reason, f"{what} is an absolute path", op_id=op_id)

    parts = _path_parts(relative)
    if not parts:
        raise OperationValidationError("path_empty", f"{what} resolves to nothing", op_id=op_id)

    for part in parts:
        if part == "..":
            raise OperationValidationError(
                "path_traversal", f"{what} traverses upward", op_id=op_id
            )
        if part != part.rstrip(". "):
            # Windows drops trailing dots and spaces when it opens the file, so
            # "a.py." and "a.py" are two names for one target.
            raise OperationValidationError(
                "path_trailing_dot", f"{what} has a trailing dot or space", op_id=op_id
            )
        if part.upper().split(".")[0] in _RESERVED_DEVICES:
            raise OperationValidationError(
                "path_reserved_name", f"{what} uses a device name", op_id=op_id
            )

    # ``:`` is forbidden in every part except a stream-free name; the first part may
    # legitimately carry a drive only if it were absolute, which the resolver stops.
    joined = posixpath.join(*parts)
    if any(character in joined for character in _WINDOWS_FORBIDDEN):
        raise OperationValidationError(
            "path_invalid_character",
            f"{what} contains a character Windows reserves",
            op_id=op_id,
        )
    if ntpath.splitdrive(relative)[0]:
        raise OperationValidationError(
            "path_absolute_drive", f"{what} carries a drive", op_id=op_id
        )
    return joined


def _check_scope(relative: str, scope: Sequence[str], *, op_id: str) -> None:
    """Whether the path falls inside a declared scope entry (§8.8).

    An operation outside the approved scope is refused even when the workspace
    would allow it: the scope is what the reviewer actually agreed to.
    """

    if not scope:
        return
    parts = _path_parts(relative)
    for entry in scope:
        entry_parts = _path_parts(normalize_path(entry))
        if not entry_parts:
            continue
        if len(entry_parts) <= len(parts) and all(
            fnmatch.fnmatchcase(parts[index], entry_parts[index])
            for index in range(len(entry_parts))
        ):
            return
        if fnmatch.fnmatchcase(relative, normalize_path(entry)):
            return
    raise OperationValidationError(
        "out_of_scope", f"{relative} is outside the declared scope {list(scope)}", op_id=op_id
    )


def _validate_structure(operation: Operation) -> None:
    op_id = operation.op_id
    if not _OP_ID_RE.match(op_id):
        raise OperationValidationError(
            "op_id_invalid", "operation ids are 'op-' plus four order digits", op_id=op_id
        )
    if int(op_id[3:]) != operation.order:
        raise OperationValidationError(
            "op_id_order_mismatch",
            f"id {op_id} does not match order {operation.order}",
            op_id=op_id,
        )
    if operation.mode_policy != MODE_POLICY_PRESERVE:
        raise OperationValidationError(
            "mode_policy_unsupported",
            f"{operation.mode_policy!r} is not a supported mode policy",
            op_id=op_id,
        )

    if operation.is_file_operation:
        _validate_file_structure(operation)
    else:
        _validate_command_structure(operation)


def _validate_file_structure(operation: Operation) -> None:
    op_id = operation.op_id
    if not operation.root_id:
        raise OperationValidationError(
            "root_required", "a file operation needs a root id", op_id=op_id
        )
    check_path_syntax(operation.path, op_id=op_id, what="path")

    rename = operation.type is OperationType.RENAME_FILE
    if rename != bool(operation.destination_path):
        raise OperationValidationError(
            "destination_mismatch",
            "only RENAME_FILE carries a destination_path",
            op_id=op_id,
        )
    if rename:
        destination = check_path_syntax(
            operation.destination_path, op_id=op_id, what="destination_path"
        )
        if destination == operation.path:
            raise OperationValidationError(
                "rename_to_self", "a rename must change the name", op_id=op_id
            )

    expected_base = operation.type is not OperationType.CREATE_FILE
    if operation.base.exists is not expected_base:
        raise OperationValidationError(
            "base_exists_mismatch",
            f"{operation.type.value} expects base.exists={str(expected_base).lower()}",
            op_id=op_id,
        )

    if operation.type is OperationType.DELETE_FILE:
        if operation.result.exists:
            raise OperationValidationError(
                "result_exists_mismatch", "a delete leaves the path absent", op_id=op_id
            )
        if operation.content is not None:
            raise OperationValidationError(
                "content_not_allowed", "a delete stages no content", op_id=op_id
            )
        return

    if not operation.result.exists:
        raise OperationValidationError(
            "result_exists_mismatch",
            f"{operation.type.value} must state the content it leaves behind",
            op_id=op_id,
        )

    if operation.type in (OperationType.CREATE_FILE, OperationType.MODIFY_FILE):
        _validate_content(operation)
        return
    # RENAME_FILE moves bytes; the result hash must be the base hash.
    if operation.content is not None:
        raise OperationValidationError(
            "content_not_allowed", "a rename stages no content", op_id=op_id
        )
    if operation.result.sha256 != operation.base.sha256:
        raise OperationValidationError(
            "rename_content_changed",
            "a rename cannot also change the bytes",
            op_id=op_id,
        )


def _validate_content(operation: Operation) -> None:
    op_id = operation.op_id
    content = operation.content
    if content is None:
        raise OperationValidationError("content_required", "no staged content", op_id=op_id)
    if not content.staged_blob:
        raise OperationValidationError(
            "blob_required", "no pre-image/result blob path", op_id=op_id
        )
    if not _SHA256_RE.fullmatch(content.sha256):
        raise OperationValidationError(
            "blob_digest_invalid", "staged content has no sha256", op_id=op_id
        )
    if content.sha256 != operation.result.sha256:
        raise OperationValidationError(
            "blob_result_mismatch",
            "the staged blob and the expected result disagree",
            op_id=op_id,
        )
    if content.size != operation.result.size:
        raise OperationValidationError(
            "blob_result_mismatch",
            f"blob size {content.size} is not the expected {operation.result.size}",
            op_id=op_id,
        )
    if content.size > DEFAULT_MAX_CONTENT_BYTES:
        raise OperationValidationError(
            "content_too_large",
            f"{content.size} bytes exceeds the {DEFAULT_MAX_CONTENT_BYTES} byte per-file cap",
            op_id=op_id,
        )
    if content.binary and content.hunks:
        # A binary blob has no line structure; hunks over it would be a review
        # surface lying about what it showed.
        raise OperationValidationError(
            "hunks_on_binary", "binary content carries no hunks", op_id=op_id
        )


def _validate_command_structure(operation: Operation) -> None:
    op_id = operation.op_id
    command = operation.command
    if command is None:
        raise OperationValidationError("command_required", "no command specification", op_id=op_id)
    if not command.argv or not all(argument.strip() for argument in command.argv):
        raise OperationValidationError("argv_empty", "the command has no executable", op_id=op_id)
    if command.timeout_seconds <= 0:
        raise OperationValidationError(
            "timeout_required", "commands need a positive timeout", op_id=op_id
        )
    if not command.expected_exit_codes:
        raise OperationValidationError(
            "expected_exit_codes_required", "an unbounded success set is not a check", op_id=op_id
        )
    if operation.path or operation.destination_path:
        raise OperationValidationError(
            "path_not_allowed", "a command operation declares no file path", op_id=op_id
        )
    if operation.base.exists or operation.result.exists:
        raise OperationValidationError(
            "fingerprint_not_allowed", "a command operation changes no file", op_id=op_id
        )
    for argument in command.argv:
        if any(marker in argument for marker in (";", "&&", "||", "|", ">", "<", "`", "$(", "\n")):
            # shell=False already prevents these, but a proposal that contains one
            # was written by something expecting a shell, and that intent must be
            # refused in review rather than silently mangled at run time.
            raise OperationValidationError(
                "command_metacharacters", f"{argument!r} looks like shell syntax", op_id=op_id
            )
    if command.cwd not in ("", "."):
        check_path_syntax(command.cwd, op_id=op_id, what="command cwd")


def _check_cross_operation_collisions(
    operations: Sequence[Operation],
    *,
    case_insensitive: bool,
) -> None:
    """Two operations must not write one path, and no rename may chain (§8.1, §14.1).

    On Windows the comparison is case-insensitive, because the filesystem is:
    ``Readme.md`` and ``README.md`` are one file and the second write silently
    destroys the first. Cross-root renames cannot be expressed at all -- both ends
    of a rename are resolved under the operation's single ``root_id``.
    """

    def key(path: str) -> str:
        # Normalise first: "src\a.py" and "src/a.py" are one file, and a collision
        # check that compares spellings would miss it.
        unified = normalize_path(path)
        return unified.casefold() if case_insensitive else unified

    writers: dict[tuple[str, str], str] = {}
    sources: dict[tuple[str, str], str] = {}
    for operation in operations:
        if not operation.is_file_operation:
            continue
        root_path = (operation.root_id, key(operation.path))
        target = (operation.root_id, key(operation.target_path))

        if target in writers:
            raise OperationValidationError(
                "duplicate_target",
                f"{operation.target_path} is also written by {writers[target]}",
                op_id=operation.op_id,
            )
        writers[target] = operation.op_id
        sources[root_path] = operation.op_id

    for operation in operations:
        if operation.type is not OperationType.RENAME_FILE:
            continue
        destination = (operation.root_id, key(operation.destination_path))
        source = (operation.root_id, key(operation.path))
        # The order of these two checks is the message the user gets. A swap is also
        # a chain, so testing the general case first would report every swap as a
        # chained rename and leave the specific refusal unreachable.
        partner = writers.get(source, "")
        if partner and partner == sources.get(destination) and partner != operation.op_id:
            raise OperationValidationError(
                "rename_swap",
                f"{operation.path} and {operation.destination_path} would swap names",
                op_id=operation.op_id,
            )
        if partner and partner != operation.op_id:
            # A rename whose source something else produces cannot be checked
            # before commit, and an undo of it cannot be ordered. §8.5 refuses them.
            raise OperationValidationError(
                "chained_rename",
                f"source {operation.path} is also written by {partner}",
                op_id=operation.op_id,
            )


def validate_operations(
    operations: Sequence[Operation],
    *,
    resolver: WorkspaceResolver | None = None,
    scope: Sequence[str] = (),
    case_insensitive_paths: bool | None = None,
    allowed_types: Iterable[OperationType] | None = None,
) -> tuple[Operation, ...]:
    """Validate a whole transaction before anything is written, and return the ops.

    Ordering, identity, per-type semantics, path syntax, scope and cross-operation
    collisions are all checked here rather than during apply, because a failure
    found after the first write is not a refusal, it is a partial change someone
    has to clean up.
    """

    if not operations:
        raise OperationValidationError(
            "no_operations", "a ChangeSet with no operations does nothing"
        )

    permitted = set(allowed_types) if allowed_types is not None else set(OperationType)
    unknown = [item.op_id for item in operations if item.type not in permitted]
    if unknown:
        raise OperationValidationError(
            "operation_type_unsupported",
            f"these operations are not authorised here: {unknown}",
        )

    ids = [item.op_id for item in operations]
    if len(set(ids)) != len(ids):
        raise OperationValidationError("op_id_duplicate", "operation ids must be unique")
    # Order uniqueness is not checked separately, and cannot be: an id must equal
    # its order, so two operations with one order either share an id (refused
    # above) or break that rule in _validate_structure.
    orders = [item.order for item in operations]
    if sorted(orders) != orders:
        raise OperationValidationError(
            "order_not_ascending", "operations must be listed in the order they will run"
        )

    # Windows folds case in the filesystem itself, so the collision check has to.
    insensitive = (os.name == "nt") if case_insensitive_paths is None else case_insensitive_paths

    for operation in operations:
        _validate_structure(operation)
        if operation.is_file_operation:
            _check_scope(normalize_path(operation.path), scope, op_id=operation.op_id)
            if operation.destination_path:
                _check_scope(
                    normalize_path(operation.destination_path), scope, op_id=operation.op_id
                )
            if resolver is not None:
                _check_containment(operation, resolver)

    _check_cross_operation_collisions(operations, case_insensitive=insensitive)
    return tuple(operations)


def _check_containment(operation: Operation, resolver: WorkspaceResolver) -> None:
    """Resolve every path through the workspace, at validation as at commit (§8.1).

    The resolver follows reparse points. A symlink that leaves the root therefore
    fails here instead of at the write, where the failure would arrive after a
    pre-image had already been staged for a file the user never approved touching.
    """

    targets = [operation.path]
    if operation.destination_path:
        targets.append(operation.destination_path)
    for target in targets:
        try:
            resolver.resolve_for(operation.root_id, target)
        except Exception as exc:  # containment owns the vocabulary of refusal
            raise OperationValidationError(
                "path_escape",
                f"{target} is not resolvable inside the workspace: {exc}",
                op_id=operation.op_id,
            ) from exc
