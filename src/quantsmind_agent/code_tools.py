"""The v0.3 code-navigation tools.

``code_outline``
    What is in this file: every class, function, method and property with its
    exact line range, signature, decorators and docstring summary.
``find_references``
    Where a symbol is defined and where it is used, across the project.

Both are read-only and require :attr:`Permission.READ_WORKSPACE`, so they
inherit the same containment rule as every other read: paths go through
:meth:`Workspace.resolve` and cannot escape the project.

Why they matter for editing, not just for looking
--------------------------------------------------
``edit_file`` already supports line-addressed operations (``replace_lines``).
An outline hands the model the line range of the exact block it wants to change,
so a precise edit no longer requires reading a whole module into the context --
and on a file larger than the read limit it becomes possible at all.

Failure behaviour is part of the contract. Neither tool guesses:

* a file that is not Python is refused with a pointer to ``read_file``;
* a file that does not parse reports the syntax error and its line, because a
  half-parsed outline would be worse than none;
* files that are skipped (too large, unreadable, non-text) are counted and
  reported, so a truncated answer is never mistaken for a complete one.
"""

from __future__ import annotations

from typing import Any

from quantsmind_agent.code_index import (
    MAX_SYMBOLS_PER_FILE,
    Symbol,
    find_symbols,
    find_uses,
    parse_symbols,
)
from quantsmind_agent.errors import InvalidPathError
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.permissions import Permission
from quantsmind_agent.tools import ToolResult, ToolSpec
from quantsmind_agent.workspace import Workspace

__all__ = [
    "CodeOutlineTool",
    "FindReferencesTool",
    "build_code_tools",
]

logger = get_logger(__name__)

#: Only Python source is parsed. Anything else is refused by name.
PYTHON_SUFFIX = ".py"

_MAX_REFERENCES = 200
_MAX_SCANNED_FILES = 400
_MAX_RENDERED_SYMBOLS = 200


def _as_int(arguments: dict[str, Any], key: str, default: int, *, low: int, high: int) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, int(value)))


def _syntax_failure(tool: str, summary: str, path: str, exc: SyntaxError) -> ToolResult:
    """A parse failure the model can act on, not a stack trace."""

    line = exc.lineno or 1
    message = (exc.msg or "could not be parsed").strip()
    return ToolResult.failure(
        tool,
        summary,
        f"'{path}' is not valid Python (line {line}: {message}). "
        "Fix the syntax error, or use read_file to inspect the file directly.",
    )


def _render_outline(symbols: list[Symbol], limit: int) -> str:
    """A compact tree the model can read without parsing JSON."""

    lines: list[str] = []
    for symbol in symbols[:limit]:
        indent = "    " * (0 if not symbol.parent else symbol.parent.count(".") + 1)
        label = f"{symbol.signature}  [{symbol.line}-{symbol.end_line}]"
        if symbol.doc:
            label += f"  # {symbol.doc}"
        lines.append(f"{indent}{symbol.kind} {label}")
    return "\n".join(lines)


class CodeOutlineTool:
    """Structured outline of one Python file."""

    def __init__(self, workspace: Workspace, *, max_bytes: int = 2_000_000) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self.spec = ToolSpec(
            name="code_outline",
            description=(
                "List every class, function, method and property in one Python file, with "
                "line ranges, signatures, decorators and the first line of each docstring. "
                "Use it before editing: the line range tells edit_file exactly which lines "
                "to replace, so you often do not need to read the whole file. Python files "
                "only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path of a Python file.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="outline {path}",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        path = str(arguments.get("path") or "")
        if not path:
            return ToolResult.failure(self.spec.name, "outline", "No file path was provided.")

        relative = self._workspace.relative(path)
        if not relative.endswith(PYTHON_SUFFIX):
            return ToolResult.failure(
                self.spec.name,
                self.spec.summarize(arguments),
                f"code_outline reads Python source, and '{relative}' is not a .py file. "
                "Use read_file for other files.",
            )

        try:
            source = self._workspace.read_text(relative, max_bytes=self._max_bytes)
        except InvalidPathError as exc:
            return ToolResult.failure(
                self.spec.name, self.spec.summarize(arguments), exc.user_message
            )

        try:
            symbols = parse_symbols(source)
        except SyntaxError as exc:
            return _syntax_failure(self.spec.name, self.spec.summarize(arguments), relative, exc)

        shown = symbols[:MAX_SYMBOLS_PER_FILE]
        return ToolResult.success(
            self.spec.name,
            f"outlined {relative} ({len(shown)} symbols)",
            {
                "path": relative,
                "lines": source.count("\n") + 1 if source else 0,
                "symbol_count": len(symbols),
                "returned": len(shown),
                "truncated": len(symbols) > len(shown),
                "outline": _render_outline(shown, _MAX_RENDERED_SYMBOLS),
                "symbols": [symbol.to_dict() for symbol in shown],
            },
        )


class FindReferencesTool:
    """Definitions and usages of one Python symbol across the project."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        max_bytes: int = 2_000_000,
        max_files: int = _MAX_SCANNED_FILES,
    ) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self._max_files = max_files
        self.spec = ToolSpec(
            name="find_references",
            description=(
                "Find where a Python symbol is defined and where it is used, across the "
                "project. Returns definitions with line ranges and a list of call sites, "
                "so you can see the blast radius before renaming or changing behaviour. "
                "Python files only; other file types are not scanned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Name of the function, class or method to look for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory to search under.",
                        "default": ".",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of references to return (1-200).",
                        "default": 50,
                    },
                },
                "required": ["symbol"],
                "additionalProperties": False,
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="references to {symbol}",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        symbol = str(arguments.get("symbol") or "").strip()
        if not symbol:
            return ToolResult.failure(
                self.spec.name, self.spec.summarize(arguments), "No symbol name was provided."
            )
        root = str(arguments.get("path") or ".")
        limit = _as_int(arguments, "max_results", 50, low=1, high=_MAX_REFERENCES)

        try:
            entries = list(self._workspace.walk(root, max_entries=self._max_files))
        except InvalidPathError as exc:
            return ToolResult.failure(
                self.spec.name, self.spec.summarize(arguments), exc.user_message
            )

        definitions: list[dict[str, object]] = []
        references: list[dict[str, object]] = []
        scanned = 0
        skipped = 0
        unparsable: list[str] = []
        truncated = False

        for entry in entries:
            if truncated:
                break
            if not entry.path.endswith(PYTHON_SUFFIX):
                continue
            try:
                source = self._workspace.read_text(entry.path, max_bytes=self._max_bytes)
            except InvalidPathError:
                skipped += 1
                continue

            scanned += 1
            try:
                found = find_symbols(source, symbol)
                used = find_uses(source, symbol)
            except SyntaxError:
                unparsable.append(entry.path)
                continue

            definition_lines = {item.line for item in found}
            for item in found:
                definitions.append(
                    {
                        "path": entry.path,
                        "qualified": item.qualified,
                        "kind": item.kind,
                        "line": item.line,
                        "end_line": item.end_line,
                    }
                )
            for line in used:
                if line in definition_lines:
                    continue
                if len(references) >= limit:
                    truncated = True
                    break
                references.append({"path": entry.path, "line": line})

        if len(references) >= limit and not truncated:
            truncated = False

        return ToolResult.success(
            self.spec.name,
            f"{len(definitions)} definitions and {len(references)} references of {symbol}",
            {
                "symbol": symbol,
                "root": root,
                "files_scanned": scanned,
                "files_skipped": skipped,
                "unparsable_files": unparsable[:20],
                "definition_count": len(definitions),
                "reference_count": len(references),
                "truncated": truncated,
                "definitions": definitions[:limit],
                "references": references,
            },
        )


def build_code_tools(workspace: Workspace, *, max_file_bytes: int = 2_000_000) -> list[Any]:
    """Instantiate the v0.3 code-navigation tools bound to ``workspace``."""

    return [
        CodeOutlineTool(workspace, max_bytes=max_file_bytes),
        FindReferencesTool(workspace, max_bytes=max_file_bytes),
    ]
