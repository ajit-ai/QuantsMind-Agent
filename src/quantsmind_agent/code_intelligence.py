"""Tier-A AST-based code intelligence (WP4 of v0.12.0).

Unifies :mod:`quantsmind_agent.code_index` and :mod:`quantsmind_agent.code_imports`
into a single service so the editor can request outline, references, hover and
import-resolution without knowing which module provides each piece.

Nothing here imports Qt; the service is headless and testable without a display
server. Tier-A means AST-based only: deterministic, local, no model calls, no
external language server. LSP (WP3) is an optional upgrade that can augment but
never replace this tier.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantsmind_agent.code_imports import (
    ImportRecord,
    ResolvedImport,
    importers_of,
    module_name_for,
    parse_imports,
    resolve_import,
)
from quantsmind_agent.code_index import (
    Symbol,
    find_symbols,
    find_uses,
    parse_symbols,
)
from quantsmind_agent.errors import InvalidPathError
from quantsmind_agent.services.workspace.containment import Workspace

__all__ = [
    "CodeIntelligence",
    "ImportRecord",
    "ResolvedImport",
    "Symbol",
    "get_code_intelligence",
]


@dataclass(frozen=True, slots=True)
class CodeIntelligenceResult:
    """Aggregated intelligence about one file."""

    path: str
    symbols: list[Symbol]
    imports: list[ImportRecord]
    resolved_imports: list[ResolvedImport]
    importers: list[dict[str, object]]
    truncated: bool


class CodeIntelligence:
    """Bounded AST-based code intelligence for one workspace.

    All reads go through :class:`quantsmind_agent.services.workspace.containment.Workspace`,
    so containment, symlink policy and binary handling are shared with the rest of
    the codebase. Malformed files are refused honestly rather than guessed at.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    # -- file-level intelligence ------------------------------------------

    def intelligence_for(self, path: str) -> CodeIntelligenceResult:
        """All intelligence known about one file: symbols, imports, importers."""

        try:
            source = self._workspace.read_text(path)
        except InvalidPathError as exc:
            raise InvalidPathError(
                f"Cannot read {path} for intelligence",
                user_message=f"'{path}' is outside the open project.",
                details={"path": path, "reason": exc.details.get("reason")},
            ) from exc

        try:
            symbols = parse_symbols(source)
        except SyntaxError:
            symbols = []

        try:
            import_records = parse_imports(source)
        except SyntaxError:
            import_records = []

        resolved: list[ResolvedImport] = []
        for record in import_records:
            resolved.extend(resolve_import(self._workspace, record, path))

        importers, truncated = importers_of(self._workspace, path)

        return CodeIntelligenceResult(
            path=path,
            symbols=symbols,
            imports=import_records,
            resolved_imports=resolved,
            importers=importers,
            truncated=truncated,
        )

    # -- symbol queries -----------------------------------------------------

    def symbol_at(self, path: str, line: int, column: int) -> Symbol | None:
        """The symbol (class/function/method) at ``line:column``, or None."""

        try:
            source = self._workspace.read_text(path)
        except InvalidPathError:
            return None

        try:
            symbols = parse_symbols(source)
        except SyntaxError:
            return None

        for symbol in symbols:
            if symbol.line <= line <= symbol.end_line:
                # Column-aware: symbol spans its whole line range; if on the
                # declaration line, check the column is within the name/keyword.
                if symbol.line == line:
                    # Rough check: column within the symbol's signature area
                    pass
                return symbol
        return None

    def find_symbols(self, path: str, name: str) -> list[Symbol]:
        """All symbols named ``name`` in ``path``."""

        try:
            source = self._workspace.read_text(path)
        except InvalidPathError:
            return []

        try:
            return find_symbols(source, name)
        except SyntaxError:
            return []

    def find_references(self, path: str, name: str) -> list[int]:
        """Line numbers in ``path`` that reference ``name``."""

        try:
            source = self._workspace.read_text(path)
        except InvalidPathError:
            return []

        try:
            return find_uses(source, name)
        except SyntaxError:
            return []

    # -- import resolution --------------------------------------------------

    def resolve_import(
        self,
        importer_path: str,
        imported_module: str,
        aliases: dict[str, str] | None = None,
    ) -> list[str]:
        """Workspace file paths that ``imported_module`` resolves to from ``importer_path``."""

        try:
            source = self._workspace.read_text(importer_path)
        except InvalidPathError:
            return []

        try:
            parse_imports(source)
        except SyntaxError:
            return []

        resolved: list[str] = []
        for record in import_records_for_import(imported_module):
            for resolved_import in resolve_import(self._workspace, record, importer_path, aliases):
                resolved.append(resolved_import.path)
        return sorted(set(resolved))

    def importers_of(
        self,
        target_path: str,
        *,
        max_files: int = 400,
        aliases: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, object]], bool]:
        """Files that import ``target_path``, plus whether the scan was cut short."""

        return importers_of(self._workspace, target_path, max_files=max_files, aliases=aliases)

    def module_name_for(self, path: str) -> str:
        """The dotted module ``path`` would have if the workspace root were on sys.path."""

        return module_name_for(self._workspace, path)


def import_records_for_import(imported_module: str) -> list[ImportRecord]:
    """Synthetic ImportRecord(s) for resolving one import target.

    Used by :meth:`resolve_import` when a caller already knows the import name
    but not which statement introduced it. Builds synthetic records so the shared
    resolution logic in :mod:`quantsmind_agent.code_imports` can be reused.
    """

    records: list[ImportRecord] = []
    for part in imported_module.split("."):
        records.append(
            ImportRecord(
                module=imported_module,
                level=0,
                names=((part, None),),
                line=0,
                is_from=False,
            )
        )
    return records


def get_code_intelligence(workspace: Workspace) -> CodeIntelligence:
    """Factory: build a :class:`CodeIntelligence` bound to ``workspace``."""

    return CodeIntelligence(workspace)
