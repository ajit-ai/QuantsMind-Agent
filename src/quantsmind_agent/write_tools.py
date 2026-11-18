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
``undo_file``
    Put one file back the way it was before the newest reversible change to it.
    Asks first, always, like ``delete_file``: an undo is still a mutation, and
    for a file the agent created it is a deletion.

All four share one containment rule (:meth:`Workspace.resolve`) and one
permission requirement (:attr:`Permission.WRITE_WORKSPACE`). The tools never
check permissions themselves: the registry does that before ``run`` is called,
so a mis-annotated tool cannot bypass policy and a denied call never reaches the
code below.

Security failures raise a :class:`~quantsmind_agent.errors.QuantsMindError`
subclass rather than returning a message, which keeps them out of the "expected
tool failure" bucket and makes them visible in the log and the audit trail.

Reversibility
-------------
``write_file`` and ``edit_file`` record a pre-image in the session's
:class:`~quantsmind_agent.revisions.RevisionJournal` *after* the write succeeded,
so a failed write leaves nothing to undo. ``delete_file`` is deliberately absent
from the journal: it is the one irreversible operation, its approval dialog says
so, and journalling it would quietly weaken a promise the product makes to the
user.

Structured diffs
----------------
``write_file`` and ``edit_file`` also report *what* changed, as a bounded unified
diff plus line counts, in ``ToolResult.data`` (see
:mod:`quantsmind_agent.diffs`). The text comes from the same pre-image the undo
journal captures, so no extra read of the file is needed. ``delete_file`` has no
diff: the file it changed no longer has content to compare against.
"""

from __future__ import annotations

from typing import Any

from quantsmind_agent.code_imports import importers_of
from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.diffs import revision_diff
from quantsmind_agent.edit_ops import EditOperation, apply_edit, detect_eol
from quantsmind_agent.errors import (
    InvalidPathError,
    StaleContentError,
    ToolFailedError,
)
from quantsmind_agent.project_intel import ProjectIntelligence
from quantsmind_agent.revisions import FileRevision, RevisionJournal, capture_revision
from quantsmind_agent.runtime.tools import ToolResult, ToolSpec
from quantsmind_agent.security.audit import (
    AuditEventType,
    AuditPersistenceError,
    SecurityAuditLog,
)
from quantsmind_agent.security.permissions import Permission
from quantsmind_agent.services.workspace.containment import FileFingerprint, Workspace

__all__ = [
    "DeleteFileTool",
    "EditFileTool",
    "UndoFileTool",
    "WriteFileTool",
    "build_mutating_tools",
    "build_undo_tool",
]

logger = get_logger(__name__)


class _PreImage:
    """The bytes a write is about to replace, captured for the undo journal.

    ``captured`` is ``False`` when the pre-image exists but could not be read
    (too large, unreadable). That must never block the write the user approved,
    so the tool proceeds and simply offers nothing to undo -- and says so in the
    result, rather than leaving the model to discover it later.
    """

    __slots__ = ("bytes", "captured")

    def __init__(self, data: bytes | None, *, captured: bool = True) -> None:
        self.bytes = data
        self.captured = captured


def _capture_pre_image(workspace: Workspace, path: str, existed: bool, limit: int) -> _PreImage:
    """Read the exact bytes of ``path`` before it is overwritten."""

    if not existed:
        return _PreImage(None)
    try:
        return _PreImage(workspace.read_bytes(path, max_bytes=limit))
    except InvalidPathError as exc:
        logger.info("Not journalling the change to %s: %s", path, exc.message)
        return _PreImage(None, captured=False)


def _affected_files(
    workspace: Workspace,
    path: str,
    *,
    limit: int = 50,
    max_files: int = 400,
) -> dict[str, object]:
    """Which files import the one just changed, reported to the model.

    A cross-file change only stays correct if the caller notices the callers.
    Surfacing them in the edit result means the agent does not have to remember
    to ask, and it is skipped entirely for a file that did not exist a moment ago
    -- nothing could have been importing it.
    """

    importers, truncated = importers_of(workspace, path, max_files=max_files)
    return {"dependents": importers[:limit], "dependents_truncated": truncated}


def _record_revision(
    journal: RevisionJournal | None,
    path: str,
    tool: str,
    pre_image: _PreImage,
    after: FileFingerprint,
) -> bool:
    """Add a write to the journal, now that it has actually happened.

    Called only after :meth:`Workspace.write_text` returned, which is what makes
    "a failed write creates no revision" true by construction rather than by
    remembering to clean up.

    Returning ``False`` is not an error: it means this change cannot be undone
    (no journal wired up, or a pre-image too large to hold), and the caller
    reports that honestly instead of implying a safety net that is not there.
    """

    if journal is None or not pre_image.captured:
        return False
    journal.record(
        capture_revision(
            path=path,
            tool=tool,
            previous_bytes=pre_image.bytes,
            result_sha256=after.sha256,
        )
    )
    return True


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


def _record_mutation(
    audit: SecurityAuditLog | None,
    *,
    tool: str,
    path: str,
    outcome: str,
    before_sha: str,
    after_sha: str,
    size: int,
    changeset_id: str = "",
) -> None:
    """Say on disk that a workspace file changed.

    This runs after the bytes are already there, so a failed write cannot undo
    the mutation and must not pretend to: the record is fsync-ed, and if even
    that fails the loss is logged and the audit surfaces it as degraded rather
    than turning a completed change into an error.
    """

    if audit is None:
        return
    try:
        audit.record(
            AuditEventType.FILE_MUTATED,
            decision=outcome,
            outcome=outcome,
            reason_code=f"{tool}_applied",
            subject=path,
            detail=f"sha256 {before_sha or 'absent'} -> {after_sha or 'absent'}, {size} bytes",
            changeset_id=changeset_id,
            durable=True,
        )
    except AuditPersistenceError as exc:
        logger.warning("mutation of %s was not audited: %s", path, exc)


class WriteFileTool:
    """Create or replace a text file inside the workspace."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        max_bytes: int = 2_000_000,
        journal: RevisionJournal | None = None,
        intelligence: ProjectIntelligence | None = None,
        audit: SecurityAuditLog | None = None,
    ) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self._journal = journal
        #: Told about the write so a cached symbol or import record for this file
        #: is never answered after the file has changed.
        self._intelligence = intelligence
        self._audit = audit
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
        pre_image = _capture_pre_image(self._workspace, before.path, before.exists, self._max_bytes)
        after = self._workspace.write_text(
            path,
            content,
            create_parents=bool(arguments.get("create_parents", False)),
        )
        undoable = _record_revision(self._journal, before.path, "write_file", pre_image, after)
        _record_mutation(
            self._audit,
            tool="write_file",
            path=after.path,
            outcome="created" if not before.exists else "replaced",
            before_sha=before.sha256 or "",
            after_sha=after.sha256,
            size=after.size,
        )
        # A created file can add a package or an entry point; a replaced one cannot
        # change the project layout, only its own contents.
        if self._intelligence is not None:
            if before.exists:
                self._intelligence.note_written(after.path)
            else:
                self._intelligence.note_created(after.path)
        affected = {} if not before.exists else _affected_files(self._workspace, after.path)
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
                "undoable": undoable,
                **affected,
                **revision_diff(
                    pre_image.bytes,
                    content,
                    path=after.path,
                    previous_available=pre_image.captured,
                ).to_payload(),
            },
        )


class EditFileTool:
    """Apply one deterministic edit to an existing workspace file."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        max_bytes: int = 2_000_000,
        journal: RevisionJournal | None = None,
        intelligence: ProjectIntelligence | None = None,
        audit: SecurityAuditLog | None = None,
    ) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self._journal = journal
        self._intelligence = intelligence
        self._audit = audit
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

        pre_image = _capture_pre_image(self._workspace, before.path, before.exists, self._max_bytes)
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
        undoable = _record_revision(self._journal, before.path, "edit_file", pre_image, after)
        _record_mutation(
            self._audit,
            tool="edit_file",
            path=after.path,
            outcome="edited",
            before_sha=before.sha256 or "",
            after_sha=after.sha256,
            size=after.size,
        )
        if self._intelligence is not None:
            self._intelligence.note_written(after.path)
        affected = _affected_files(self._workspace, after.path)
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
                "undoable": undoable,
                **affected,
                **revision_diff(
                    pre_image.bytes,
                    outcome.content,
                    path=after.path,
                    previous_available=pre_image.captured,
                ).to_payload(),
            },
        )


class DeleteFileTool:
    """Delete a single file from the workspace, after an explicit approval."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        intelligence: ProjectIntelligence | None = None,
        audit: SecurityAuditLog | None = None,
    ) -> None:
        self._workspace = workspace
        self._intelligence = intelligence
        self._audit = audit
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
        _record_mutation(
            self._audit,
            tool="delete_file",
            path=removed.path,
            outcome="deleted",
            before_sha=removed.sha256 or "",
            after_sha="",
            size=0,
        )
        if self._intelligence is not None:
            # Both directions matter: the file's own record goes, and so does every
            # edge that pointed at it, so no caller is reported for a module that is
            # no longer there.
            self._intelligence.note_deleted(removed.path)
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
    journal: RevisionJournal | None = None,
    intelligence: ProjectIntelligence | None = None,
    audit: SecurityAuditLog | None = None,
) -> list[Any]:
    """Instantiate the v0.2 write/edit/delete tools bound to ``workspace``.

    ``journal`` is optional so the tools stay usable on their own (tests, the
    acceptance harness, anyone embedding the core); without one, writes simply
    report ``undoable: False`` instead of pretending to be reversible.
    """

    return [
        WriteFileTool(
            workspace,
            max_bytes=max_file_bytes,
            journal=journal,
            intelligence=intelligence,
            audit=audit,
        ),
        EditFileTool(
            workspace,
            max_bytes=max_file_bytes,
            journal=journal,
            intelligence=intelligence,
            audit=audit,
        ),
        DeleteFileTool(workspace, intelligence=intelligence, audit=audit),
    ]


class UndoFileTool:
    """Restore one file to its state before the newest reversible change.

    The tool is deliberately narrow. It undoes a single path, it undoes only one
    step per call, and it undoes only changes the agent itself made and recorded.
    It will not reconstruct content it never saw, cannot undo a
    ``delete_file`` -- the one operation the product calls irreversible -- and
    refuses rather than overwriting a file that changed outside the journal.
    """

    def __init__(
        self,
        workspace: Workspace,
        journal: RevisionJournal,
        *,
        intelligence: ProjectIntelligence | None = None,
        audit: SecurityAuditLog | None = None,
    ) -> None:
        self._workspace = workspace
        self._journal = journal
        #: An undo changes file contents just as a write does, so the project map
        #: has to hear about it too -- otherwise the agent undoes an edit and keeps
        #: answering questions from the version it just discarded.
        self._intelligence = intelligence
        self._audit = audit
        self.spec = ToolSpec(
            name="undo_file",
            description=(
                "Undo the most recent change the agent made to one file, putting it "
                "back exactly as it was. One call undoes one step, so repeat it to walk "
                "further back through the same file's history. Deleting a file with "
                "delete_file cannot be undone, and files you changed outside the agent "
                "are never touched. Always asks first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path of the file to undo.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            permission=Permission.WRITE_WORKSPACE,
            summary_template="undo {path}",
            read_only=False,
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        path = str(arguments.get("path") or "")
        if not path:
            raise InvalidPathError(
                "undo_file was called without a path",
                user_message="No file path was provided.",
                details={"reason": "missing path"},
            )

        revision = self._journal.peek(path)
        if revision is None:
            known = self._journal.paths
            hint = (
                f" Changes the agent can still undo: {', '.join(known)}."
                if known
                else " No reversible change has been made in this session."
            )
            return ToolResult.failure(
                self.spec.name,
                self.spec.summarize(arguments),
                f"There is nothing to undo for '{path}' in this conversation.{hint}",
            )

        target = self._workspace.fingerprint(path)
        self._assert_unchanged(target, revision)
        rel = target.path

        # Restore first, consume second: if the filesystem call raises, the
        # revision is still on the stack and the user can simply retry.
        if revision.previous_bytes is None:
            self._workspace.delete_file(rel)
            restored = "removed"
            restored_sha256: str | None = None
            restored_bytes: int | None = 0
        else:
            fingerprint = self._workspace.write_bytes(rel, revision.previous_bytes)
            restored = "restored"
            restored_sha256 = fingerprint.sha256
            restored_bytes = fingerprint.size
            if fingerprint.sha256 != revision.previous_sha256:
                # write_bytes reports a digest of what it put on disk; if that is
                # not the pre-image the disk is not telling us the truth, so do
                # not claim success and do not forget the revision.
                raise ToolFailedError(
                    f"Undo of {rel!r} did not reproduce the original content",
                    user_message=(
                        f"'{rel}' was written but its content did not verify, so the "
                        "change was not marked as undone. Please check the file."
                    ),
                    details={"path": rel, "expected_sha256": revision.previous_sha256},
                )

        if self._intelligence is not None:
            if restored == "removed":
                self._intelligence.note_deleted(rel)
            else:
                self._intelligence.note_written(rel)

        self._journal.consume(path, revision)
        _record_mutation(
            self._audit,
            tool="undo_file",
            path=rel,
            outcome=f"undid_{revision.tool}",
            before_sha=revision.result_sha256 or "",
            after_sha=restored_sha256 or "",
            size=restored_bytes or 0,
        )
        logger.info("undo_file reverted %s (%s) recorded by %s", rel, restored, revision.tool)
        return ToolResult.success(
            self.spec.name,
            f"undid the last {revision.tool} to {rel} ({restored})",
            {
                "path": rel,
                "undone_tool": revision.tool,
                "action": restored,
                "was_creation": revision.is_creation,
                "bytes": restored_bytes,
                "restored_sha256": restored_sha256,
                "sha256_before_undo": revision.result_sha256,
                "remaining_undo_steps": self._journal.depth(path),
            },
        )

    def _assert_unchanged(self, current: FileFingerprint, revision: FileRevision) -> None:
        """Refuse to overwrite a file that no longer matches the recorded change.

        The recorded digest is the file's identity right after the write this
        undo would reverse. If it no longer matches, something outside the
        journal touched the file, and restoring the pre-image would throw that
        work away without the user ever seeing it.
        """

        if not current.exists:
            raise StaleContentError(
                current.path,
                "the file was removed after the change that undo would revert",
                details={
                    "expected_sha256": revision.result_sha256,
                    "actual_sha256": None,
                    "recorded_tool": revision.tool,
                },
            )
        if current.sha256 != revision.result_sha256:
            raise StaleContentError(
                current.path,
                "the file changed after the change that undo would revert",
                details={
                    "expected_sha256": revision.result_sha256,
                    "actual_sha256": current.sha256,
                    "recorded_tool": revision.tool,
                },
            )


def build_undo_tool(
    workspace: Workspace,
    journal: RevisionJournal,
    *,
    intelligence: ProjectIntelligence | None = None,
    audit: SecurityAuditLog | None = None,
) -> UndoFileTool:
    """Instantiate the undo tool bound to ``workspace`` and ``journal``."""

    return UndoFileTool(workspace, journal, intelligence=intelligence, audit=audit)


def describe_eol(text: str) -> str:
    """Human label for the line ending of ``text`` (used by tests and the GUI)."""

    return "crlf" if detect_eol(text) == "\r\n" else "lf"
