"""The v0.3 code-navigation tools.

``code_outline``
    What is in this file: every class, function, method and property with its
    exact line range, signature, decorators and docstring summary.
``find_references``
    Where a symbol is defined and where it is used, across the project.
``project_symbols``
    Every definition in the project, or the ones matching a name or a kind, in
    one call -- so locating code does not cost one outline per file.

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

from quantsmind_agent.code_imports import ResolvedImport, parse_imports, resolve_import
from quantsmind_agent.code_index import (
    MAX_SYMBOLS_PER_FILE,
    Symbol,
    find_uses,
    parse_symbols,
)
from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import InvalidPathError
from quantsmind_agent.project_intel import ProjectIntelligence, task_context
from quantsmind_agent.runtime.tools import ToolResult, ToolSpec
from quantsmind_agent.security.permissions import Permission
from quantsmind_agent.services.workspace.containment import Workspace

__all__ = [
    "CodeOutlineTool",
    "FindReferencesTool",
    "ProjectSymbolsTool",
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
                "Find where a Python symbol is defined, where it is imported, and where "
                "it is used, across the project. Imports are resolved to real files, so "
                "dependent_files tells you exactly which modules a change to this symbol "
                "reaches, and related adds one level of import expansion for those files. "
                "Use it before changing an interface so you update its callers. Python "
                "files only; matching is by name and import, with no type inference."
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
                    "expand": {
                        "type": "boolean",
                        "description": (
                            "Follow one import level from the dependent files to show what "
                            "else they pull in. Default true."
                        ),
                        "default": True,
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
        expand = arguments.get("expand", True) is not False

        try:
            entries = list(self._workspace.walk(root, max_entries=self._max_files))
        except InvalidPathError as exc:
            return ToolResult.failure(
                self.spec.name, self.spec.summarize(arguments), exc.user_message
            )

        definitions: list[dict[str, object]] = []
        references: list[dict[str, object]] = []
        defined_in: dict[str, set[str]] = {}
        dependents: list[str] = []
        per_file_imports: dict[str, list[ResolvedImport]] = {}
        unresolved = 0
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
                all_symbols = parse_symbols(source)
                used = find_uses(source, symbol)
                records = parse_imports(source)
            except SyntaxError:
                unparsable.append(entry.path)
                continue

            found = [
                item for item in all_symbols if item.name == symbol or item.qualified == symbol
            ]
            definition_lines = {item.line for item in found}
            if found:
                # Every name this file defines, not just the one searched for:
                # expansion needs to see what a dependent file shares with it.
                defined_in[entry.path] = {item.name for item in all_symbols}
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

            resolved_here: list[ResolvedImport] = []
            import_lines: set[int] = set()
            for record in records:
                bound = resolve_import(self._workspace, record, entry.path)
                if not bound:
                    # An import the workspace cannot map to a file. Counted, never
                    # guessed at: pretending to know the target is worse than
                    # saying it could not be resolved.
                    unresolved += 1
                    continue
                for binding in bound:
                    resolved_here.append(binding)
                    if binding.bound_name == symbol or binding.imported_name == symbol:
                        import_lines.add(binding.line)
                        if entry.path not in dependents:
                            dependents.append(entry.path)
            if resolved_here:
                per_file_imports[entry.path] = resolved_here

            for line in used:
                if line in definition_lines:
                    continue
                if len(references) >= limit:
                    truncated = True
                    break
                # Where a name appears decides what it means: on an import line
                # it is a dependency, anywhere else it is a use.
                kind = "import" if line in import_lines else "use"
                references.append({"path": entry.path, "line": line, "kind": kind})

        related = (
            _related_symbols(per_file_imports, defined_in, dependents, limit=limit)
            if expand
            else []
        )

        return ToolResult.success(
            self.spec.name,
            f"{len(definitions)} definitions and {len(references)} references of {symbol}"
            + (f" across {len(dependents)} importing files" if dependents else ""),
            {
                "symbol": symbol,
                "root": root,
                "files_scanned": scanned,
                "files_skipped": skipped,
                "unparsable_files": unparsable[:20],
                "unresolved_imports": unresolved,
                "definition_count": len(definitions),
                "reference_count": len(references),
                "dependent_files": dependents[:limit],
                "related": related,
                "truncated": truncated,
                "expanded": expand,
                # Two files defining one name is a real ambiguity, and the agent
                # has to see it rather than pick one silently.
                "ambiguous": len(defined_in) > 1,
                "resolution": "name and import matching, one import level, no type inference",
                "definitions": definitions[:limit],
                "references": references,
            },
        )


def _related_symbols(
    per_file_imports: dict[str, list[ResolvedImport]],
    defined_in: dict[str, set[str]],
    dependents: list[str],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """One level of import expansion: what else the dependent files pull in.

    When ``symbol`` is defined in ``pkg/store.py`` and ``pkg/reader.py`` imports
    from it, the change is not only in ``reader.py``'s call sites -- it touches
    whatever else ``reader.py`` binds from that same module. That is one level
    of import following: enough to scope a cross-file change, and bounded by
    construction so it cannot walk the whole project.
    """

    related: list[dict[str, object]] = []
    for path in dependents:
        for item in per_file_imports.get(path, []):
            if item.bound_name in defined_in.get(item.path, set()) and len(related) < limit:
                related.append(
                    {
                        "importer": path,
                        "source_module": item.path,
                        "imported_name": item.imported_name,
                        "bound_name": item.bound_name,
                        "line": item.line,
                    }
                )
    return related


class ProjectSymbolsTool:
    """Project-wide map of classes and functions, in one call.

    ``code_outline`` answers "what is in this file", but locating one symbol in
    an unfamiliar project otherwise costs one outline per file. This is the
    index view: every definition in the project, filtered by a name fragment or a
    kind, so the model can go straight to the file that matters.

    Uncached, this walks and parses the project on every call. When a shared
    :class:`ProjectIntelligence` is supplied it answers from that instead, and
    a file the agent has just written is re-read rather than reported stale.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        max_bytes: int = 2_000_000,
        max_files: int = _MAX_SCANNED_FILES,
        intelligence: ProjectIntelligence | None = None,
    ) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._intelligence = intelligence
        self.spec = ToolSpec(
            name="project_symbols",
            description=(
                "List the classes and functions defined anywhere in the project, each with "
                "its file and line range. Use it to locate code you have not read yet: "
                "filter by name to find one definition, or by kind to map a package. "
                "Cheaper than outlining files one at a time. Python files only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory to index.",
                        "default": ".",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Only report symbols whose qualified name contains this text "
                            "(case-insensitive). Omit for everything."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "description": "Only report symbols of this kind.",
                        "enum": [
                            "class",
                            "function",
                            "method",
                            "property",
                            "async function",
                            "async method",
                        ],
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of symbols to return (1-500).",
                        "default": 200,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="project symbols",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Answer from the shared index when there is one, else by scanning.

        Both paths return the same shape, so nothing downstream -- a test, the
        model, the GUI -- has to care which one ran.
        """

        if self._intelligence is not None:
            return self.run_indexed(arguments)
        return self.scan(arguments)

    def scan(self, arguments: dict[str, Any]) -> ToolResult:
        root = str(arguments.get("path") or ".")
        query = str(arguments.get("query") or "").strip().lower()
        kind = str(arguments.get("kind") or "").strip()
        limit = _as_int(arguments, "max_results", 200, low=1, high=500)

        try:
            entries = list(self._workspace.walk(root, max_entries=self._max_files))
        except InvalidPathError as exc:
            return ToolResult.failure(
                self.spec.name, self.spec.summarize(arguments), exc.user_message
            )

        symbols: list[dict[str, object]] = []
        scanned = 0
        skipped = 0
        unparsable: list[str] = []
        total = 0
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
                found = parse_symbols(source)
            except SyntaxError:
                unparsable.append(entry.path)
                continue

            for symbol in found:
                if kind and symbol.kind != kind:
                    continue
                if query and query not in symbol.qualified.lower():
                    continue
                total += 1
                if len(symbols) < limit:
                    symbols.append({"path": entry.path, **symbol.to_dict()})
                else:
                    truncated = True
                    break

        filters = []
        if query:
            filters.append(f" matching '{query}'")
        if kind:
            filters.append(f" of kind {kind}")
        described = "".join(filters) if filters else ""

        return ToolResult.success(
            self.spec.name,
            f"{total} symbols{described} in {scanned} files",
            {
                "root": root,
                "files_scanned": scanned,
                "files_skipped": skipped,
                "unparsable_files": unparsable[:20],
                "symbol_count": total,
                "returned": len(symbols),
                "truncated": truncated,
                "symbols": symbols,
            },
        )

    def run_indexed(self, arguments: dict[str, Any]) -> ToolResult:
        """Answer from the shared project map instead of walking the project.

        Same contract as :meth:`run`: a filtered list of symbols with their file
        and line range, bounded, and ``truncated`` set when the cap was hit. The
        difference is cost -- one walk for the whole session rather than one per
        call.
        """

        intel = self._intelligence
        if intel is None:  # pragma: no cover - guarded by build_code_tools
            return self.run(arguments)
        root = str(arguments.get("path") or ".")
        query = str(arguments.get("query") or "").strip().lower()
        kind = str(arguments.get("kind") or "").strip()
        limit = _as_int(arguments, "max_results", 200, low=1, high=500)

        if not intel.is_indexed:
            intel.index()
        # ``path`` may name a directory to map, or one file to report on. Treating
        # a file as a directory prefix silently returned nothing at all, which reads
        # as "this file defines no symbols" rather than "you asked the wrong way".
        target = "" if root in (".", "") else root.rstrip("/")
        exact_file = bool(target) and self._workspace.is_file(target)

        symbols: list[dict[str, object]] = []
        total = 0
        truncated = False
        unparsable: list[str] = []
        considered = 0
        for path in intel.map.known_paths():
            if exact_file:
                if path != target:
                    continue
            elif target and not path.startswith(f"{target}/"):
                continue
            considered += 1
            facts = intel.map.facts(path)
            if facts.unparsable:
                unparsable.append(path)
                continue
            for symbol in facts.symbols:
                if kind and symbol.kind != kind:
                    continue
                if query and query not in symbol.qualified.lower():
                    continue
                total += 1
                if len(symbols) < limit:
                    symbols.append({"path": path, **symbol.to_dict()})
                else:
                    truncated = True
                    break
            if truncated:
                break

        described = "".join(
            part
            for part in (
                f" matching '{query}'" if query else "",
                f" of kind {kind}" if kind else "",
            )
        )
        return ToolResult.success(
            self.spec.name,
            f"{total} symbols{described} across {intel.indexed_files} indexed files",
            {
                "root": root,
                # Same keys as the scanning path: a caller must not have to know
                # which one answered.
                "source": "project index",
                "files_scanned": considered,
                "files_skipped": 0,
                "files_indexed": intel.indexed_files,
                "unparsable_files": unparsable[:20],
                "symbol_count": total,
                "returned": len(symbols),
                "truncated": truncated,
                "symbols": symbols,
            },
        )


class ProjectContextTool:
    """Bounded, task-oriented context for the project the agent is working in.

    The gap this fills: ``project_symbols`` answers "where is X defined" but
    nothing answers "what *is* this project" or "what should I read before I touch
    this file". Without it the agent walks the workspace a directory at a time,
    which is slow on a large project and unbounded in what it puts in the context.

    Deliberately *bounded and lexical*. It matches the words of a task against
    paths and symbol names -- the same evidence ``search_content`` uses, gathered
    once -- and says so in ``relevance`` rather than pretending to semantic
    understanding. Output is capped on every axis and ``truncated`` says when a
    cap was hit, because a model told "12 related files" when there were 300 will
    work with 12 and call it thorough.
    """

    def __init__(
        self,
        workspace: Workspace,
        intelligence: ProjectIntelligence,
        *,
        max_bytes: int = 2_000_000,
    ) -> None:
        self._workspace = workspace
        self._intelligence = intelligence
        self._max_bytes = max_bytes
        self.spec = ToolSpec(
            name="project_context",
            description=(
                "Describe this project and return the bounded context relevant to a task: "
                "what kind of project it is, where the source and tests live, the files "
                "and symbols that match, the tests covering them, and who imports the "
                "file you named. Use it first, before outlining files one by one, to "
                "understand the project. Results are capped and say so; ask again with a "
                "path or symbol to narrow down. Python projects only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "What you are about to do, in your own words. Matched against "
                            "file and symbol names to pick the most relevant files."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "A file you are about to change. Its importers and the tests "
                            "around it are reported, which is what an edit needs."
                        ),
                    },
                    "symbol": {
                        "type": "string",
                        "description": "A symbol name to centre the context on.",
                    },
                    "max_files": {
                        "type": "integer",
                        "description": "Maximum files to return (1-100, default 20).",
                    },
                    "max_symbols": {
                        "type": "integer",
                        "description": "Maximum symbols to return (1-400, default 40).",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="project context",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        task = str(arguments.get("task") or "")
        path = str(arguments.get("path") or "")
        symbol = str(arguments.get("symbol") or "")
        if not (task or path or symbol):
            return ToolResult.failure(
                self.spec.name,
                self.spec.summarize(arguments),
                "Give a task, a path or a symbol so the context has something to aim at.",
            )

        profile = self._intelligence.profile
        if not self._intelligence.is_indexed:
            # One bounded walk for the whole session, paid for by the first call
            # rather than by every one of them.
            self._intelligence.index()
        context = task_context(
            self._intelligence,
            task=task,
            path=path,
            symbol=symbol,
            max_files=_as_int(arguments, "max_files", 20, low=1, high=100),
            max_symbols=_as_int(arguments, "max_symbols", 40, low=1, high=400),
        )
        data = context.to_dict()
        data["relevance"] = (
            "lexical: task words matched against file and symbol names, plus import edges"
        )
        if not self._intelligence.is_indexed:
            data["index"] = {
                "state": "not indexed",
                "hint": "call again after a change for symbol-level detail",
            }
        else:
            data["index"] = {
                "state": "indexed",
                "files": self._intelligence.indexed_files,
                "pending_refresh": list(self._intelligence.stale_files)[:10],
            }
        return ToolResult.success(
            self.spec.name,
            f"{profile.kind} project, {len(context.files)} files and {len(context.symbols)} "
            f"symbols relevant{'' if not context.truncated else ' (truncated)'}",
            data,
        )


def build_code_tools(
    workspace: Workspace,
    *,
    max_file_bytes: int = 2_000_000,
    intelligence: ProjectIntelligence | None = None,
) -> list[Any]:
    """Instantiate the v0.3 code-navigation tools bound to ``workspace``.

    ``intelligence`` is the shared, session-scoped project map. Passing it is what
    makes repeated navigation cheap; when it is absent the tools keep working
    uncached, which is what an embedding caller gets by default.
    """

    tools: list[Any] = [
        CodeOutlineTool(workspace, max_bytes=max_file_bytes),
        FindReferencesTool(workspace, max_bytes=max_file_bytes),
    ]
    if intelligence is not None:
        tools.append(
            ProjectSymbolsTool(
                workspace,
                max_bytes=max_file_bytes,
                max_files=_MAX_SCANNED_FILES,
                intelligence=intelligence,
            )
        )
        tools.append(ProjectContextTool(workspace, intelligence, max_bytes=max_file_bytes))
    else:
        tools.append(ProjectSymbolsTool(workspace, max_bytes=max_file_bytes))
    return tools
