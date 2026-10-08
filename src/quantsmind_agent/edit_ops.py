"""Deterministic edit operations.

v0.2 deliberately does **not** give the model a "rewrite this file however you
like" primitive. Every edit is expressed as *what should change* plus *what must
already be there*, so the operation either applies exactly as intended or fails
without touching the file. That is what makes a coding agent safe to point at a
real repository: the model cannot destroy work it did not understand, because a
mismatch is an error rather than a silent overwrite.

Two independent staleness guards, either of which aborts the edit:

``expected_sha256`` (checked by the tool layer, not here)
    The fingerprint observed when the model read the file. Any write in between
    -- by the user, by a formatter hook, or by a previous agent turn -- makes the
    hash differ and the edit is refused.
``expected_occurrences`` (checked here)
    The edit's own precondition: ``old_text`` must appear exactly as often as the
    model saw it, and the target line range must still exist.

Newlines are handled explicitly. Comparisons happen on newline-normalised text
and the file's dominant line ending is detected and re-applied on the way out,
so editing one line of a CRLF file on Windows does not silently rewrite the
whole file with LF endings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from quantsmind_agent.errors import EditConflictError, StaleContentError

__all__ = [
    "EditOperation",
    "EditOutcome",
    "apply_edit",
    "detect_eol",
    "normalize_newlines",
]


class EditOperation(StrEnum):
    """The deterministic edits the model may request."""

    REPLACE = "replace"
    """Replace one occurrence of ``old_text`` with ``new_text``."""

    REPLACE_ALL = "replace_all"
    """Replace every occurrence; ``expected_occurrences`` may pin the count."""

    INSERT_BEFORE = "insert_before"
    """Insert ``new_text`` immediately before the line matching ``old_text``."""

    INSERT_AFTER = "insert_after"
    """Insert ``new_text`` immediately after the line matching ``old_text``."""

    REPLACE_LINES = "replace_lines"
    """Replace the inclusive 1-based line range ``start_line``..``end_line``."""

    APPEND = "append"
    """Add ``new_text`` at the end of the file."""

    PREPEND = "prepend"
    """Add ``new_text`` at the start of the file."""


@dataclass(frozen=True, slots=True)
class EditOutcome:
    """Result of a successful edit."""

    content: str
    description: str
    occurrences: int = 1
    eol: str = "\n"


def detect_eol(text: str) -> str:
    """Return the dominant line ending of ``text`` (defaults to ``\\n``)."""

    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def normalize_newlines(text: str) -> str:
    """Collapse CRLF and lone CR to LF so comparisons are platform-neutral."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_bodies(text: str) -> tuple[list[str], bool]:
    """Split ``text`` into line bodies (no terminators) plus a trailing flag."""

    normalized = normalize_newlines(text)
    trailing = normalized.endswith("\n")
    if trailing:
        normalized = normalized[:-1]
    if normalized == "":
        return [], trailing
    return normalized.split("\n"), trailing


def _rebuild(lines: list[str], eol: str, trailing: bool) -> str:
    """Join line bodies with ``eol``, restoring the trailing newline."""

    if not lines:
        return ""
    return eol.join(lines) + (eol if trailing else "")


def _require(value: str, field: str, path: str) -> str:
    if not value:
        raise EditConflictError(path, f"{field} must not be empty for this operation")
    return normalize_newlines(value)


def _count(haystack: str, needle: str) -> int:
    total = 0
    position = haystack.find(needle)
    while position != -1:
        total += 1
        position = haystack.find(needle, position + len(needle))
    return total


def _nth(haystack: str, needle: str, occurrence: int) -> int:
    position = -1
    for _ in range(max(1, occurrence)):
        position = haystack.find(needle, position + 1)
        if position == -1:
            return -1
    return position


def _matching_lines(lines: list[str], needle: str) -> list[int]:
    """Indices of lines equal to ``needle``, else lines merely containing it."""

    exact = [index for index, line in enumerate(lines) if line == needle]
    if exact:
        return exact
    return [index for index, line in enumerate(lines) if needle in line]


def apply_edit(
    content: str,
    path: str,
    *,
    operation: EditOperation | str,
    old_text: str = "",
    new_text: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    expected_occurrences: int | None = None,
    occurrence: int = 1,
) -> EditOutcome:
    """Apply one deterministic edit to ``content``.

    ``content`` must be the file's current text, read with newlines preserved.
    This function is pure: it returns the new text and never touches the disk,
    so the tool layer can verify the fingerprint immediately before writing.

    Raises
    ------
    EditConflictError
        The request is malformed (missing field, bad line range, unknown
        operation, occurrence out of range).
    StaleContentError
        ``old_text`` is absent, appears a different number of times than
        ``expected_occurrences``, or the requested line range moved.
    """

    try:
        op = EditOperation(operation)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in EditOperation)
        raise EditConflictError(
            path,
            f"unknown operation {operation!r}; use one of: {allowed}",
            details={"allowed": allowed},
        ) from exc

    eol = detect_eol(content)
    lines, trailing = _split_bodies(content)

    if op is EditOperation.REPLACE_LINES:
        return _replace_lines(lines, trailing, eol, start_line, end_line, new_text, path)
    if op in (EditOperation.APPEND, EditOperation.PREPEND):
        return _add_block(lines, trailing, eol, op, new_text, path)
    if op in (EditOperation.INSERT_BEFORE, EditOperation.INSERT_AFTER):
        return _insert_relative(
            lines, trailing, eol, op, old_text, new_text, path, expected_occurrences
        )
    return _replace_text(
        lines, trailing, eol, op, old_text, new_text, path, expected_occurrences, occurrence
    )


def _replace_lines(
    lines: list[str],
    trailing: bool,
    eol: str,
    start_line: int | None,
    end_line: int | None,
    new_text: str,
    path: str,
) -> EditOutcome:
    if start_line is None or end_line is None:
        raise EditConflictError(path, "replace_lines needs both start_line and end_line")
    if start_line < 1:
        raise EditConflictError(path, f"start_line must be 1 or greater, got {start_line}")
    if end_line < start_line:
        raise EditConflictError(path, f"end_line ({end_line}) is before start_line ({start_line})")
    if not lines:
        raise StaleContentError(path, "the file is empty, so there are no lines to replace")
    if end_line > len(lines):
        raise StaleContentError(
            path,
            f"the file now has {len(lines)} line(s), so line {end_line} does not exist",
            expected_count=end_line,
            details={"actual_line_count": len(lines)},
        )

    replacement = normalize_newlines(new_text)
    removed = end_line - start_line + 1
    updated = [*lines[: start_line - 1], *replacement.split("\n"), *lines[end_line:]]
    return EditOutcome(
        _rebuild(updated, eol, trailing),
        f"replaced line(s) {start_line}-{end_line} ({removed} line(s))",
        occurrences=removed,
        eol=eol,
    )


def _add_block(
    lines: list[str],
    trailing: bool,
    eol: str,
    op: EditOperation,
    new_text: str,
    path: str,
) -> EditOutcome:
    block = _require(new_text, "new_text", path)
    addition = block.split("\n")
    # A block that already ends with a newline must not add a blank line.
    if addition and addition[-1] == "":
        addition = addition[:-1]
    if not addition:
        raise EditConflictError(path, "new_text must not be empty")

    if op is EditOperation.PREPEND:
        updated = [*addition, *lines]
    else:
        updated = [*lines, *addition]
    # Keep a file that had no trailing newline that way, unless we are adding to
    # an empty file, where there is nothing to preserve.
    keep_trailing = trailing or not lines
    verb = "prepended to" if op is EditOperation.PREPEND else "appended to"
    return EditOutcome(
        _rebuild(updated, eol, keep_trailing),
        f"{verb} the file ({len(addition)} line(s) added)",
        occurrences=len(addition),
        eol=eol,
    )


def _insert_relative(
    lines: list[str],
    trailing: bool,
    eol: str,
    op: EditOperation,
    old_text: str,
    new_text: str,
    path: str,
    expected_occurrences: int | None,
) -> EditOutcome:
    needle = _require(old_text, "old_text", path)
    block = _require(new_text, "new_text", path)

    matches = _matching_lines(lines, needle)
    if not matches:
        raise StaleContentError(
            path,
            "no line matches the text you asked to insert around",
            expected_count=expected_occurrences,
            details={"operation": op.value},
        )
    if expected_occurrences is not None and len(matches) != expected_occurrences:
        raise StaleContentError(
            path,
            f"{len(matches)} line(s) match, not {expected_occurrences} as expected",
            expected_count=expected_occurrences,
            details={"operation": op.value, "actual_occurrences": len(matches)},
        )

    anchor = matches[0]
    at = anchor if op is EditOperation.INSERT_BEFORE else anchor + 1
    addition = block.split("\n")
    updated = [*lines[:at], *addition, *lines[at:]]
    where = "before" if op is EditOperation.INSERT_BEFORE else "after"
    return EditOutcome(
        _rebuild(updated, eol, trailing),
        f"inserted {len(addition)} line(s) {where} line {anchor + 1}",
        occurrences=len(addition),
        eol=eol,
    )


def _replace_text(
    lines: list[str],
    trailing: bool,
    eol: str,
    op: EditOperation,
    old_text: str,
    new_text: str,
    path: str,
    expected_occurrences: int | None,
    occurrence: int,
) -> EditOutcome:
    needle = _require(old_text, "old_text", path)
    replacement = normalize_newlines(new_text)
    joined = "\n".join(lines)

    matches = _count(joined, needle)
    if matches == 0:
        raise StaleContentError(
            path,
            "the text you asked to replace is not in the file any more",
            expected_count=expected_occurrences,
            details={"operation": op.value},
        )
    if expected_occurrences is not None and matches != expected_occurrences:
        raise StaleContentError(
            path,
            f"it appears {matches} time(s), not {expected_occurrences} as expected",
            expected_count=expected_occurrences,
            details={"operation": op.value, "actual_occurrences": matches},
        )

    if op is EditOperation.REPLACE_ALL:
        updated = joined.replace(needle, replacement)
        detail = f"replaced all {matches} occurrence(s)"
    else:
        if occurrence < 1 or occurrence > matches:
            raise EditConflictError(
                path,
                f"occurrence {occurrence} was requested but the text appears {matches} time(s)",
                details={"occurrences": matches},
            )
        position = _nth(joined, needle, occurrence)
        updated = joined[:position] + replacement + joined[position + len(needle) :]
        detail = f"replaced occurrence {occurrence} of {matches}"

    updated_lines = updated.split("\n") if updated else []
    return EditOutcome(
        _rebuild(updated_lines, eol, trailing),
        detail,
        occurrences=matches,
        eol=eol,
    )
