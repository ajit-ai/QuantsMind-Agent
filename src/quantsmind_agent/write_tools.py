"""The v0.2 mutating filesystem tools.

``write_file``
    Create or replace a text file. Always asks first; reports whether it
    created or replaced.
``edit_file``
    Apply one deterministic edit (:mod:`quantsmind_agent.edit_ops`) with an
    explicit precondition, so a stale request fails instead of destroying work.
``delete_file``
    Remove a single file. Asks first, always -- and keeps asking even when the
    user switched ordinary writes to auto-approve.

All three share one containment rule (:meth:`Workspace.resolve`) and one
permission requirement (:attr:`Permission.WRITE_WORKSPACE`). The tools never
check permissions themselves: the registry does that before ``run`` is called,
so a mis-annotated tool cannot bypass policy and a denied call never reaches the
code below.

Security failures raise a :class:`~quantsmind_agent.errors.QuantsMindError`
subclass rather than returning a message, which keeps them out of the "expected
tool failure" bucket and makes them visible in the log and the audit trail.
"""

from __future__ import annotations

from typing import Any

from quantsmind_agent.edit_ops import EditOperation, apply_edit, detect_eol
from quantsmind_agent.errors import InvalidPathError, StaleContentError
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.permissions import Permission
from quantsmind_agent.tools import ToolResult, ToolSpec
from quantsmind_agent.workspace import FileFingerprint, Workspace

__all__ = [
    "DeleteFileTool",
    "EditFileTool",
    "WriteFileTool",
    "build_mutating_tools",
]

logger = get_logger(__name__)


def _as_int(arguments: dict[str, Any], key: str, default: int) -> int | None:
    """Read an optional integer argument, ignoring bools and bad types."""

    if key not in arguments or arguments[key] is None:
        return None
    value = arguments[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _check_expected_hash(
    path: str,
    before: FileFingerprint,
    expected: Any,
) -> None:
    """Refuse to modify a file that changed since the model read it."""

    if not isinstance(expected, str) or not expected:
        return
    actual = before.sha256 if before.exists else ""
    if actual != expected:
        raise StaleContentError(
            path,
            "the file changed since you last read it",
            details={"expected_sha256": expected, "actual_sha256": actual or None},
        )


class WriteFileTool:
    """Create or replace a text file inside the workspace."""

    def __init__(self, workspace: Workspace, *, max_bytes: int = 2_000_000) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self.spec = ToolSpec(
            name="write_file",
            description=(
                "Create or replace a text file with the given content. The whole file "
                "is written; the path must be workspace-relative. Prefer edit_file when "
                "changing part of an existing file, because edit_file refuses to "
                "overwrite content that changed since you read it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path of the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full new content of the file.",
                    },
                    "create_parents": {
                        "type": "boolean",
                        "description": "Create missing parent folders. Default false.",
                        "default": False,
                    },
                    "expected_sha256": {
                        "type": "string",
                        "description": (
                            "Optional fingerprint of the file as you last read it. The "
                            "write is refused if the file changed since then."
                        ),
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            permission=Permission.WRITE_WORKSPACE,
            summary_template="write {path}",
            read_only=False,
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        path = str(arguments.get("path") or "")
        content = arguments.get("content")

        if not path:
            raise InvalidPathError(
                "write_file was called without a path",
                user_message="No file path was provided.",
                details={"reason": "missing path"},
            )
        if not isinstance(content, str):
            raise InvalidPathError(
                "write_file was called without string content",
                user_message="The new file content must be a string.",
                details={"reason": "missing content", "path": path},
            )
        if len(content.encode("utf-8", errors="ignore")) > self._max_bytes:
            raise InvalidPathError(
                f"Refusing to write more than {self._max_bytes} bytes to {path!r}",
                user_message="That file is too large to write in one step.",
                details={"reason": "content too large", "path": path},
            )

        before = self._workspace.fingerprint(path)
        _check_expected_hash(path, before, arguments.get("expected_sha256"))
        after = self._workspace.write_text(
            path,
            content,
            create_parents=bool(arguments.get("create_parents", False)),
        )
        return ToolResult.success(
            self.spec.name,
            f"{'replaced' if before.exists else 'created'} {after.path} ({after.size} bytes)",
            {
                "path": after.path,
                "created": not before.exists,
                "replaced": before.exists,
                "bytes": after.size,
                "lines": after.line_count,
                "eol": "crlf" if "\r\n" in content else "lf",
                "sha256": after.sha256,
                "previous_sha256": before.sha256 or None,
            },
        )


class EditFileTool:
    """Apply one deterministic edit to an existing workspace file."""

    def __init__(self, workspace: Workspace, *, max_bytes: int = 2_000_000) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self.spec = ToolSpec(
            name="edit_file",
            description=(
                "Change part of an existing text file. Every edit states what must "
                "already be in the file, so a mismatch fails instead of overwriting "
                "someone else's change. Operations: replace, replace_all, "
                "insert_before, insert_after, replace_lines, append, prepend. Pass "
                "the file's sha256 as expected_sha256 to be refused if it changed in "
                "the meantime."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path of the file to edit.",
                    },
                    "operation": {
                        "type": "string",
                        "description": (
                            "One of: replace, replace_all, insert_before, insert_after, "
                            "replace_lines, append, prepend."
                        ),
                        "enum": [member.value for member in EditOperation],
                    },
                    "old_text": {
                        "type": "string",
                        "description": (
                            "Exact existing text to anchor the edit. Required for replace, "
                            "replace_all, insert_before and insert_after."
                        ),
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement or inserted text.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to replace, 1-based (replace_lines only).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to replace, inclusive (replace_lines only).",
                    },
                    "expected_occurrences": {
                        "type": "integer",
                        "description": (
                            "How many times old_text must occur. The edit is refused if the "
                            "count differs, which catches a file that changed under you."
                        ),
                    },
                    "expected_sha256": {
                        "type": "string",
                        "description": (
                            "Fingerprint of the file as you last read it. The edit is "
                            "refused if the file changed since then."
                        ),
                    },
                },
                "required": ["path", "operation"],
                "additionalProperties": False,
            },
            permission=Permission.WRITE_WORKSPACE,
            summary_template="edit {path} ({operation})",
            read_only=False,
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        path = str(arguments.get("path") or "")
        operation = str(arguments.get("operation") or "")
        if not path:
            raise InvalidPathError(
                "edit_file was called without a path",
                user_message="No file path was provided.",
                details={"reason": "missing path"},
            )

        before = self._workspace.fingerprint(path)
        if not before.exists:
            raise InvalidPathError(
                f"Cannot edit {path!r}: it does not exist",
                user_message=(
                    f"'{path}' does not exist. Create it with write_file first, "
                    "or pick a path that exists."
                ),
                details={"path": path, "reason": "file does not exist"},
            )

        _check_expected_hash(path, before, arguments.get("expected_sha256"))

        current = self._workspace.read_text(path, preserve_newlines=True)
        outcome = apply_edit(
            current,
            path,
            operation=operation,
            old_text=str(arguments.get("old_text") or ""),
            new_text=str(arguments.get("new_text") or ""),
            start_line=_as_int(arguments, "start_line", 1),
            end_line=_as_int(arguments, "end_line", 1),
            expected_occurrences=_as_int(arguments, "expected_occurrences", 0),
            occurrence=_as_int(arguments, "occurrence", 1) or 1,
        )
        after = self._workspace.write_text(path, outcome.content)
        logger.info("edit_file applied %s to %s", outcome.description, path)
        return ToolResult.success(
            self.spec.name,
            f"edited {after.path}: {outcome.description}",
            {
                "path": after.path,
                "operation": operation,
                "description": outcome.description,
                "lines_before": before.line_count,
                "lines_after": after.line_count,
                "bytes_before": before.size,
                "bytes_after": after.size,
                "eol": "crlf" if outcome.eol == "\r\n" else "lf",
                "sha256_before": before.sha256,
                "sha256_after": after.sha256,
            },
        )


class DeleteFileTool:
    """Delete a single file from the workspace, after an explicit approval."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="delete_file",
            description=(
                "Delete one file from the workspace. This is irreversible from the agent's "
                "point of view, so it always needs explicit approval, every time. "
                "Folders cannot be deleted: remove their files one at a time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path of the file to delete.",
                    },
                    "expected_sha256": {
                        "type": "string",
                        "description": (
                            "Fingerprint of the file as you last read it. The delete is "
                            "refused if the file changed since then."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            permission=Permission.WRITE_WORKSPACE,
            summary_template="delete {path}",
            read_only=False,
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        path = str(arguments.get("path") or "")
        if not path:
            raise InvalidPathError(
                "delete_file was called without a path",
                user_message="No file path was provided.",
                details={"reason": "missing path"},
            )
        before = self._workspace.fingerprint(path)
        _check_expected_hash(path, before, arguments.get("expected_sha256"))
        removed = self._workspace.delete_file(path)
        return ToolResult.success(
            self.spec.name,
            f"deleted {removed.path} ({removed.size} bytes)",
            {
                "path": removed.path,
                "bytes": removed.size,
                "lines": removed.line_count,
                "sha256": removed.sha256,
                "workspace": str(self._workspace.root),
            },
        )


def build_mutating_tools(
    workspace: Workspace,
    *,
    max_file_bytes: int = 2_000_000,
) -> list[Any]:
    """Instantiate the v0.2 write/edit/delete tools bound to ``workspace``."""

    return [
        WriteFileTool(workspace, max_bytes=max_file_bytes),
        EditFileTool(workspace, max_bytes=max_file_bytes),
        DeleteFileTool(workspace),
    ]


def describe_eol(text: str) -> str:
    """Human label for the line ending of ``text`` (used by tests and the GUI)."""

    return "crlf" if detect_eol(text) == "\r\n" else "lf"
