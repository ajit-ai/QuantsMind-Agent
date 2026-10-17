"""Unit tests for the symbol parser behind the code-navigation tools.

These are the facts an agent will act on: a wrong line range turns a precise
edit into a wrong edit, so the ranges, kinds and signatures are pinned rather
than spot-checked.
"""

from __future__ import annotations

import pytest

from quantsmind_agent.code_index import Symbol, find_symbols, find_uses, parse_symbols

SAMPLE = '''"""Module docstring."""

import os

TOP_LEVEL = 1


class Widget:
    """A widget."""

    kind = "widget"

    def __init__(self, name: str, size: int = 3) -> None:
        self.name = name
        self.size = size

    @property
    def label(self) -> str:
        """The label."""
        return self.name

    @label.setter
    def label(self, value: str) -> None:
        self.name = value

    async def refresh(self, *, force: bool = False) -> None:
        pass


def build(name: str, *tags, **options) -> Widget:
    """Build one."""
    def inner() -> None:
        pass

    return Widget(name)


async def fetch(url: str) -> bytes:
    return b""


class Empty:
    pass
'''


@pytest.fixture(scope="module")
def symbols() -> list[Symbol]:
    return parse_symbols(SAMPLE)


def lines_of(source: str) -> list[str]:
    return source.splitlines()


class TestOutline:
    def test_every_definition_is_found(self, symbols: list[Symbol]) -> None:
        names = {symbol.qualified for symbol in symbols}

        assert names == {
            "Widget",
            "Widget.__init__",
            "Widget.label",
            "Widget.refresh",
            "build",
            "build.inner",
            "fetch",
            "Empty",
        }

    def test_kinds_distinguish_the_constructs(self, symbols: list[Symbol]) -> None:
        kinds = {symbol.qualified: symbol.kind for symbol in symbols}

        assert kinds["Widget"] == "class"
        assert kinds["Widget.__init__"] == "method"
        assert kinds["build"] == "function"
        assert kinds["fetch"] == "async function"
        assert kinds["build.inner"] == "function"

    def test_a_property_getter_and_setter_are_separate_blocks(self, symbols: list[Symbol]) -> None:
        # They are distinct source ranges, so they are reported separately; both
        # are classified as properties so neither is mistaken for a method.
        labels = [s for s in symbols if s.qualified == "Widget.label"]

        assert len(labels) == 2
        assert {s.kind for s in labels} == {"property"}
        assert labels[0].line < labels[1].line

    def test_line_ranges_are_one_based_and_inclusive(self, symbols: list[Symbol]) -> None:
        by_name = {symbol.qualified: symbol for symbol in symbols}
        lines = SAMPLE.splitlines()

        widget = by_name["Widget"]
        assert lines[widget.line - 1].startswith("class Widget")
        assert widget.end_line >= widget.line
        assert lines[widget.end_line - 1].strip() == "pass"

    def test_a_range_covers_the_whole_body(self, symbols: list[Symbol]) -> None:
        lines = SAMPLE.splitlines()
        init = next(s for s in symbols if s.qualified == "Widget.__init__")

        body = lines[init.line - 1 : init.end_line]

        assert any("self.name = name" in line for line in body)
        assert any("self.size = size" in line for line in body)

    def test_signatures_carry_types_and_defaults(self, symbols: list[Symbol]) -> None:
        init = next(s for s in symbols if s.qualified == "Widget.__init__")
        refresh = next(s for s in symbols if s.qualified == "Widget.refresh")
        build = next(s for s in symbols if s.qualified == "build")

        assert init.signature == "def __init__(self, name: str, size: int = 3) -> None"
        assert refresh.signature == "async def refresh(self, *, force: bool = False) -> None"
        assert build.signature == "def build(name: str, *tags, **options) -> Widget"

    def test_signatures_survive_line_wrapping(self) -> None:
        wrapped = (
            "def wide(\n    first: int,\n    second: str = 'x',\n) -> bool:\n    return True\n"
        )

        signature = parse_symbols(wrapped)[0].signature

        assert signature == "def wide(first: int, second: str = 'x') -> bool"

    def test_docstring_summary_is_the_first_line_only(self, symbols: list[Symbol]) -> None:
        build = next(s for s in symbols if s.qualified == "build")

        assert build.doc == "Build one."

    def test_decorators_are_recorded(self, symbols: list[Symbol]) -> None:
        refresh = next(s for s in symbols if s.qualified == "Widget.refresh")
        empty = next(s for s in symbols if s.qualified == "Empty")

        assert refresh.doc == ""
        assert empty.decorators == ()

    def test_a_class_with_only_a_pass_body_still_has_a_range(self, symbols: list[Symbol]) -> None:
        empty = next(s for s in symbols if s.qualified == "Empty")

        assert empty.end_line > empty.line
        assert lines_of(SAMPLE)[empty.end_line - 1].strip() == "pass"

    def test_symbols_serialise_for_the_tool_result(self, symbols: list[Symbol]) -> None:
        build = next(s for s in symbols if s.qualified == "build")

        payload = build.to_dict()

        assert payload["qualified"] == "build"
        lines = lines_of(SAMPLE)
        header = next(index for index, line in enumerate(lines) if line.startswith("def build("))
        assert payload["line"] == header + 1
        assert set(payload) == {
            "name",
            "qualified",
            "kind",
            "line",
            "end_line",
            "signature",
            "decorators",
            "doc",
            "parent",
        }

    def test_an_empty_file_has_no_symbols(self) -> None:
        assert parse_symbols("") == []

    def test_a_syntax_error_is_raised_for_the_caller_to_handle(self) -> None:
        with pytest.raises(SyntaxError) as info:
            parse_symbols("def broken(:\n")

        assert info.value.lineno == 1


class TestDefinitions:
    def test_the_simple_name_matches(self) -> None:
        assert [s.qualified for s in find_symbols(SAMPLE, "Widget")] == ["Widget"]

    def test_the_qualified_name_matches(self) -> None:
        # Getter and setter share the qualified name, so both are reported.
        found = find_symbols(SAMPLE, "Widget.label")

        assert [s.qualified for s in found] == ["Widget.label", "Widget.label"]
        assert found[0].kind == found[1].kind == "property"

    def test_an_unknown_symbol_has_no_definitions(self) -> None:
        assert find_symbols(SAMPLE, "nonexistent") == []


class TestUses:
    def test_call_sites_are_found(self) -> None:
        # The two constructions inside build(); the class header is not a use.
        assert find_uses(SAMPLE, "Widget") == [30, 35]

    def test_attribute_access_is_matched(self) -> None:
        # The ``@label.setter`` decorator reads the existing property.
        assert find_uses(SAMPLE, "label") == [22]

    def test_an_assignment_target_is_not_a_use(self) -> None:
        # ``TOP_LEVEL = 1`` stores the name; nothing reads it.
        assert find_uses(SAMPLE, "TOP_LEVEL") == []

    def test_a_read_of_a_module_constant_is_a_use(self) -> None:
        assert find_uses("TOP_LEVEL = 1\nvalue = TOP_LEVEL\n", "TOP_LEVEL") == [2]

    def test_imports_count_as_uses(self) -> None:
        assert find_uses("import os\nos.path\n", "path") == [2]
