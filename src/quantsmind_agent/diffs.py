"""Structured diffs for mutating tool results.

The v0.2 roadmap asks for "structured diffs in ``ToolResult.data``": when the
agent rewrites a file, the result it reads back should say *what changed*, not
only that something changed. This module builds that description and nothing
else -- it never touches the filesystem.

Three properties drive the design:

* **Bounded.** A whole-file rewrite can differ in ten thousand lines. The diff
  is therefore capped in lines and characters, and the payload says so
  explicitly instead of letting a truncated diff look like a complete one. The
  model can always read the file for more; it cannot un-read a megabyte.
* **Comparable.** Line counts describe the *whole* change even when the text is
  truncated, so ``diff_added``/``diff_removed`` stay usable as a summary.
* **Honest about the cases with nothing to compare.** Creating a file has no
  previous content to differ from, and a pre-image too large to read has no
  comparison either. Both are reported by name rather than being rendered as an
  empty diff, which would read as "nothing changed".
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

__all__ = ["MAX_DIFF_CHARS", "MAX_DIFF_LINES", "FileDiff", "revision_diff"]

#: Lines of context around each change, matching the usual ``git diff -U3``.
CONTEXT_LINES = 3

#: Upper bound on the diff text kept in a tool result.
MAX_DIFF_LINES = 200
MAX_DIFF_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class FileDiff:
    """One bounded, unified diff plus the counts that describe the whole change."""

    text: str
    lines_added: int
    lines_removed: int
    truncated: bool
    kind: str

    def to_payload(self) -> dict[str, object]:
        """The keys added to ``ToolResult.data``.

        Every key is always present so a consumer can read them without probing
        for optional fields.
        """

        return {
            "diff": self.text,
            "diff_kind": self.kind,
            "diff_added": self.lines_added,
            "diff_removed": self.lines_removed,
            "diff_truncated": self.truncated,
        }


def revision_diff(
    previous_bytes: bytes | None,
    new_text: str,
    *,
    path: str,
    previous_available: bool = True,
) -> FileDiff:
    """Describe the change from ``previous_bytes`` to ``new_text`` for ``path``.

    ``previous_bytes`` is ``None`` when the file did not exist. Bytes are decoded
    leniently because this text is for reading, not for restoring: the undo
    journal keeps the exact bytes, and a diff that shows a replacement character
    is still an honest picture of what the model is about to write over.
    ``previous_available=False`` says the previous content exists but could not
    be read, which is a different situation from "there was no previous content"
    and is labelled as such.
    """

    if not previous_available:
        return FileDiff("", 0, 0, False, "unavailable")
    if previous_bytes is None:
        return FileDiff("", 0, 0, False, "created")

    old_text = previous_bytes.decode("utf-8", errors="replace")
    lines = _unified(old_text, new_text, path=path)
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))

    truncated = len(lines) > MAX_DIFF_LINES
    if truncated:
        lines = lines[:MAX_DIFF_LINES]
    text = "".join(lines)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS]
        truncated = True
    return FileDiff(text, added, removed, truncated, "modified")


def _unified(old_text: str, new_text: str, *, path: str) -> list[str]:
    """Render a unified diff as lines that all end in a newline."""

    produced = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=CONTEXT_LINES,
    )
    return [line if line.endswith("\n") else f"{line}\n" for line in produced]
