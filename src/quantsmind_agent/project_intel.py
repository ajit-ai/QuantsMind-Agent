"""Persistent, bounded, deterministic understanding of a project.

The navigation tools answer questions about a *file*: outline this, find that
symbol. They answer nothing about the project, so answering "where are the
tests?" means walking the workspace, and asking twice means walking it twice.
That is slow, and worse, it is unbounded: the cost of a question grows with the
size of the repository rather than with the size of the answer.

This module answers the project-level questions once, caches the answer for the
lifetime of the project, and invalidates the smallest part that can have changed:

* :func:`discover` reads the layout and classifies the project, reporting what it
  is confident about and what it could not determine rather than guessing.
* :class:`ProjectMap` holds the per-file symbols and import edges.
* :class:`ProjectIntelligence` owns both, and is told about agent mutations so a
  file the agent just wrote is never answered from a stale record.

Design constraints, all deliberate:

* **No embeddings, no index service, no daemon, no network.** Everything here is
  :func:`ast` and the filesystem, which the workspace already mediates.
* **Reuse, not duplication.** Symbols and imports come from
  :mod:`quantsmind_agent.code_index` and :mod:`quantsmind_agent.code_imports`, the
  same parsers the navigation tools use. A second parser would eventually disagree
  with the first.
* **Bounded.** Every walk has a budget, and every result says whether it hit it.
* **Never stale, or visibly so.** A record for a file that has not been
  invalidated is returned; one that has been invalidated is re-read, not guessed.

Nothing here is persisted. A project map is derived entirely from the filesystem,
so caching it across restarts would mean caching a claim about files that have
almost certainly changed -- see :mod:`quantsmind_agent.conversations` for what *is*
stored, and why it is stable metadata only.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quantsmind_agent.code_imports import module_name_for, parse_imports, resolve_import
from quantsmind_agent.code_index import Symbol, parse_symbols
from quantsmind_agent.errors import InvalidPathError
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.workspace import Workspace

__all__ = [
    "FileFacts",
    "ProjectIntelligence",
    "ProjectMap",
    "ProjectProfile",
    "TaskContext",
    "discover",
    "task_context",
]

logger = get_logger(__name__)

#: Python source suffix. Kept in one place so every walker agrees.
PYTHON_SUFFIX = ".py"

#: Directory names that are conventionally the test root.
_TEST_DIR_NAMES = ("tests", "test", "testing")

#: Documentation roots.
_DOC_DIR_NAMES = ("docs", "doc", "documentation")

#: Files that describe how the project is built, tested, packaged or run, keyed
#: by a short role name so the profile can say *why* a file matters.
_BUILD_FILES: dict[str, tuple[str, ...]] = {
    "packaging": ("pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in"),
    "dependencies": ("requirements.txt", "requirements-dev.txt"),
    "testing": ("pytest.ini", "tox.ini", "noxfile.py"),
    "automation": ("Makefile", "makefile", "GNUmakefile", "justfile"),
    "container": ("Dockerfile", "docker-compose.yml", "compose.yaml"),
    "formatting": (".pre-commit-config.yaml", ".editorconfig"),
}

#: Requirement files are matched by prefix, not by exact name.
_REQUIREMENT_PREFIX = "requirements"

#: Root-level names that are checkout metadata, not project content.
_VCS_MARKERS = (".git", ".hg", ".svn")

#: Directories that are never a source root, whatever else they contain.
_NON_SOURCE_DIRS = frozenset(
    {
        "tests",
        "test",
        "testing",
        "docs",
        "doc",
        "documentation",
        "examples",
        "example",
        "benchmarks",
        "build",
        "dist",
        "venv",
        "node_modules",
        "__pycache__",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectProfile:
    """What kind of project this is, and how sure we are.

    ``kind`` is deliberately coarse -- ``python-package``, ``python-application``
    or ``unknown`` -- because a wrong guess at a finer label is worse than an
    honest coarse one. ``confidence`` is derived from how many independent signals
    agree, and ``unknowns`` lists what could not be determined, so a caller can
    tell "it is not a package" apart from "we could not tell".
    """

    kind: str
    confidence: float
    signals: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    source_roots: tuple[str, ...] = ()
    test_roots: tuple[str, ...] = ()
    doc_roots: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    build_files: dict[str, str] = field(default_factory=dict)
    entry_points: tuple[tuple[str, str], ...] = ()
    vcs: str = ""
    file_count: int = 0
    truncated: bool = False

    @property
    def is_python(self) -> bool:
        return self.kind.startswith("python")

    @property
    def layout(self) -> str:
        """``src`` when sources live under ``src/``, otherwise ``flat``."""

        if any(root == "src" or root.startswith("src/") for root in self.source_roots):
            return "src"
        return "flat"

    @property
    def primary_source_root(self) -> str:
        """The source root an agent should look in first, or ``"."``."""

        for candidate in ("src", "."):
            if candidate == "src" and "src" not in self.source_roots:
                continue
            if candidate == "." and "." not in self.source_roots:
                continue
            return candidate
        return self.source_roots[0] if self.source_roots else "."

    def build_file(self, role: str) -> str:
        """The file filling ``role``, or ``""`` when there is none."""

        return self.build_files.get(role, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "confidence": round(self.confidence, 2),
            "signals": list(self.signals),
            "unknowns": list(self.unknowns),
            "languages": list(self.languages),
            "layout": self.layout,
            "source_roots": list(self.source_roots),
            "test_roots": list(self.test_roots),
            "doc_roots": list(self.doc_roots),
            "packages": list(self.packages),
            "build_files": dict(self.build_files),
            "entry_points": [
                {"command": name, "target": target} for name, target in self.entry_points
            ],
            "vcs": self.vcs,
            "file_count": self.file_count,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class FileFacts:
    """What is known about one file: its symbols, imports and module name."""

    path: str
    symbols: tuple[Symbol, ...] = ()
    imports: tuple[str, ...] = ()
    module: str = ""
    unparsable: bool = False

    @property
    def symbol_count(self) -> int:
        return len(self.symbols)


class ProjectMap:
    """Bounded per-file facts, with the reverse import index kept consistent.

    Both directions matter and cost different things: symbols answer "what is in
    this file", importers answer "what breaks if I change it". The reverse index
    is maintained incrementally, because rebuilding it after every edit would put a
    full project walk back on the hot path.
    """

    def __init__(self, workspace: Workspace, *, max_bytes: int = 2_000_000) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self._facts: dict[str, FileFacts] = {}
        self._importers: dict[str, set[str]] = {}
        self._stale: set[str] = set()
        #: Importable name -> real directory, supplied by the owner once the
        #: layout is known. Kept here rather than passed per-call because the
        #: whole point is that resolving a file's imports is not a per-call cost.
        self._aliases: dict[str, str] = {}
        self.generation = 0

    def set_aliases(self, aliases: dict[str, str]) -> None:
        """Record import-name to directory aliases and drop the affected edges.

        Changing where a package lives changes which imports resolve, so the
        cached edges cannot survive it.
        """

        if aliases == self._aliases:
            return
        self._aliases = dict(aliases)
        self.invalidate_all()

    # -- reading -----------------------------------------------------------
    def facts(self, path: str) -> FileFacts:
        """Current facts for ``path``, re-reading it if it was invalidated.

        The re-read is what makes the cache safe: an invalidated file is never
        answered from the previous record.
        """

        if path in self._stale or path not in self._facts:
            self.refresh_file(path)
        return self._facts.get(path, FileFacts(path=path))

    def has(self, path: str) -> bool:
        return path in self._facts and path not in self._stale

    def symbols(self, path: str) -> tuple[Symbol, ...]:
        return self.facts(path).symbols

    def importers_of(self, path: str) -> tuple[str, ...]:
        """Files that import ``path``, sorted so the answer is deterministic.

        Stale files are re-read first. An edge contributed by a file the agent has
        just written is either new or gone, and answering from the previous record
        is how a deleted caller keeps being reported.
        """

        self._refresh_stale()
        return tuple(sorted(self._importers.get(path, set())))

    def _refresh_stale(self) -> None:
        """Fold every invalidated file back into the reverse index.

        Bounded by construction: only files that were actually invalidated are
        re-read, and invalidation happens on writes rather than on queries.
        """

        for path in list(self._stale):
            self.refresh_file(path)

    def dependents_of(self, path: str) -> tuple[str, ...]:
        """Alias of :meth:`importers_of`, named for how callers think about it."""

        return self.importers_of(path)

    def known_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._facts))

    @property
    def stale_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._stale))

    @property
    def size(self) -> int:
        return len(self._facts)

    # -- invalidation ------------------------------------------------------
    def note_changed(self, path: str) -> None:
        """A file's content changed, so its own facts are now suspect.

        Only that file is invalidated. Its edges are repaired on the next read
        rather than the whole index being rebuilt, so an edit to one module costs
        one re-read instead of a project scan.
        """

        self._stale.add(path)
        self.generation += 1

    def note_created(self, path: str) -> None:
        self.note_changed(path)

    def note_deleted(self, path: str) -> None:
        """A file is gone: drop it and every edge that pointed at it.

        A stale reverse edge would make the agent believe a deleted module still
        has callers, which is exactly the confident wrong answer this layer exists
        to avoid.
        """

        self._stale.add(path)
        self._facts.pop(path, None)
        for target in [key for key, value in self._importers.items() if path in value]:
            self._importers[target].discard(path)
        for importers in self._importers.values():
            importers.discard(path)
        self.generation += 1

    def invalidate_all(self) -> None:
        """Drop everything. Used when the workspace itself changes."""

        self._facts.clear()
        self._importers.clear()
        self._stale.clear()
        self.generation += 1

    def drop(self, path: str) -> None:
        """Forget a file without asserting it still exists."""

        self.note_deleted(path)

    # -- population --------------------------------------------------------
    def refresh_file(self, path: str) -> FileFacts:
        """Re-read one file and fold its edges into the reverse index."""

        self._stale.discard(path)
        if not self._workspace.is_file(path):
            self._forget(path)
            return FileFacts(path=path)

        source = self._read(path)
        facts = FileFacts(path=path) if source is None else self._parse(path, source)
        self._forget(path)
        self._facts[path] = facts
        for target in facts.imports:
            self._importers.setdefault(target, set()).add(path)
        return facts

    def load(self, paths: list[str]) -> int:
        """Populate from a list of paths. Returns how many were read."""

        loaded = 0
        for path in paths:
            self.refresh_file(path)
            loaded += 1
        return loaded

    def _forget(self, path: str) -> None:
        """Remove ``path``'s contribution to the reverse index."""

        self._facts.pop(path, None)
        for target in [key for key, value in self._importers.items() if path in value]:
            self._importers[target].discard(path)

    def _read(self, path: str) -> str | None:
        try:
            return self._workspace.read_text(path, max_bytes=self._max_bytes)
        except (InvalidPathError, OSError, UnicodeDecodeError) as exc:
            logger.debug("Could not read %s for the project map: %s", path, exc)
            return None

    def _parse(self, path: str, source: str) -> FileFacts:
        try:
            symbols = tuple(parse_symbols(source))
        except SyntaxError:
            # A file being edited mid-turn will not parse. That is a normal state,
            # not a failure: it is recorded so a caller can say "unparsable" rather
            # than reporting a confident empty result.
            return FileFacts(path=path, unparsable=True)
        return FileFacts(
            path=path,
            symbols=symbols,
            imports=self._imports(path, source),
            module=self._module(path),
        )

    def _module(self, path: str) -> str:
        try:
            return module_name_for(self._workspace, path)
        except InvalidPathError:  # pragma: no cover - defensive
            return ""

    def _imports(self, path: str, source: str) -> tuple[str, ...]:
        """Workspace-relative paths this file imports, deduplicated and sorted."""

        try:
            records = parse_imports(source)
        except SyntaxError:
            return ()
        targets: set[str] = set()
        for record in records:
            for binding in resolve_import(self._workspace, record, path, self._aliases):
                targets.add(binding.path)
        return tuple(sorted(targets))


def discover(
    workspace: Workspace,
    *,
    max_entries: int = 4_000,
    max_bytes: int = 2_000_000,
) -> ProjectProfile:
    """Read the project layout and classify it, in one bounded pass.

    One walk does all of it: the same traversal that finds the tests also finds
    the packages, so asking the questions separately -- and walking separately --
    is what this avoids.

    Classification reports what it saw. A project with a ``pyproject.toml`` and a
    package directory is a ``python-package`` at high confidence; a directory of
    loose scripts is a ``python-application`` at medium; a project with nothing
    recognisable is ``unknown``, with the reason recorded in ``unknowns``. It never
    invents a kind to fill the field.
    """

    signals: list[str] = []
    unknowns: list[str] = []
    build_files: dict[str, str] = {}
    packages: list[str] = []
    source_roots: list[str] = []
    test_roots: list[str] = []
    doc_roots: list[str] = []
    vcs = ""
    python_files = 0
    other_files: set[str] = set()
    total = 0
    truncated = False
    module_dirs: set[str] = set()

    for entry in _walk(workspace, max_entries):
        if entry.is_dir:
            continue
        total += 1
        path = entry.path
        top = path.split("/")[0]
        if top in _VCS_MARKERS and "/" not in path:
            vcs = top
            continue

        if path.endswith(PYTHON_SUFFIX):
            python_files += 1
            module_dirs.add(_module_dir(path))
            if (top in _TEST_DIR_NAMES or "/tests/" in f"/{path}") and top not in test_roots:
                test_roots.append(top)
            if top == "src":
                if "src" not in source_roots:
                    source_roots.append("src")
            elif top not in _NON_SOURCE_DIRS and top not in source_roots:
                source_roots.append(top)
            continue

        other_files.add(Path(path).suffix.lower())

    truncated = total >= max_entries
    _classify_build_files(workspace, build_files, signals)

    for name in _DOC_DIR_NAMES:
        if workspace.is_dir(name):
            doc_roots.append(name)
            break

    # A package is a directory that is importable: it has an ``__init__.py``, or
    # it sits directly under a source root where a namespace package is legal.
    # "A directory with .py files in it" is not enough -- ``docs/source`` and
    # ``scripts`` both hold Python without being anything the agent should treat
    # as a package to import from.
    doc_prefixes = tuple(f"{root}/" for root in doc_roots)
    for directory in sorted(module_dirs):
        if directory in ("src", ".", "scripts", "examples", "example"):
            continue
        if directory in _NON_SOURCE_DIRS or directory.split("/")[0] in _NON_SOURCE_DIRS:
            continue
        if any(directory.startswith(prefix) for prefix in doc_prefixes):
            continue
        if directory.split("/")[0].startswith("."):
            continue
        has_init = workspace.is_file(f"{directory}/__init__.py")
        # A namespace package is a directory *inside* a source root (``src/ns``),
        # not the source root itself: a flat project's ``loose/`` is just a folder,
        # and calling it a package invites the agent to import from it.
        under_source_root = (
            "/" in directory and directory.rsplit("/", 1)[0] in tuple(source_roots)
        ) or directory.startswith("src/")
        if has_init or under_source_root:
            packages.append(directory)

    entry_points = _entry_points(workspace, signals)
    kind, confidence = _classify(
        python_files=python_files,
        build_files=build_files,
        packages=packages,
        entry_points=entry_points,
        signals=signals,
        unknowns=unknowns,
    )

    if python_files == 0:
        unknowns.append(
            "no Python source files were found; this does not look like a Python project"
        )
    if not build_files:
        unknowns.append("no recognised build or packaging file at the project root")
    if truncated:
        unknowns.append(
            f"the walk stopped at the {max_entries}-entry budget, so this map is partial"
        )

    languages = ["python"] if python_files else []
    languages += sorted(suffix for suffix in other_files if suffix)[:5]

    return ProjectProfile(
        kind=kind,
        confidence=confidence,
        signals=tuple(signals),
        unknowns=tuple(unknowns),
        languages=tuple(languages),
        source_roots=tuple(source_roots[:8]),
        test_roots=tuple(test_roots[:4]),
        doc_roots=tuple(doc_roots[:2]),
        packages=tuple(packages[:40]),
        build_files=build_files,
        entry_points=entry_points,
        vcs=vcs,
        file_count=total,
        truncated=truncated,
    )


def _walk(workspace: Workspace, max_entries: int) -> list[Any]:
    """Every entry, capped, with containment enforced by the workspace."""

    try:
        return list(workspace.walk(".", max_entries=max_entries))
    except InvalidPathError as exc:  # pragma: no cover - workspace already validated
        logger.warning("Could not walk the project: %s", exc.message)
        return []


def _module_dir(path: str) -> str:
    """The directory a module lives in, ``"."`` for a top-level module."""

    parent = path.rsplit("/", 1)[0] if "/" in path else "."
    return parent


def _classify_build_files(
    workspace: Workspace, build_files: dict[str, str], signals: list[str]
) -> None:
    """Record which build file fills which role, and why it was recognised."""

    for role, names in _BUILD_FILES.items():
        for name in names:
            if workspace.is_file(name):
                build_files[role] = name
                signals.append(f"{name} present ({role})")
                break

    # requirements files are named by prefix, not by an exact list.
    for entry in _top_level(workspace):
        name = entry
        if name.startswith(_REQUIREMENT_PREFIX) and name.endswith(".txt"):
            build_files.setdefault("dependencies", name)
            signals.append(f"{name} present (dependencies)")
            break


def _top_level(workspace: Workspace) -> list[str]:
    names: list[str] = []
    for entry in _walk(workspace, 512):
        if "/" in entry.path:
            continue
        names.append(entry.path)
    return names


def _entry_points(workspace: Workspace, signals: list[str]) -> tuple[tuple[str, str], ...]:
    """Declared console entry points, from ``pyproject.toml`` or ``setup.cfg``.

    Read from the metadata rather than inferred from a ``main`` function: a
    declared entry point is what actually runs, and guessing from a function name
    would claim a command that does not exist.
    """

    found: list[tuple[str, str]] = []
    pyproject = workspace.is_file("pyproject.toml")
    if pyproject:
        found.extend(_entry_points_from_pyproject(workspace))
        if found:
            signals.append(f"pyproject.toml declares {len(found)} console script(s)")

    if workspace.is_file("setup.cfg"):
        found.extend(_entry_points_from_setup_cfg(workspace))
        if found:
            signals.append("setup.cfg declares console_scripts")

    if workspace.is_file("__main__.py") or workspace.is_file("src/__main__.py"):
        found.append(("python -m <package>", "__main__.py"))
        signals.append("a __main__.py makes the package runnable with -m")

    for module in _main_files(workspace):
        found.append((f"python -m {module.module}", module.path))
    if any(target.endswith("__main__.py") for _, target in found):
        signals.append("__main__.py found: the package can be run with python -m")

    return tuple(found[:10])


def _main_files(workspace: Workspace) -> list[FileFacts]:
    """``__main__.py`` files anywhere in the tree -- a real runnable entry point.

    The command name uses the dotted module *as imported*, so a ``src/`` layout
    yields ``python -m pkg.__main__`` rather than the path-shaped
    ``python -m src.pkg.__main__``, which would not run.
    """

    found: list[FileFacts] = []
    for entry in _walk(workspace, 2_000):
        if entry.is_dir or Path(entry.path).name != "__main__.py":
            continue
        if any(part.startswith(".") for part in entry.path.split("/")[:-1]):
            continue
        try:
            module = module_name_for(workspace, entry.path)
        except InvalidPathError:
            module = ""
        if not module:
            continue
        if module.startswith("src."):
            module = module[len("src.") :]
        # ``pkg/__main__.py`` is run as ``python -m pkg``: the module name of the
        # file is not the module the user invokes, and reporting the wrong one
        # hands the agent a command that does not work.
        if module.endswith(".__main__"):
            module = module[: -len(".__main__")]
        found.append(FileFacts(path=entry.path, module=module))
        if len(found) >= 5:
            break
    return found


def _entry_points_from_pyproject(workspace: Workspace) -> list[tuple[str, str]]:
    try:
        raw = tomllib.loads(workspace.read_text("pyproject.toml"))
    except (InvalidPathError, OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []

    found: list[tuple[str, str]] = []
    project = raw.get("project", {})
    scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
    if isinstance(scripts, dict):
        found.extend((str(name), str(target)) for name, target in scripts.items())

    poetry = raw.get("tool", {})
    if isinstance(poetry, dict):
        poetry_scripts = poetry.get("poetry", {})
        if isinstance(poetry_scripts, dict):
            legacy = poetry_scripts.get("scripts", {})
            if isinstance(legacy, dict):
                found.extend((str(name), str(target)) for name, target in legacy.items())
    return found


def _entry_points_from_setup_cfg(workspace: Workspace) -> list[tuple[str, str]]:
    """``[console_scripts]`` from setup.cfg, without a config parser dependency."""

    try:
        text = workspace.read_text("setup.cfg")
    except (InvalidPathError, OSError, UnicodeDecodeError):
        return []
    found: list[tuple[str, str]] = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped.lower().startswith("[console_scripts")
            continue
        if not in_section or "=" not in stripped or stripped.startswith(("#", ";")):
            continue
        name, _, target = stripped.partition("=")
        found.append((name.strip(), target.strip()))
    return found


def _classify(
    *,
    python_files: int,
    build_files: dict[str, str],
    packages: list[str],
    entry_points: tuple[tuple[str, str], ...],
    signals: list[str],
    unknowns: list[str],
) -> tuple[str, float]:
    """Coarse kind plus a confidence derived from agreeing signals."""

    if python_files == 0:
        unknowns.append("nothing in this project is Python, so it was not classified")
        return "unknown", 0.0

    score = 0.0
    if "pyproject.toml" in build_files.get("packaging", ""):
        score += 2.0
        signals.append("pyproject.toml is the authoritative build file")
    elif build_files.get("packaging"):
        score += 1.5
    if packages:
        score += 1.5
        signals.append(f"{len(packages)} importable package directories")
    if entry_points:
        score += 1.0
    if build_files.get("testing") or build_files.get("automation"):
        score += 0.5
    if python_files and not packages and not build_files.get("packaging"):
        signals.append("loose modules with no packaging metadata")

    kind = "python-package" if score >= 2.0 else "python-application"
    # Confidence is saturating rather than a ratio: five agreeing signals and ten
    # should both read as "confident", not as "twice as confident".
    confidence = round(min(1.0, score / 4.0), 2)
    if confidence < 0.4:
        unknowns.append("few build signals agreed; treat the classification as a hint, not a fact")
    return kind, confidence


def package_aliases(profile: ProjectProfile) -> dict[str, str]:
    """Importable package name -> the directory it actually occupies.

    Only needed where the two spellings differ, which in practice means a
    ``src/`` layout: ``import mypkg.mod`` names ``src/mypkg/mod.py``. Resolving
    without this reports every such import as unresolvable, and an unresolved
    import is indistinguishable from a missing caller.

    A directory with an ``__init__.py`` is a package; a plain directory under a
    source root is treated as a namespace package, which is importable too.
    """

    aliases: dict[str, str] = {}
    for directory in profile.packages:
        if directory in ("src", "."):
            continue
        prefix = ""
        name = directory
        if "/" in directory:
            prefix, _, name = directory.rpartition("/")
            prefix += "/"
        if not name or name.startswith("."):
            continue
        aliases.setdefault(name, f"{prefix}{name}")
    return aliases


class ProjectIntelligence:
    """The session-scoped answer to "what is this project?".

    Owned by the application context rather than by any tool, because the tools
    are rebuilt on every workspace or settings change: a cache attached to a tool
    would be discarded exactly when it is most needed.

    Not persisted. The profile is a claim about files on disk, and files change
    while the application is closed. What *is* stored in a conversation is stable
    metadata only -- the workspace path and the model that was in use -- so a
    reopened conversation can say where it was without claiming what is there.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        max_bytes: int = 2_000_000,
        max_entries: int = 4_000,
        max_indexed_files: int = 600,
    ) -> None:
        self._workspace = workspace
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._max_indexed_files = max_indexed_files
        self._map = ProjectMap(workspace, max_bytes=max_bytes)
        self._profile: ProjectProfile | None = None
        self._indexed = False
        #: Whether the last uncached importer scan stopped early. See
        #: :attr:`scan_truncated` -- kept so "no callers" is never a silent guess.
        self._scan_truncated = False
        #: Rebuilt with the profile: importable name -> real directory.
        self._aliases: dict[str, str] = {}

    @property
    def aliases(self) -> dict[str, str]:
        """Import-name to directory aliases, from the discovered layout."""

        # Reading the profile is what produces them, so it has to happen first.
        _ = self.profile
        return dict(self._aliases)

    # -- the profile -------------------------------------------------------
    @property
    def profile(self) -> ProjectProfile:
        """The classification, computed on first use and reused after that."""

        if self._profile is None:
            self._profile = discover(
                self._workspace, max_entries=self._max_entries, max_bytes=self._max_bytes
            )
            self._aliases = package_aliases(self._profile)
            self._map.set_aliases(self._aliases)
            logger.info(
                "Project profiled as %s (confidence %.2f) with %d files",
                self._profile.kind,
                self._profile.confidence,
                self._profile.file_count,
            )
        return self._profile

    @property
    def map(self) -> ProjectMap:
        return self._map

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def is_indexed(self) -> bool:
        return self._indexed

    @property
    def indexed_files(self) -> int:
        return self._map.size

    @property
    def stale_files(self) -> tuple[str, ...]:
        return self._map.stale_paths

    def index(self) -> int:
        """Populate the symbol/import map for the source roots.

        Bounded by file count, because a monorepo has to fail gracefully rather
        than spend a turn's budget on it. What was skipped is recorded.
        """

        # Reading the profile first is what decides which roots are worth indexing.
        _ = self.profile
        candidates = self._index_candidates()
        self._map.load(candidates)
        self._indexed = True
        logger.info(
            "Indexed %d of %d candidate files (generation %d)",
            self._map.size,
            len(candidates),
            self._map.generation,
        )
        return self._map.size

    def _index_candidates(self) -> list[str]:
        """Python files under the source roots, excluding tests, capped."""

        roots = self.profile.source_roots or (".",)
        found: list[str] = []
        for root in roots:
            for entry in _walk(self._workspace, self._max_entries):
                if entry.is_dir or not entry.path.endswith(PYTHON_SUFFIX):
                    continue
                top = entry.path.split("/")[0]
                if root != "." and top != root:
                    continue
                if top in _TEST_DIR_NAMES:
                    continue
                found.append(entry.path)
                if len(found) >= self._max_indexed_files:
                    return found
        return found

    # -- mutation awareness ------------------------------------------------
    def note_written(self, path: str) -> None:
        """The agent changed a file: its facts are now suspect."""

        self._map.note_changed(path)
        self._note_structure_may_have_changed(path)

    def note_created(self, path: str) -> None:
        """The agent created a file.

        A new file can add a package, a test directory or an entry point, so the
        profile is dropped as well -- but the expensive symbol index is not.
        """

        self._map.note_created(path)
        self._note_structure_may_have_changed(path)

    def note_deleted(self, path: str) -> None:
        """The agent deleted a file: drop it and every edge that pointed at it."""

        self._map.note_deleted(path)
        self._note_structure_may_have_changed(path)

    def _note_structure_may_have_changed(self, path: str) -> None:
        """Drop the profile only when the change could alter the layout.

        Editing a function cannot change what kind of project this is; adding a
        file at the root, or a directory, can. Refreshing the profile is cheap
        because it is a single bounded walk, but re-doing it on every keystroke of
        every edit would be waste.
        """

        parts = path.split("/")
        if len(parts) == 1 or (len(parts) == 2 and parts[1] == "__init__.py"):
            logger.debug("Dropping the project profile after a change to %s", path)
            self._profile = None

    def invalidate(self) -> None:
        """Forget the profile and the map. Used when the workspace changes."""

        logger.info("Project intelligence invalidated")
        self._profile = None
        self._map.invalidate_all()
        self._indexed = False

    def refresh(self) -> ProjectProfile:
        """Rebuild the profile now, without waiting for the next question."""

        self._profile = None
        return self.profile

    # -- queries -----------------------------------------------------------
    def facts(self, path: str) -> FileFacts:
        return self._map.facts(path)

    def symbols(self, path: str) -> tuple[Symbol, ...]:
        return self._map.symbols(path)

    def importers_of(self, path: str) -> tuple[str, ...]:
        """Files that import ``path``.

        When the map has not been built yet this answers from a bounded scan
        rather than claiming there are none -- an unindexed project reporting "no
        callers" would be a lie, and a dangerous one.
        """

        known = self._map.importers_of(path)
        if known or self._indexed:
            return known
        from quantsmind_agent.code_imports import importers_of

        # Aliases matter here for the same reason they matter in the map: without
        # them an absolute import in a src-layout project resolves to nothing and
        # the scan would report "no callers" for a file that has callers.
        found, self._scan_truncated = importers_of(self._workspace, path, aliases=self.aliases)
        return tuple(sorted({str(item["path"]) for item in found}))

    @property
    def scan_truncated(self) -> bool:
        """Whether the last uncached importer scan hit its file budget.

        Meaningless once :meth:`index` has run -- an indexed answer is complete
        for the files it covers, and :attr:`stale_files` names what it does not.
        """

        return self._scan_truncated


@dataclass(frozen=True, slots=True)
class TaskContext:
    """Bounded context for one task, and an explicit account of what was left out.

    ``truncated`` is not decoration: a model told "here are 12 related files"
    when there were 300 will confidently work with 12 and call it thorough. The
    counts and the flag are what let the model decide whether to ask for more.
    """

    project: ProjectProfile
    files: tuple[dict[str, Any], ...] = ()
    symbols: tuple[dict[str, Any], ...] = ()
    tests: tuple[str, ...] = ()
    callers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    scanned: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.to_dict(),
            "files": [dict(item) for item in self.files],
            "symbols": [dict(item) for item in self.symbols],
            "tests": list(self.tests),
            "callers": list(self.callers),
            "notes": list(self.notes),
            "scanned": self.scanned,
            "truncated": self.truncated,
        }


def task_context(
    intelligence: ProjectIntelligence,
    *,
    task: str = "",
    path: str = "",
    symbol: str = "",
    max_files: int = 20,
    max_symbols: int = 40,
    max_tests: int = 10,
) -> TaskContext:
    """Assemble the context a task needs, and nothing more.

    Bounded on every axis, because the failure mode of "give the model the
    project" is a transcript-sized wall of text that crowds out the conversation
    and is expensive to carry every turn.

    Relevance is lexical and deterministic: a task's words are matched against
    paths and symbol names. That is not semantic search and does not pretend to
    be -- it is the same evidence the navigation tools already use, gathered once
    instead of repeatedly.
    """

    workspace = intelligence.workspace
    profile = intelligence.profile
    terms = _terms(task, symbol)
    max_files = max(1, min(int(max_files), 100))
    max_symbols = max(1, min(int(max_symbols), 400))
    max_tests = max(0, min(int(max_tests), 50))

    notes: list[str] = []
    anchor = _resolve_anchor(workspace, path, symbol, notes)
    if anchor:
        terms.update(_terms(Path(anchor).stem, ""))

    candidates = _candidate_files(intelligence, profile)
    scored = [(_score(path_name, terms, anchor), path_name) for path_name in candidates]
    scored = [item for item in scored if item[0] > 0 or anchor in (item[1],)]
    scored.sort(key=lambda item: (-item[0], item[1]))

    truncated = len(scored) > max_files
    chosen = [name for _, name in scored[:max_files]]

    files: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for name in chosen:
        facts = intelligence.facts(name)
        files.append(
            {
                "path": name,
                "module": facts.module,
                "symbols": facts.symbol_count,
                "imports": len(facts.imports),
                "unparsable": facts.unparsable,
            }
        )
        for symbol_record in facts.symbols:
            if len(symbols) >= max_symbols:
                truncated = True
                break
            if symbol_record.qualified in seen_symbols:
                continue
            seen_symbols.add(symbol_record.qualified)
            symbols.append({"path": name, **symbol_record.to_dict()})
    if truncated and len(symbols) >= max_symbols:
        notes.append(f"symbol list capped at {max_symbols}")

    callers: tuple[str, ...] = ()
    if anchor:
        callers = intelligence.importers_of(anchor)[:max_files]
        if not callers and intelligence.scan_truncated:
            notes.append(
                f"the importer scan for {anchor} stopped at its budget, so there may be "
                "callers it did not reach"
            )
        elif not callers:
            notes.append(f"no known importer of {anchor}; the import map may be incomplete")

    tests = _related_tests(intelligence, profile, chosen)[:max_tests]
    if profile.truncated:
        notes.append("the project walk hit its budget, so the layout may be partial")
    if profile.unknowns:
        notes.append("unknown: " + profile.unknowns[0])
    if not chosen:
        notes.append("nothing matched; ask for a path or a symbol name")

    return TaskContext(
        project=profile,
        files=tuple(files),
        symbols=tuple(symbols),
        tests=tuple(tests),
        callers=callers,
        notes=tuple(notes),
        scanned=len(candidates),
        truncated=truncated,
    )


def _terms(task: str, symbol: str) -> set[str]:
    """Meaningful, lower-cased words from a task and an optional symbol."""

    words: set[str] = set()
    for source in (task, symbol):
        for chunk in str(source or "").replace("/", " ").replace("_", " ").split():
            word = chunk.strip().lower()
            # Drop the words that match everything, so they cannot make every file
            # look equally relevant.
            if len(word) >= 3 and word not in _STOPWORDS:
                words.add(word)
    return words


#: Words that would otherwise make every path match.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "please",
        "make",
        "code",
        "file",
        "files",
        "project",
        "test",
        "tests",
        "add",
        "use",
        "using",
        "when",
        "where",
        "what",
        "which",
        "should",
        "does",
        "not",
        "you",
        "your",
        "can",
        "need",
        "want",
        "help",
        "bug",
        "fix",
        "run",
        "check",
        "find",
        "show",
    }
)


def _resolve_anchor(workspace: Workspace, path: str, symbol: str, notes: list[str]) -> str:
    """Turn a path or symbol into one concrete file, if possible."""

    if path:
        candidate = path.strip().replace("\\", "/")
        if workspace.is_file(candidate):
            return candidate
        if workspace.is_dir(candidate):
            notes.append(f"{candidate} is a directory; showing the files inside it")
            return ""
        notes.append(f"{candidate} does not exist; matched on names instead")
        return ""

    if symbol:
        wanted = symbol.strip().lower()
        for candidate in _python_files(workspace, 2_000):
            name = Path(candidate).stem.lower()
            if name == wanted:
                return candidate
        notes.append(f"no file is named {symbol}; matched on names instead")
    return ""


def _candidate_files(intelligence: ProjectIntelligence, profile: ProjectProfile) -> list[str]:
    """Python files in the source and test roots, in a deterministic order.

    A file the agent has just created is in the map but not yet indexed, so the
    invalidated paths are unioned in. Leaving them out would mean the context
    cannot find the file the agent wrote one turn ago.
    """

    known = set(intelligence.map.known_paths()) | set(intelligence.map.stale_paths)
    if known:
        return sorted(known)
    roots = list(profile.source_roots) or ["."]
    found: list[str] = []
    for entry in _walk(intelligence.workspace, 3_000):
        if entry.is_dir or not entry.path.endswith(PYTHON_SUFFIX):
            continue
        if any(entry.path.startswith(f"{root}/") or entry.path == root for root in roots):
            found.append(entry.path)
    return found


def _python_files(workspace: Workspace, limit: int) -> list[str]:
    return [
        entry.path
        for entry in _walk(workspace, limit)
        if not entry.is_dir and entry.path.endswith(PYTHON_SUFFIX)
    ]


def _score(path: str, terms: set[str], anchor: str) -> float:
    """Lexical relevance. Deterministic, cheap, and honest about being lexical."""

    if not terms:
        return 1.0 if path == anchor else 0.0
    name = path.lower()
    stem = Path(name).stem
    score = 0.0
    for term in terms:
        if term in stem:
            score += 3.0
        elif term in name:
            score += 1.0
    if path == anchor:
        score += 10.0
    elif anchor and path.startswith(anchor.rsplit("/", 1)[0] + "/"):
        score += 0.5
    return score


def _related_tests(
    intelligence: ProjectIntelligence, profile: ProjectProfile, chosen: list[str]
) -> list[str]:
    """Tests that touch the same subject as the chosen files.

    Matched by shared name fragment, which catches the ordinary case -- a test
    file named after the module it exercises -- and is explicitly not trying to
    trace test coverage, which needs execution.
    """

    stems = {Path(name).stem for name in chosen}
    fragments = {stem for stem in stems if len(stem) >= 4}
    tests: list[str] = []
    for root in profile.test_roots:
        for entry in _walk(intelligence.workspace, 2_000):
            if entry.is_dir or not entry.path.endswith(PYTHON_SUFFIX):
                continue
            if not entry.path.startswith(f"{root}/"):
                continue
            if Path(entry.path).name == "__init__.py":
                # A package marker is not a test, however much else is in the folder.
                continue
            stem = Path(entry.path).stem
            named_after_module = stem.startswith("test_") and stem[5:] in stems
            shares_a_fragment = any(fragment and fragment in stem for fragment in fragments)
            if named_after_module or shares_a_fragment:
                tests.append(entry.path)
            if len(tests) >= 50:
                break
    return sorted(dict.fromkeys(tests))
