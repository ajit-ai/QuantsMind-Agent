"""Regression tests for how command output is bounded.

The cap exists so one verbose command cannot flood the context window. But a
build or test command puts its useful part last -- the failing assertion, the
traceback, the error summary -- so a head-only cap silently discarded the one
thing the agent needs in order to fix the failure it just caused. These pin the
head-and-tail behaviour and, just as importantly, the promise that the cap is
still a cap.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from quantsmind_agent.runtime.tools.builtin.command import CappedReader


def _read(text: str, cap: int) -> CappedReader:
    reader = CappedReader(io.StringIO(text), cap)
    reader.start()
    reader.join(timeout=5)
    return reader


class TestHeadAndTail:
    def test_small_output_is_untouched(self) -> None:
        reader = _read("hello world", 100)

        assert reader.text == "hello world"
        assert reader.truncated is False
        assert reader.omitted_chars == 0

    def test_output_exactly_at_the_cap_is_untouched(self) -> None:
        reader = _read("x" * 100, 100)

        assert reader.text == "x" * 100
        assert reader.truncated is False

    def test_the_tail_survives_truncation(self) -> None:
        reader = _read("A" * 5000 + "FAILURE AT THE END", 200)

        assert reader.truncated is True
        assert "FAILURE AT THE END" in reader.text, "the diagnostic end is what matters"

    def test_the_head_survives_truncation(self) -> None:
        reader = _read("FIRST LINE\n" + "x" * 5000, 200)

        assert reader.text.startswith("FIRST LINE")

    def test_the_middle_is_replaced_by_a_visible_marker(self) -> None:
        reader = _read("head" + "y" * 5000 + "tail", 200)

        assert "chars omitted" in reader.text
        assert "y" * 100 not in reader.text
        assert reader.omitted_chars > 4000

    def test_the_retained_text_never_exceeds_the_cap(self) -> None:
        # The marker is part of the budget: a cap is a promise about how much
        # text the model is handed, not just about how much is kept.
        for cap in (40, 100, 200, 1_000, 20_000):
            reader = _read("z" * (cap * 5), cap)
            assert len(reader.text) <= cap, cap

    def test_truncation_is_reported(self) -> None:
        reader = _read("q" * 10_000, 100)

        assert reader.truncated is True
        assert reader.omitted_chars == 10_000 - 100

    def test_a_very_small_cap_still_returns_something_useful(self) -> None:
        reader = _read("start" + "w" * 500 + "end", 10)

        assert reader.truncated is True
        assert reader.text.endswith("end")

    def test_a_zero_cap_keeps_nothing_and_says_so(self) -> None:
        # The configuration model forbids a zero cap, but the reader must not
        # crash on one: there is nothing to show, and the omission is reported.
        reader = _read("anything", 0)

        assert reader.text == ""
        assert reader.truncated is True
        assert reader.omitted_chars == 8

    def test_reading_a_closed_stream_does_not_raise(self) -> None:
        class Broken(io.StringIO):
            def read(self, size: int = -1) -> str:  # type: ignore[override]
                raise OSError("pipe closed")

        reader = CappedReader(Broken("x"), 100)
        reader.start()
        reader.join(timeout=5)

        assert reader.text == ""


class TestThroughTheTool:
    def test_a_failing_suite_keeps_its_traceback(self, tmp_path: Path) -> None:
        # End to end: a real failing pytest run through a deliberately tiny cap,
        # which is the situation that used to lose the failure entirely.
        from quantsmind_agent.runtime.tools import ToolRegistry
        from quantsmind_agent.runtime.tools.builtin.command import build_command_tools
        from quantsmind_agent.security.permissions import (
            ApprovalOutcome,
            PermissionBroker,
            StaticPermissionProvider,
        )
        from quantsmind_agent.services.workspace.containment import Workspace

        broken = tmp_path / "test_broken.py"
        broken.write_text(
            "def test_fails():\n    assert 1 == 2, 'the assertion message'\n", encoding="utf-8"
        )
        registry = ToolRegistry(
            permissions=PermissionBroker(
                StaticPermissionProvider.for_v0_2(),
                approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
            )
        )
        registry.register_all(build_command_tools(Workspace(tmp_path), max_output_chars=300))

        result = registry.execute(
            "run_command",
            {
                "command": sys.executable,
                "arguments": ["-m", "pytest", "-q", "-p", "no:cacheprovider"],
            },
        )

        assert result.ok
        assert result.data["exit_code"] != 0
        combined = result.data["stdout"] + result.data["stderr"]
        assert "test_fails" in combined, "the failing test must survive the cap"
        assert result.data["stdout_truncated"] is True or result.data["stderr_truncated"] is True
        assert result.data["stdout_omitted_chars"] > 0 or result.data["stderr_omitted_chars"] > 0
