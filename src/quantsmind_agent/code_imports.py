"""Import records and workspace-relative resolution for Python modules.

``find_references`` already answers "where is this name used". That is not
enough to change an interface safely: when ``Store.save`` grows a parameter,
the files that matter are the ones that *import and call* it, and finding those
by name means re-reading every file and hoping.

This module resolves an import statement to a file inside the workspace, so the
agent can follow an import edge instead of guessing at one. It is deliberately
one level and name-based:

* an import is resolved by dotted path against the workspace root, the way
  Python would if the root were on ``sys.path``;
* relative imports (``from . import x``) are resolved against the *importing
  file's* package;
* nothing here infers types, follows re-exports or understands ``__all__``.

A resolver that claimed more than this would mislead the model into thinking a
change was safe when it was not. Everything it cannot resolve is returned as
unresolved rather than guessed, and the tools report that count.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from quantsmind_agent.errors import InvalidPathError
from quantsmind_agent.workspace import Workspace


def importers_of(
    workspace: Workspace,
    target_path: str,
    *,
    max_files: int = 400,
) -> tuple[list[dict[str, object]], bool]:
    """Files that import ``target_path``, and whether the scan was cut short.

    This is the reverse direction, and it is what an edit needs: after changing
    a module, the agent has to know which other files reach into it. Asking for
    it explicitly means remembering to ask, and the agent that forgets leaves a
    broken caller behind.

    Bounded on purpose -- a scan that walks the project on every edit would make
    the common case slow, so the file count is capped and the caller is told when
    the answer may be incomplete.
    """

    importers: list[dict[str, object]] = []
    truncated = False
    scanned = 0

    # One entry beyond the budget, so "we stopped early" is distinguishable from
    # "we looked at everything".
    for entry in workspace.walk(".", max_entries=max_files + 1):
        if scanned >= max_files:
            truncated = True
            break
        if not entry.path.endswith(".py") or entry.path == target_path:
            continue
        try:
            source = workspace.read_text(entry.path)
        except InvalidPathError:
            continue
        scanned += 1
        try:
            records = parse_imports(source)
        except SyntaxError:
            continue
        for record in records:
            for binding in resolve_import(workspace, record, entry.path):
                if binding.path == target_path:
                    importers.append(
                        {
                            "path": entry.path,
                            "line": binding.line,
                            "imported_name": binding.imported_name,
                            "bound_name": binding.bound_name,
                        }
                    )
    return importers, truncated


__all__ = [
    "ImportRecord",
    "ResolvedImport",
    "importers_of",
    "module_name_for",
    "parse_imports",
    "resolve_import",
]


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One ``import`` statement, as written.

    ``level`` is the leading-dot count for relative imports: 0 for ``import x``
    and ``from x import y``, 1 for ``from . import y``, 2 for ``from .. import y``.
    """

    module: str
    level: int
    names: tuple[tuple[str, str | None], ...]
    line: int
    is_from: bool

    @property
    def bare_module(self) -> str:
        """The dotted module without any leading dots."""

        return self.module


@dataclass(frozen=True, slots=True)
class ResolvedImport:
    """An import statement mapped onto a file in the workspace."""

    #: Workspace-relative file the import resolves to, when it resolves at all.
    path: str
    #: The name bound in the importing module, e.g. ``Store`` or ``store``.
    bound_name: str
    #: What was imported: ``from pkg.store import Store`` -> ``Store``.
    imported_name: str
    #: ``module`` for ``import pkg.store``, ``from_module`` for ``from ... import x``.
    kind: str
    line: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "bound_name": self.bound_name,
            "imported_name": self.imported_name,
            "kind": self.kind,
            "line": self.line,
        }


def parse_imports(source: str) -> list[ImportRecord]:
    """Every import statement in ``source``, in file order.

    Raises:
        SyntaxError: the source does not parse; callers report it per file
            rather than pretending the file has no imports.
    """

    tree = ast.parse(source)
    records: list[ImportRecord] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                records.append(
                    ImportRecord(
                        module=alias.name,
                        level=0,
                        names=((alias.name.split(".")[0], None),),
                        line=node.lineno,
                        is_from=False,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            names = tuple((alias.name, alias.asname) for alias in node.names)
            records.append(
                ImportRecord(
                    module=node.module or "",
                    level=node.level or 0,
                    names=names,
                    line=node.lineno,
                    is_from=True,
                )
            )

    records.sort(key=lambda record: record.line)
    return records


def module_name_for(workspace: Workspace, path: str) -> str:
    """The dotted module a workspace file would have if the root were on ``sys.path``.

    ``pkg/store.py`` becomes ``pkg.store``; ``pkg/__init__.py`` becomes ``pkg``.
    A file outside the root, or one that is not a ``.py`` file, becomes ``""``.
    """

    try:
        relative = workspace.relative(path)
    except InvalidPathError:
        # A path outside the workspace has no module here; callers treat that as
        # "cannot resolve" rather than as a project module.
        return ""
    if not relative.endswith(".py"):
        return ""
    without_suffix = relative[: -len(".py")]
    parts = [part for part in without_suffix.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_for(workspace: Workspace, path: str) -> str:
    """The dotted package containing ``path`` -- the anchor for relative imports.

    A package's own ``__init__.py`` is anchored to the package itself, not to its
    parent: ``pkg/__init__.py`` does ``from .store import x`` to mean
    ``pkg.store``, the same as ``pkg/reader.py`` does.
    """

    try:
        relative = workspace.relative(path)
    except InvalidPathError:
        return ""
    module = module_name_for(workspace, path)
    if not module:
        return ""
    name = relative.rsplit("/", 1)[-1]
    if name == "__init__.py":
        return module
    return module.rsplit(".", 1)[0] if "." in module else ""


def _candidate_paths(dotted: str) -> list[str]:
    """Files a dotted module could be, most specific first.

    ``pkg.store`` may be ``pkg/store.py`` or the package ``pkg/store/``. Both are
    legitimate and the caller reports whichever exists.
    """

    parts = [part for part in dotted.split(".") if part]
    if not parts:
        return []
    return ["/".join([*parts, "__init__.py"]), "/".join(parts) + ".py"]


def resolve_absolute(workspace: Workspace, dotted: str) -> str | None:
    """Resolve a dotted module against the workspace root, or ``None``."""

    for candidate in _candidate_paths(dotted):
        if workspace.is_file(candidate):
            return candidate
    return None


def resolve_import(
    workspace: Workspace,
    record: ImportRecord,
    importer_path: str,
) -> list[ResolvedImport]:
    """Map one import statement onto workspace files.

    Returns every binding the statement creates, which is usually one and is
    more than one for ``import a, b``. A statement that cannot be resolved
    returns an empty list, which is how the tool reports it honestly instead of
    guessing at a target.
    """

    if record.level:
        base = _package_for(workspace, importer_path)
        parts = base.split(".") if base else []
        # level=1 means the current package, so strip level-1 parts.
        if record.level > 1:
            parts = parts[: len(parts) - (record.level - 1)]
        elif record.level < 1:  # pragma: no cover - ast never yields this
            return []
        prefix = ".".join(parts)
        target = f"{prefix}.{record.module}" if record.module else prefix
    else:
        target = record.bare_module

    if not target:
        return []

    if not record.is_from:
        # ``import pkg.store`` binds the top-level name ``pkg`` in the importer,
        # but the module it names lives in a file the agent wants to see.
        resolved = resolve_absolute(workspace, target)
        if resolved is None:
            return []
        return [
            ResolvedImport(
                path=resolved,
                bound_name=target.split(".")[0],
                imported_name=target,
                kind="module",
                line=record.line,
            )
        ]

    resolved = resolve_absolute(workspace, target)
    if resolved is None:
        # ``from pkg.store import name`` may still point at a symbol inside the
        # module even when the module is a package that re-exports it.
        return []

    return [
        ResolvedImport(
            path=resolved,
            bound_name=asname or name,
            imported_name=name,
            kind="from_module",
            line=record.line,
        )
        for name, asname in record.names
        if name != "*"
    ]
