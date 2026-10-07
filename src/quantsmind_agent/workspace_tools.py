"""The v0.1 workspace tool set.

Four read-only tools, all requiring :attr:`Permission.READ_WORKSPACE`:

``list_directory``
    Direct children of a folder, directories first.
``read_file``
    Bounded, UTF-8-tolerant read of one text file.
``search_content``
    Case-insensitive literal or regex search across text files.
``project_structure``
    Compact tree of the project up to a configurable depth.

None of them accept absolute paths; every path argument is workspace-relative
and validated by :class:`~quantsmind_agent.workspace.Workspace`.

File *writes* are intentionally reserved for v0.2 -- see
``docs/source/releases/v0.1.rst``.
"""

from __future__ import annotations

import re
from typing import Any

from quantsmind_agent.errors import InvalidPathError
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.permissions import Permission
from quantsmind_agent.tools import ToolResult, ToolSpec
from quantsmind_agent.workspace import Workspace

__all__ = [
    "ListDirectoryTool",
    "ProjectStructureTool",
    "ReadFileTool",
    "SearchContentTool",
    "build_default_tools",
]

logger = get_logger(__name__)

_MAX_SEARCH_MATCHES = 200
_MAX_LINE_LENGTH = 300


def _as_int(arguments: dict[str, Any], key: str, default: int, *, low: int, high: int) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, int(value)))


class ListDirectoryTool:
    """List the direct children of a workspace directory."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="list_directory",
            description=(
                "List the files and folders directly inside a workspace directory. "
                "Use '.' for the project root. Paths are relative to the workspace root."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory path, '.' for the root.",
                        "default": ".",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "Maximum number of entries to return (1-500).",
                        "default": 200,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="list {path}",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        path = str(arguments.get("path") or ".")
        limit = _as_int(arguments, "max_entries", 200, low=1, high=500)
        try:
            entries = self._workspace.list_dir(path)
        except InvalidPathError as exc:
            return ToolResult.failure(self.spec.name, f"list {path}", exc.user_message)

        shown = entries[:limit]
        directories = [entry.path for entry in shown if entry.is_dir]
        files = [entry.path for entry in shown if not entry.is_dir]
        return ToolResult.success(
            self.spec.name,
            f"listed {path} ({len(directories)} folders, {len(files)} files)",
            {
                "path": self._workspace.relative(path),
                "total": len(entries),
                "truncated": len(entries) > limit,
                "entries": [entry.to_dict() for entry in shown],
            },
        )


class ReadFileTool:
    """Read a UTF-8 text file from the workspace, with line numbers."""

    def __init__(self, workspace: Workspace, *, max_bytes: int = 200_000) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self.spec = ToolSpec(
            name="read_file",
            description=(
                "Read a text file from the workspace and return its contents with line "
                "numbers. Paths are relative to the workspace root. Binary files cannot "
                "be read."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path of the file to read.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to return (1-based). Default 1.",
                        "default": 1,
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum number of lines to return (1-2000).",
                        "default": 800,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="read {path}",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        path = str(arguments.get("path") or "")
        start = _as_int(arguments, "start_line", 1, low=1, high=1_000_000)
        limit = _as_int(arguments, "max_lines", 800, low=1, high=2000)
        summary = f"read {path or '(no path)'}"

        if not path:
            return ToolResult.failure(self.spec.name, summary, "No file path was provided.")

        try:
            content = self._workspace.read_text(path, max_bytes=self._max_bytes)
        except InvalidPathError as exc:
            return ToolResult.failure(self.spec.name, summary, exc.user_message)

        all_lines = content.splitlines()
        window = all_lines[start - 1 : start - 1 + limit]
        numbered = "\n".join(f"{start + offset:>5}\t{line}" for offset, line in enumerate(window))
        return ToolResult.success(
            self.spec.name,
            f"read {self._workspace.relative(path)} ({len(window)} lines)",
            {
                "path": self._workspace.relative(path),
                "total_lines": len(all_lines),
                "start_line": start,
                "returned_lines": len(window),
                "truncated": len(window) < len(all_lines[start - 1 :]),
                "content": numbered,
            },
        )


class SearchContentTool:
    """Search file contents for a literal string or a regular expression."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="search_content",
            description=(
                "Search the workspace for a text pattern. Set use_regex=true for regular "
                "expressions, otherwise the pattern is matched literally "
                "(case-insensitive). Returns matching file paths, line numbers and lines."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text or regex to search for."},
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory to search. Default '.'.",
                        "default": ".",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Glob restricting the file names, e.g. '*.py'.",
                    },
                    "use_regex": {
                        "type": "boolean",
                        "description": "Treat the pattern as a regular expression.",
                        "default": False,
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive matching. Default true.",
                        "default": True,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matches (1-200).",
                        "default": 50,
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="search {pattern!r} in {path}",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        pattern = str(arguments.get("pattern") or "")
        root = str(arguments.get("path") or ".")
        glob = arguments.get("file_pattern")
        use_regex = bool(arguments.get("use_regex", False))
        ignore_case = bool(arguments.get("ignore_case", True))
        limit = _as_int(arguments, "max_results", 50, low=1, high=_MAX_SEARCH_MATCHES)
        summary = f"search {pattern!r} in {root}"

        if not pattern:
            return ToolResult.failure(self.spec.name, summary, "No search pattern was provided.")

        matcher = self._compile(pattern, use_regex, ignore_case)
        if matcher is None:
            return ToolResult.failure(
                self.spec.name, summary, f"The regular expression {pattern!r} is not valid."
            )

        file_pattern = str(glob) if isinstance(glob, str) and glob else "*"
        matches: list[dict[str, Any]] = []
        files_scanned = 0
        try:
            entries = list(self._workspace.walk(root, max_entries=8000))
        except InvalidPathError as exc:
            return ToolResult.failure(self.spec.name, summary, exc.user_message)

        import fnmatch

        for entry in entries:
            if not fnmatch.fnmatch(entry.name, file_pattern):
                continue
            try:
                content = self._workspace.read_text(entry.path, max_bytes=1_000_000)
            except InvalidPathError:
                continue
            files_scanned += 1
            for number, line in enumerate(content.splitlines(), start=1):
                if matcher.search(line):
                    matches.append(
                        {
                            "path": entry.path,
                            "line": number,
                            "text": line[:_MAX_LINE_LENGTH],
                        }
                    )
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break

        return ToolResult.success(
            self.spec.name,
            f"search {pattern!r} in {root}: {len(matches)} match(es) in {files_scanned} file(s)",
            {
                "pattern": pattern,
                "root": self._workspace.relative(root),
                "files_scanned": files_scanned,
                "match_count": len(matches),
                "truncated": len(matches) >= limit,
                "matches": matches,
            },
        )

    @staticmethod
    def _compile(pattern: str, use_regex: bool, ignore_case: bool) -> re.Pattern[str] | None:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            if use_regex:
                return re.compile(pattern, flags)
            return re.compile(re.escape(pattern), flags)
        except re.error:
            return None


class ProjectStructureTool:
    """Render a bounded tree of the workspace."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="project_structure",
            description=(
                "Show the project layout as a compact tree. Start here to understand a "
                "codebase before reading individual files. Paths are relative to the "
                "workspace root."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory to inspect. Default '.'.",
                        "default": ".",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "How many directory levels to descend (1-6).",
                        "default": 3,
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "Maximum number of lines to return (1-400).",
                        "default": 200,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="structure of {path}",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        root = str(arguments.get("path") or ".")
        depth = _as_int(arguments, "max_depth", 3, low=1, high=6)
        limit = _as_int(arguments, "max_entries", 200, low=1, high=400)
        summary = f"structure of {root}"

        lines: list[str] = []
        truncated = False
        try:
            root_path = self._workspace.resolve(root)
        except InvalidPathError as exc:
            return ToolResult.failure(self.spec.name, summary, exc.user_message)
        if not root_path.is_dir():
            return ToolResult.failure(
                self.spec.name, summary, f"'{root}' is not a directory in this workspace."
            )

        lines.append(self._workspace.relative(root) or ".")
        try:
            truncated = self._render(root, "", 0, depth, lines, limit)
        except InvalidPathError as exc:
            return ToolResult.failure(self.spec.name, summary, exc.user_message)

        directories = sum(1 for line in lines[1:] if line.rstrip().endswith("/"))
        files = len(lines) - 1 - directories
        return ToolResult.success(
            self.spec.name,
            f"structure of {self._workspace.relative(root)} ({files} files, {directories} folders)",
            {
                "root": self._workspace.relative(root),
                "max_depth": depth,
                "entry_count": len(lines) - 1,
                "truncated": truncated,
                "tree": "\n".join(lines),
            },
        )

    def _render(
        self,
        directory: Any,
        prefix: str,
        level: int,
        max_depth: int,
        lines: list[str],
        limit: int,
    ) -> bool:
        if level >= max_depth or len(lines) > limit:
            return False
        try:
            entries = self._workspace.list_dir(directory)
        except InvalidPathError:
            return False

        visible = entries[: max(0, limit - len(lines) + 1)]
        truncated = len(entries) > len(visible)

        for index, entry in enumerate(visible):
            last = index == len(visible) - 1 and not truncated
            connector = "`-- " if last else "|-- "
            suffix = "/" if entry.is_dir else ""
            lines.append(f"{prefix}{connector}{entry.name}{suffix}")
            if entry.is_dir:
                child_prefix = prefix + ("    " if last else "|   ")
                if self._render(entry.path, child_prefix, level + 1, max_depth, lines, limit):
                    return True
            if len(lines) > limit:
                return True
        return truncated


def build_default_tools(workspace: Workspace, *, max_read_bytes: int = 200_000) -> list[Any]:
    """Instantiate the v0.1 tool set bound to ``workspace``."""

    return [
        ListDirectoryTool(workspace),
        ReadFileTool(workspace, max_bytes=max_read_bytes),
        SearchContentTool(workspace),
        ProjectStructureTool(workspace),
    ]
