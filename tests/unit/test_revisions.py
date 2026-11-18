"""Unit tests for the session-scoped revision journal.

The journal is deliberately a small data structure, so these tests stay close to
it: a stack per path, identity-based consumption, and a digest that describes the
bytes rather than the text. Filesystem behaviour is covered in
``test_undo_tools.py``.
"""

from __future__ import annotations

import hashlib

from quantsmind_agent.revisions import FileRevision, RevisionJournal, capture_revision


def _revision(path: str, *, previous: bytes | None, result: str = "a" * 64) -> FileRevision:
    return capture_revision(
        path=path,
        tool="write_file",
        previous_bytes=previous,
        result_sha256=result,
    )


class TestCapture:
    def test_an_existing_file_digests_its_pre_image(self) -> None:
        revision = _revision("a.txt", previous=b"hello")

        assert revision.existed is True
        assert revision.is_creation is False
        assert revision.previous_sha256 == hashlib.sha256(b"hello").hexdigest()

    def test_a_created_file_records_absence(self) -> None:
        revision = _revision("new.txt", previous=None)

        assert revision.existed is False
        assert revision.is_creation is True
        assert revision.previous_bytes is None
        assert revision.previous_sha256 is None

    def test_an_empty_file_is_not_the_same_as_an_absent_one(self) -> None:
        # Zero bytes still existed, and restoring emptiness is not restoring
        # absence: one recreates the file, the other removes it.
        revision = _revision("empty.txt", previous=b"")

        assert revision.existed is True
        assert revision.previous_bytes == b""
        assert revision.previous_sha256 == hashlib.sha256(b"").hexdigest()

    def test_the_revision_describes_itself_without_the_payload(self) -> None:
        described = _revision("a.txt", previous=b"secret bytes").describe()

        assert described["path"] == "a.txt"
        assert described["tool"] == "write_file"
        assert "secret bytes" not in str(described)

    def test_the_recorded_write_is_always_identified(self) -> None:
        # A file the agent created has no previous digest, so the post-write
        # digest is the only thing an undo can check against.
        revision = _revision("new.txt", previous=None, result="b" * 64)

        assert revision.result_sha256 == "b" * 64


class TestPerPathStacks:
    def test_peek_returns_the_newest_revision(self) -> None:
        journal = RevisionJournal()
        first = _revision("a.txt", previous=b"1")
        second = _revision("a.txt", previous=b"2")
        journal.record(first)
        journal.record(second)

        assert journal.peek("a.txt") is second
        assert journal.depth("a.txt") == 2

    def test_paths_keep_independent_stacks(self) -> None:
        journal = RevisionJournal()
        journal.record(_revision("a.txt", previous=b"a"))
        journal.record(_revision("b.txt", previous=b"b"))

        assert journal.paths == ["a.txt", "b.txt"]
        assert journal.depth("a.txt") == 1
        assert journal.depth("b.txt") == 1
        assert journal.size == 2

    def test_an_unknown_path_has_no_revision(self) -> None:
        assert RevisionJournal().peek("nothing.txt") is None
        assert RevisionJournal().depth("nothing.txt") == 0


class TestConsumption:
    def test_consuming_removes_only_the_named_revision(self) -> None:
        journal = RevisionJournal()
        first = _revision("a.txt", previous=b"1")
        second = _revision("a.txt", previous=b"2")
        journal.record(first)
        journal.record(second)

        assert journal.consume("a.txt", second) is True
        assert journal.peek("a.txt") is first
        assert journal.depth("a.txt") == 1

    def test_a_stale_consumer_cannot_corrupt_the_stack(self) -> None:
        journal = RevisionJournal()
        first = _revision("a.txt", previous=b"1")
        second = _revision("a.txt", previous=b"2")
        journal.record(first)
        journal.record(second)

        # A caller holding an old revision must not be able to drop the new one.
        assert journal.consume("a.txt", first) is False
        assert journal.peek("a.txt") is second
        assert journal.depth("a.txt") == 2

    def test_equal_revisions_are_distinguished_by_identity(self) -> None:
        # Two writes can be indistinguishable by value. Consumption must still
        # remove the revision that was actually restored, not an equal one.
        journal = RevisionJournal()
        first = FileRevision("a.txt", "write_file", True, b"same", "d" * 64, "c" * 64, 1.0)
        second = FileRevision("a.txt", "write_file", True, b"same", "d" * 64, "c" * 64, 1.0)
        journal.record(first)
        journal.record(second)

        assert first == second
        assert journal.consume("a.txt", second) is True
        assert journal.peek("a.txt") is first

    def test_a_path_left_empty_disappears(self) -> None:
        journal = RevisionJournal()
        revision = _revision("a.txt", previous=b"1")
        journal.record(revision)
        journal.consume("a.txt", revision)

        assert journal.paths == []
        assert journal.size == 0
        assert len(journal) == 0

    def test_consuming_an_empty_path_reports_failure(self) -> None:
        journal = RevisionJournal()

        assert journal.consume("a.txt", _revision("a.txt", previous=b"1")) is False

    def test_undoable_lists_the_newest_revision_per_path(self) -> None:
        journal = RevisionJournal()
        journal.record(_revision("a.txt", previous=b"1"))
        newest = _revision("a.txt", previous=b"2")
        journal.record(newest)
        journal.record(_revision("b.txt", previous=b"3"))

        undoable = journal.undoable()

        assert [revision.path for revision in undoable] == ["a.txt", "b.txt"]
        assert undoable[0] is newest


class TestLifecycle:
    def test_clear_forgets_everything(self) -> None:
        journal = RevisionJournal()
        journal.record(_revision("a.txt", previous=b"1"))
        journal.record(_revision("b.txt", previous=b"2"))

        journal.clear()

        assert journal.size == 0
        assert journal.paths == []

    def test_the_journal_can_be_reused_after_clearing(self) -> None:
        journal = RevisionJournal()
        journal.record(_revision("a.txt", previous=b"1"))
        journal.clear()

        journal.record(_revision("a.txt", previous=b"2"))

        assert journal.depth("a.txt") == 1
