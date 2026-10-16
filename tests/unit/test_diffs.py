"""Unit tests for the bounded diffs reported by the mutating tools.

A diff is only worth anything if it is honest about its own limits, so these
tests pin the three states a caller has to tell apart -- a change, a creation,
and a pre-image that could not be read -- along with the truncation behaviour
that keeps a whole-file rewrite from filling the model's context.
"""

from __future__ import annotations

from quantsmind_agent.diffs import MAX_DIFF_CHARS, MAX_DIFF_LINES, revision_diff

README = "README.md"


class TestUnifiedDiff:
    def test_a_one_line_change_is_shown_with_context(self) -> None:
        diff = revision_diff(b"alpha\nbeta\ngamma\n", "alpha\nBETA\ngamma\n", path=README)

        assert diff.kind == "modified"
        assert "-beta" in diff.text
        assert "+BETA" in diff.text
        assert f"--- a/{README}" in diff.text
        assert f"+++ b/{README}" in diff.text

    def test_line_counts_describe_the_change(self) -> None:
        diff = revision_diff(b"one\ntwo\n", "one\nTWO\nthree\n", path=README)

        assert diff.lines_added == 2
        assert diff.lines_removed == 1

    def test_the_headers_are_not_counted_as_changes(self) -> None:
        # "---" and "+++" start with the diff markers; counting them would inflate
        # every result by two.
        diff = revision_diff(b"a\n", "b\n", path=README)

        assert diff.lines_added == 1
        assert diff.lines_removed == 1

    def test_identical_content_produces_no_diff(self) -> None:
        diff = revision_diff(b"same\n", "same\n", path=README)

        assert diff.text == ""
        assert diff.lines_added == 0
        assert diff.lines_removed == 0
        assert diff.truncated is False

    def test_every_line_ends_with_a_newline(self) -> None:
        diff = revision_diff(b"no trailing newline", "now there is one", path=README)

        assert diff.text.endswith("\n")
        assert "\n\n" not in diff.text

    def test_crlf_content_is_compared_as_stored(self) -> None:
        diff = revision_diff(b"one\r\ntwo\r\n", "one\r\nTWO\r\n", path=README)

        assert diff.lines_added == 1
        assert diff.lines_removed == 1

    def test_content_that_is_not_valid_utf8_still_produces_a_diff(self) -> None:
        # The pre-image is decoded leniently for display only; the journal keeps
        # the exact bytes. A replacement character is better than no diff at all.
        diff = revision_diff(b"caf\xe9\n", "cafe\n", path=README)

        assert diff.kind == "modified"
        assert diff.lines_added == 1
        assert diff.lines_removed == 1

    def test_separate_changes_are_reported_together(self) -> None:
        diff = revision_diff(b"a\nb\nc\nd\ne\nf\ng\nh\n", "A\nb\nc\nd\ne\nf\ng\nH\n", path=README)

        assert diff.lines_added == 2
        assert diff.lines_removed == 2
        assert "@@" in diff.text


class TestNothingToCompare:
    def test_a_creation_is_named_rather_than_rendered_as_empty(self) -> None:
        diff = revision_diff(None, "brand new\n", path=README)

        assert diff.kind == "created"
        assert diff.text == ""
        assert diff.lines_added == 0
        assert diff.lines_removed == 0

    def test_an_unreadable_pre_image_is_reported_as_unavailable(self) -> None:
        diff = revision_diff(None, "replacement\n", path=README, previous_available=False)

        assert diff.kind == "unavailable"
        assert diff.text == ""


class TestBounded:
    def test_a_large_change_is_truncated_but_still_counted(self) -> None:
        before = "".join(f"line {index}\n" for index in range(2000))
        after = "".join(f"changed {index}\n" for index in range(2000))

        diff = revision_diff(before.encode(), after, path=README)

        assert diff.truncated is True
        assert diff.lines_added == 2000
        assert diff.lines_removed == 2000
        assert diff.text.count("\n") <= MAX_DIFF_LINES

    def test_a_small_change_is_not_truncated(self) -> None:
        diff = revision_diff(b"a\n", "b\n", path=README)

        assert diff.truncated is False

    def test_the_text_never_exceeds_the_character_budget(self) -> None:
        # Few lines, very long ones: the line cap alone would not catch this.
        before = "x" * 4000
        after = f"{'y' * 4000}\n{'z' * 4000}\n"

        diff = revision_diff(before.encode(), after, path=README)

        assert len(diff.text) <= MAX_DIFF_CHARS
        assert diff.truncated is True


class TestPayload:
    def test_every_key_is_always_present(self) -> None:
        payload = revision_diff(b"a\n", "b\n", path=README).to_payload()

        assert set(payload) == {
            "diff",
            "diff_kind",
            "diff_added",
            "diff_removed",
            "diff_truncated",
        }

    def test_a_creation_still_reports_every_key(self) -> None:
        payload = revision_diff(None, "new\n", path=README).to_payload()

        assert payload["diff_kind"] == "created"
        assert payload["diff"] == ""
        assert payload["diff_truncated"] is False
