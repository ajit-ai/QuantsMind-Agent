"""Session-scoped revision journal for reversible writes.

v0.2 made writes reversible in the product's own terms: the roadmap asks for "a
session-scoped undo list". This module is that list, and nothing more. It is a
bounded, in-memory, per-path stack of pre-images -- deliberately not a version
control system. There is no repository, no commit graph, no object store, no
garbage collection and no on-disk state: a revision lives only as long as the
session that made it, and restoring a revision puts the file back exactly as it
was.

Design constraints that come from the surrounding code rather than from taste:

* **Owned by the session.** :class:`~quantsmind_agent.session.Session` holds one
  journal, so it survives the tool re-registration in
  :meth:`~quantsmind_agent.app.ApplicationContext._rebuild` and is cleared by
  :meth:`~quantsmind_agent.session.Session.reset`, which is the same lifecycle
  that clears the conversation and the trust boundary.
* **Per-path stacks.** Undoing a path consumes only that path's newest revision,
  so a session can walk back one file at a time instead of rewinding the whole
  workspace.
* **Exact bytes.** A revision stores the pre-image as raw bytes plus a SHA-256
  digest, because "restore what was there" is a statement about bytes and not
  about visually equivalent text: a CRLF file must come back as CRLF, and a file
  that is not valid UTF-8 must come back unchanged rather than lossily decoded.
* **Digest-guarded.** Every revision also stores the digest the file had *after*
  the recorded write. Undo compares it against the file on disk and refuses on a
  mismatch, so an undo can never silently overwrite a change made outside the
  agent.

The journal records what a tool *actually did*: entries are appended after the
filesystem call succeeded, so a failed write leaves no revision, and a revision
is consumed only after the restore succeeded, so a failed undo can be retried.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

__all__ = ["FileRevision", "RevisionJournal", "capture_revision"]


@dataclass(frozen=True, slots=True)
class FileRevision:
    """One reversible change to one file.

    ``previous_bytes`` is ``None`` when the file did not exist before the change,
    which is how a creation is undone: by restoring the absence, not by writing
    empty content. ``result_sha256`` is the digest the file had once the change
    landed; it is the precondition for undo, and it is why an undo can refuse
    instead of overwriting a later external edit.
    """

    path: str
    tool: str
    existed: bool
    previous_bytes: bytes | None
    previous_sha256: str | None
    result_sha256: str
    recorded_at: float = field(default_factory=time.time)

    @property
    def is_creation(self) -> bool:
        """Whether undoing this revision has to remove the file."""

        return not self.existed

    def describe(self) -> dict[str, object]:
        """Serialisable summary for tool results and diagnostics."""

        return {
            "path": self.path,
            "tool": self.tool,
            "existed": self.existed,
            "previous_sha256": self.previous_sha256,
            "result_sha256": self.result_sha256,
            "recorded_at": round(self.recorded_at, 3),
        }


def capture_revision(
    *,
    path: str,
    tool: str,
    previous_bytes: bytes | None,
    result_sha256: str,
) -> FileRevision:
    """Build a revision, digesting the pre-image that was captured before the write."""

    existed = previous_bytes is not None
    return FileRevision(
        path=path,
        tool=tool,
        existed=existed,
        previous_bytes=previous_bytes,
        previous_sha256=(
            hashlib.sha256(previous_bytes).hexdigest() if previous_bytes is not None else None
        ),
        result_sha256=result_sha256,
    )


class RevisionJournal:
    """Per-path stacks of pre-images for the current session.

    The journal is not thread-safe by design: tool execution is serialised on the
    agent worker thread, and a lock would only hide a real ordering bug.
    """

    def __init__(self) -> None:
        self._stacks: dict[str, list[FileRevision]] = {}

    # -- writing -----------------------------------------------------------
    def record(self, revision: FileRevision) -> None:
        """Push ``revision`` on top of its path's stack."""

        self._stacks.setdefault(revision.path, []).append(revision)

    # -- reading -----------------------------------------------------------
    def peek(self, path: str) -> FileRevision | None:
        """The revision ``undo`` would consume for ``path``, without removing it."""

        stack = self._stacks.get(path)
        return stack[-1] if stack else None

    def depth(self, path: str) -> int:
        """How many changes can still be undone for ``path``."""

        return len(self._stacks.get(path, ()))

    @property
    def size(self) -> int:
        """Total number of undoable revisions across every path."""

        return sum(len(stack) for stack in self._stacks.values())

    @property
    def paths(self) -> list[str]:
        """Every path with at least one undoable revision, sorted."""

        return sorted(self._stacks)

    def undoable(self) -> list[FileRevision]:
        """The newest revision per path -- what the agent would be offered."""

        return [revision for path in self.paths if (revision := self.peek(path)) is not None]

    # -- consuming ---------------------------------------------------------
    def consume(self, path: str, revision: FileRevision) -> bool:
        """Remove ``revision`` once its restore has succeeded.

        Identity is required rather than equality: two identical writes produce
        two equal revisions, and consuming the wrong one would let a caller undo
        a step it did not just restore. Returns ``False`` and changes nothing
        when ``revision`` is no longer on top, so a stale caller cannot corrupt
        the stack.
        """

        stack = self._stacks.get(path)
        if not stack or stack[-1] is not revision:
            return False
        stack.pop()
        if not stack:
            del self._stacks[path]
        return True

    # -- lifecycle ---------------------------------------------------------
    def clear(self) -> None:
        """Forget every revision. Called when the session is reset."""

        self._stacks.clear()

    def __len__(self) -> int:
        return self.size

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"RevisionJournal(paths={len(self._stacks)}, revisions={self.size})"
