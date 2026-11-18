"""Workspace explorer: directory listing, quick open and text search.

The service is the only place that walks a project for the workbench, and it is
bounded in both time and memory: listings are capped, walks stop at a configured
limit, ignored directories are never entered, and every long operation accepts a
cancellation event. It contains no Qt so it can run on a worker thread.
"""

from __future__ import annotations

import fnmatch
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from quantsmind_agent.errors import InvalidPathError, QuantsMindError
from quantsmind_agent.services.workspace.containment import FileEntry, Workspace

__all__ = [
    "ExplorerEntry",
    "ExplorerService",
    "SearchMatch",
    "SearchOutcome",
    "SearchQuery",
]

#: Default ceiling for one recursive walk. Large enough for real projects,
#: small enough that the GUI never has to wait for an unbounded scan.
DEFAULT_WALK_LIMIT = 20_000

#: Refuse to search a single file larger than this; it is almost always a
#: generated artifact and would make one file dominate the whole search.
SEARCH_FILE_LIMIT = 2_000_000


@dataclass(frozen=True, slots=True)
class ExplorerEntry:
    """One row in the explorer tree."""

    path: str
    name: str
    is_dir: bool
    size: int
    modified: str
    hidden: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "name": self.name,
            "type": "directory" if self.is_dir else "file",
            "size": self.size,
            "modified": self.modified,
            "hidden": self.hidden,
        }


@dataclass(frozen=True, slots=True)
class QuickOpenResult:
    path: str
    name: str
    score: int


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """One user search. Options mirror the search panel."""

    text: str
    include: str = ""
    exclude: str = ""
    case_sensitive: bool = False
    whole_word: bool = False
    use_regex: bool = False
    max_results: int = 200


@dataclass(frozen=True, slots=True)
class SearchMatch:
    path: str
    line: int
    column: int
    preview: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "preview": self.preview,
        }


@dataclass(slots=True)
class SearchOutcome:
    matches: list[SearchMatch] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False
    cancelled: bool = False


class ExplorerService:
    """Bounded listing and search over one :class:`Workspace`."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        walk_limit: int = DEFAULT_WALK_LIMIT,
        search_file_limit: int = SEARCH_FILE_LIMIT,
    ) -> None:
        self._workspace = workspace
        self._walk_limit = max(1, walk_limit)
        self._search_file_limit = max(1, search_file_limit)

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    # -- listing -----------------------------------------------------------
    def list_directory(
        self,
        relative: str = ".",
        *,
        include_hidden: bool = False,
    ) -> list[ExplorerEntry]:
        """Direct children, directories first, ignored directories omitted."""

        entries = self._workspace.list_dir(relative)
        result: list[ExplorerEntry] = []
        for entry in entries:
            hidden = entry.name.startswith(".")
            if hidden and not include_hidden:
                continue
            result.append(self._entry(entry, hidden))
        return result

    def _entry(self, entry: FileEntry, hidden: bool) -> ExplorerEntry:
        return ExplorerEntry(
            path=entry.path,
            name=entry.name,
            is_dir=entry.is_dir,
            size=entry.size,
            modified=entry.modified,
            hidden=hidden,
        )

    def search_text(
        self,
        query: SearchQuery,
        *,
        cancel: threading.Event | None = None,
    ) -> SearchOutcome:
        """Search file contents, respecting workspace exclusions.

        Results are capped. ``truncated`` tells the panel to ask the user to
        refine the query instead of implying the project has no more matches.
        """

        text = query.text
        outcome = SearchOutcome()
        if not text:
            return outcome
        pattern = self._compile(query)

        try:
            candidates = self._workspace.walk(max_entries=self._walk_limit)
        except (QuantsMindError, OSError) as exc:
            raise QuantsMindError(
                f"Search could not list the workspace: {exc}",
                user_message="The project could not be searched. Try refreshing the explorer.",
            ) from exc

        for entry in candidates:
            if cancel is not None and cancel.is_set():
                outcome.cancelled = True
                return outcome
            if entry.is_dir:
                continue
            if not self._path_included(entry.path, query):
                continue
            try:
                path = self._workspace.resolve(entry.path)
            except InvalidPathError:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > self._search_file_limit:
                continue
            outcome.files_scanned += 1
            try:
                with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                    for number, line in enumerate(handle, start=1):
                        if cancel is not None and cancel.is_set():
                            outcome.cancelled = True
                            return outcome
                        match = pattern.search(line)
                        if match is None:
                            continue
                        outcome.matches.append(
                            SearchMatch(
                                path=entry.path,
                                line=number,
                                column=match.start() + 1,
                                preview=line.rstrip("\r\n")[:400],
                            )
                        )
                        if len(outcome.matches) >= query.max_results:
                            outcome.truncated = True
                            return outcome
            except (OSError, UnicodeError):
                continue
        return outcome

    def _compile(self, query: SearchQuery) -> re.Pattern[str]:
        text = query.text
        if query.use_regex:
            try:
                expression = text
                if query.whole_word:
                    expression = rf"\b(?:{expression})\b"
                flags = 0 if query.case_sensitive else re.IGNORECASE
                return re.compile(expression, flags)
            except re.error as exc:
                raise QuantsMindError(
                    f"Invalid search expression: {exc}",
                    user_message=f"The search pattern is not valid: {exc}.",
                ) from exc
        expression = re.escape(text)
        if query.whole_word:
            expression = rf"\b{expression}\b"
        return re.compile(expression, 0 if query.case_sensitive else re.IGNORECASE)

    def _path_included(self, relative: str, query: SearchQuery) -> bool:
        path = relative.replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        if query.include and not (
            fnmatch.fnmatch(path, query.include) or fnmatch.fnmatch(name, query.include)
        ):
            return False
        return not (
            query.exclude
            and (fnmatch.fnmatch(path, query.exclude) or fnmatch.fnmatch(name, query.exclude))
        )

    # -- quick open --------------------------------------------------------
    def quick_open(
        self,
        query: str,
        *,
        limit: int = 100,
        cancel: threading.Event | None = None,
        recent: Iterable[str] = (),
    ) -> list[QuickOpenResult]:
        """Rank workspace files by a case-insensitive fuzzy filename match."""

        needle = query.strip().casefold()
        recent_rank = {path: index for index, path in enumerate(recent)}
        results: list[QuickOpenResult] = []
        try:
            candidates = self._workspace.walk(max_entries=self._walk_limit)
        except (QuantsMindError, OSError):
            return []
        for entry in candidates:
            if cancel is not None and cancel.is_set():
                return []
            if entry.is_dir:
                continue
            score = self._score(needle, entry.path)
            if score is None:
                continue
            if entry.path in recent_rank:
                score += 1000 - min(recent_rank[entry.path], 999)
            results.append(QuickOpenResult(entry.path, entry.name, score))
        results.sort(key=lambda item: (-item.score, item.path.lower()))
        return results[: max(1, limit)]

    @staticmethod
    def _score(needle: str, path: str) -> int | None:
        if not needle:
            return 0
        haystack = path.casefold()
        name = haystack.rsplit("/", 1)[-1]
        if name == needle:
            return 600
        if name.startswith(needle):
            return 500
        if needle in name:
            return 400 - name.index(needle)
        if needle in haystack:
            return 300 - haystack.index(needle)
        # Subsequence match: "qmwin" finds "quantsmind/window.py".
        position = 0
        gaps = 0
        for character in needle:
            found = haystack.find(character, position)
            if found < 0:
                return None
            gaps += found - position
            position = found + 1
        return max(1, 200 - gaps)

    # -- helpers -----------------------------------------------------------
    def file_size(self, relative: str) -> int:
        return self._workspace.file_size(relative)

    def read_text(self, relative: str, *, max_bytes: int | None = None) -> str:
        return self._workspace.read_text(relative, max_bytes=max_bytes)

    def canonical_path(self, relative: str) -> Path:
        return self._workspace.resolve(relative)
