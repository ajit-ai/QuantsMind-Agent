"""Workspace intelligence: a bounded, versioned account of a whole project.

Project intelligence answers "what kind of project is this and who imports
this file". Workspace intelligence broadens that answer without turning it into
a repository dump: it records the manifests, ecosystems, declared checks and a
bounded sample of files, and carries an explicit account of what was left out.

Two properties matter more than the fields themselves.

* **Bounded by construction.** Every walk has an entry budget, every snapshot
  has a file budget, and the prompt rendering has a character budget. A large
  repository must degrade visibly, not turn a planning call into a filesystem
  sweep.
* **A claim about files, not a cached truth.** The snapshot is invalidated when
  the agent changes the workspace. It is never persisted as a substitute for
  looking at the files again; the task record keeps one only as evidence of what
  the plan was made against.

The snapshot is also multi-language. :mod:`quantsmind_agent.project_intel`
classifies Python because that is where symbol navigation is strongest, but a
plan for a Node, Rust, Go or .NET repository must still know what it is in.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.project_intel import (
    PYTHON_SUFFIX,
    ProjectIntelligence,
    ProjectProfile,
    discover,
)
from quantsmind_agent.verification import Check, discover_checks
from quantsmind_agent.workspace import Workspace

__all__ = [
    "MAX_SNAPSHOT_FILES",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotFile",
    "WorkspaceIntelligence",
    "WorkspaceSnapshot",
    "discover_workspace",
    "snapshot",
]

logger = get_logger(__name__)

#: Bumped when the stored shape of a snapshot changes. Anything reading a newer
#: snapshot refuses it rather than guessing which fields survived.
SNAPSHOT_SCHEMA_VERSION = 1

#: How many files a snapshot carries. The count of everything seen is kept
#: separately, so "400 of 12,000" is visible rather than implied.
MAX_SNAPSHOT_FILES = 400

_MANIFEST_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "requirements.txt")),
    ("node", ("package.json",)),
    ("rust", ("Cargo.toml",)),
    ("go", ("go.mod",)),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts")),
    ("dotnet", ("global.json",)),
    ("ruby", ("Gemfile", "Rakefile")),
    ("php", ("composer.json",)),
    ("cpp", ("CMakeLists.txt",)),
    ("swift", ("Package.swift",)),
)

_SUFFIX_ECOSYSTEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", (PYTHON_SUFFIX,)),
    ("node", (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")),
    ("rust", (".rs",)),
    ("go", (".go",)),
    ("java", (".java", ".kt", ".kts")),
    ("dotnet", (".cs", ".fs", ".vb")),
    ("ruby", (".rb",)),
    ("php", (".php",)),
    ("cpp", (".c", ".h", ".cc", ".cpp", ".hpp", ".cxx")),
    ("swift", (".swift",)),
)

_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".fs": "fsharp",
    ".vb": "visual-basic",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cxx": "cpp",
    ".swift": "swift",
}

_ECOSYSTEM_LANGUAGE = {
    "python": "python",
    "node": "javascript",
    "rust": "rust",
    "go": "go",
    "java": "java",
    "dotnet": "csharp",
    "ruby": "ruby",
    "php": "php",
    "cpp": "cpp",
    "swift": "swift",
}

_KIND_BY_ECOSYSTEM = {
    "node": "node-package",
    "rust": "rust-package",
    "go": "go-module",
    "java": "java-project",
    "dotnet": "dotnet-project",
    "ruby": "ruby-project",
    "php": "php-project",
    "cpp": "cpp-project",
    "swift": "swift-package",
}

_TEST_DIRS = frozenset({"test", "tests", "testing", "spec", "specs", "__tests__"})
_DOC_DIRS = frozenset({"doc", "docs", "documentation"})
_CONFIG_NAMES = frozenset(
    {
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        ".pre-commit-config.yaml",
        "dockerfile",
        "docker-compose.yml",
        "compose.yaml",
        "makefile",
        "justfile",
        "manifest.in",
    }
)
_MANIFEST_NAMES = frozenset(
    {name for _ecosystem, names in _MANIFEST_RULES for name in names}
    | {"requirements-dev.txt", "phpunit.xml", "phpunit.xml.dist"}
)


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """One file in a bounded snapshot, with only the facts planning needs."""

    path: str
    size: int = 0
    kind: str = "other"
    language: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "kind": self.kind,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SnapshotFile:
        return cls(
            path=str(payload.get("path", "")),
            size=int(payload.get("size", 0) or 0),
            kind=str(payload.get("kind", "other")),
            language=str(payload.get("language", "")),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """A versioned answer to "what is in this workspace?".

    ``file_count`` is everything the bounded walk saw; ``files`` is the sample
    that was kept. ``truncated`` says the walk hit its entry budget, and
    ``sampled`` says the file list was shortened. They are separate because a
    project can be fully walked and still too large to include in a prompt.
    """

    workspace: str
    project_kind: str = "unknown"
    confidence: float = 0.0
    languages: tuple[str, ...] = ()
    ecosystems: tuple[str, ...] = ()
    manifests: tuple[str, ...] = ()
    source_roots: tuple[str, ...] = ()
    test_roots: tuple[str, ...] = ()
    doc_roots: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()
    files: tuple[SnapshotFile, ...] = ()
    file_count: int = 0
    total_bytes: int = 0
    truncated: bool = False
    sampled: bool = False
    max_entries: int = 4_000
    max_files: int = MAX_SNAPSHOT_FILES
    unknowns: tuple[str, ...] = ()
    digest: str = ""
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    @property
    def workspace_name(self) -> str:
        text = str(self.workspace or "").replace("\\", "/").rstrip("/")
        return text.rsplit("/", 1)[-1] if text else ""

    @property
    def check_ids(self) -> tuple[str, ...]:
        return tuple(check.id for check in self.checks)

    @property
    def complete(self) -> bool:
        """Whether the snapshot covers every file it saw."""

        return not self.truncated and not self.sampled

    def summary(self) -> str:
        parts = [
            self.workspace_name or "(workspace)",
            self.project_kind,
            f"{self.file_count} file(s)",
            f"{len(self.checks)} check(s)",
        ]
        if not self.complete:
            parts.append("partial")
        return " | ".join(parts)

    def format_for_prompt(self, max_chars: int = 3_000) -> str:
        """Render bounded planning context, with no absolute paths.

        The model is told where things are *inside* the project, never where the
        project lives on the user's disk. A prompt is copied into provider
        requests and traces; the root path is neither useful for planning nor
        something to spread further than it already has.
        """

        lines = [f"Workspace: {self.workspace_name or '.'}"]
        lines.append(f"Project kind: {self.project_kind} (confidence {self.confidence:.2f})")
        if self.ecosystems:
            lines.append("Ecosystems: " + ", ".join(self.ecosystems))
        if self.languages:
            lines.append("Languages: " + ", ".join(self.languages))
        if self.source_roots:
            lines.append("Source roots: " + ", ".join(self.source_roots))
        if self.test_roots:
            lines.append("Test roots: " + ", ".join(self.test_roots))
        if self.manifests:
            lines.append("Manifests: " + ", ".join(self.manifests))
        if self.checks:
            rendered = ", ".join(
                f"{check.id} ({check.source or 'declared'})" for check in self.checks[:12]
            )
            lines.append("Declared checks: " + rendered)
        else:
            lines.append("Declared checks: none found")
        files = self.files[:80]
        if files:
            lines.append(f"Files ({self.file_count} seen, {len(self.files)} sampled):")
            lines.extend(
                f"  {item.path}{'/' if item.kind == 'directory' else ''}" for item in files
            )
            if len(self.files) > len(files):
                lines.append(f"  ... and {len(self.files) - len(files)} more sampled files")
        if not self.complete:
            lines.append("Coverage: partial; the counts above are not a complete inventory.")
        if self.unknowns:
            lines.append("Not determined: " + self.unknowns[0])
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)].rstrip() + "..."

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace": self.workspace,
            "project_kind": self.project_kind,
            "confidence": round(self.confidence, 2),
            "languages": list(self.languages),
            "ecosystems": list(self.ecosystems),
            "manifests": list(self.manifests),
            "source_roots": list(self.source_roots),
            "test_roots": list(self.test_roots),
            "doc_roots": list(self.doc_roots),
            "checks": [
                {
                    "id": check.id,
                    "argv": list(check.argv),
                    "description": check.description,
                    "source": check.source,
                    "timeout_seconds": check.timeout_seconds,
                    "optional": check.optional,
                }
                for check in self.checks
            ],
            "files": [item.to_dict() for item in self.files],
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "sampled": self.sampled,
            "max_entries": self.max_entries,
            "max_files": self.max_files,
            "unknowns": list(self.unknowns),
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WorkspaceSnapshot:
        """Read a snapshot, refusing one written by a newer build."""

        if not isinstance(payload, dict):
            raise ValueError("a snapshot must be a JSON object")
        version = int(payload.get("schema_version", 0) or 0)
        if version > SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"workspace snapshot schema {version} is newer than this build")
        checks = []
        for item in payload.get("checks") or []:
            if not isinstance(item, dict):
                continue
            checks.append(
                Check(
                    id=str(item.get("id", "")),
                    argv=tuple(str(value) for value in item.get("argv") or []),
                    description=str(item.get("description", "")),
                    source=str(item.get("source", "")),
                    timeout_seconds=int(item.get("timeout_seconds", 300) or 300),
                    optional=bool(item.get("optional", False)),
                )
            )
        files = [
            SnapshotFile.from_dict(item)
            for item in payload.get("files") or []
            if isinstance(item, dict)
        ]
        return cls(
            workspace=str(payload.get("workspace", "")),
            project_kind=str(payload.get("project_kind", "unknown")),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            languages=tuple(str(value) for value in payload.get("languages") or []),
            ecosystems=tuple(str(value) for value in payload.get("ecosystems") or []),
            manifests=tuple(str(value) for value in payload.get("manifests") or []),
            source_roots=tuple(str(value) for value in payload.get("source_roots") or []),
            test_roots=tuple(str(value) for value in payload.get("test_roots") or []),
            doc_roots=tuple(str(value) for value in payload.get("doc_roots") or []),
            checks=tuple(checks),
            files=tuple(files),
            file_count=int(payload.get("file_count", 0) or 0),
            total_bytes=int(payload.get("total_bytes", 0) or 0),
            truncated=bool(payload.get("truncated", False)),
            sampled=bool(payload.get("sampled", False)),
            max_entries=int(payload.get("max_entries", 4_000) or 4_000),
            max_files=int(payload.get("max_files", MAX_SNAPSHOT_FILES) or MAX_SNAPSHOT_FILES),
            unknowns=tuple(str(value) for value in payload.get("unknowns") or []),
            digest=str(payload.get("digest", "")),
            schema_version=version or SNAPSHOT_SCHEMA_VERSION,
        )


def _workspace(value: Workspace | str | Path) -> Workspace:
    return value if isinstance(value, Workspace) else Workspace(value)


def _file_kind(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _MANIFEST_NAMES:
        return "manifest"
    parts = path.lower().split("/")
    if (
        any(part in _TEST_DIRS for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts"))
    ):
        return "test"
    if any(part in _DOC_DIRS for part in parts[:-1]) or name.endswith((".md", ".rst")):
        return "doc"
    if name in _CONFIG_NAMES or name.startswith("."):
        return "config"
    if Path(path).suffix.lower() in _LANGUAGE_BY_SUFFIX:
        return "source"
    return "other"


def _rank(kind: str) -> int:
    return {"manifest": 0, "source": 1, "test": 2, "doc": 3, "config": 4, "other": 5}.get(kind, 5)


def _manifest_names(names: set[str]) -> tuple[str, ...]:
    found = {name for name in names if name in _MANIFEST_NAMES}
    found.update(
        name for name in names if name.startswith("requirements") and name.endswith(".txt")
    )
    found.update(
        name for name in names if Path(name).suffix.lower() in {".sln", ".csproj", ".fsproj"}
    )
    if "phpunit.xml.dist" in found:
        found.discard("phpunit.xml.dist")
    return tuple(sorted(found))


def _ecosystems(manifests: tuple[str, ...], suffixes: set[str]) -> tuple[str, ...]:
    found: set[str] = set()
    for ecosystem, names in _MANIFEST_RULES:
        if any(name in manifests for name in names):
            found.add(ecosystem)
    if any(Path(name).suffix.lower() in {".sln", ".csproj", ".fsproj"} for name in manifests):
        found.add("dotnet")
    for ecosystem, ecosystem_suffixes in _SUFFIX_ECOSYSTEMS:
        if any(suffix in suffixes for suffix in ecosystem_suffixes):
            found.add(ecosystem)
    order = [name for name, _names in _MANIFEST_RULES]
    return tuple(ecosystem for ecosystem in order if ecosystem in found)


def _project_kind(ecosystems: tuple[str, ...], profile: ProjectProfile) -> str:
    if not ecosystems:
        return "unknown"
    if len(ecosystems) > 1:
        return "polyglot"
    only = ecosystems[0]
    if only == "python":
        return profile.kind if profile.kind.startswith("python") else "python-project"
    return _KIND_BY_ECOSYSTEM.get(only, f"{only}-project")


def _confidence(
    ecosystems: tuple[str, ...], manifests: tuple[str, ...], profile: ProjectProfile
) -> float:
    if ecosystems == ("python",) and profile.kind.startswith("python"):
        return profile.confidence
    if not ecosystems:
        return 0.0
    score = 0.45 + min(0.45, 0.15 * len(manifests))
    if len(ecosystems) > 1:
        score = min(score, 0.75)
    return round(min(1.0, score), 2)


def _languages(ecosystems: tuple[str, ...], suffix_counts: dict[str, int]) -> tuple[str, ...]:
    found: list[str] = []
    for ecosystem in ecosystems:
        language = _ECOSYSTEM_LANGUAGE.get(ecosystem)
        if language and language not in found:
            found.append(language)
    extras = sorted(
        (
            (language, count)
            for suffix, count in suffix_counts.items()
            if (language := _LANGUAGE_BY_SUFFIX.get(suffix)) and language not in found
        ),
        key=lambda item: (-item[1], item[0]),
    )
    found.extend(language for language, _count in extras)
    return tuple(found)


def _source_roots(
    paths: list[str], test_roots: tuple[str, ...], doc_roots: tuple[str, ...]
) -> tuple[str, ...]:
    roots: list[str] = []
    excluded = set(test_roots) | set(doc_roots)
    for path in paths:
        if Path(path).suffix.lower() not in _LANGUAGE_BY_SUFFIX:
            continue
        top = path.split("/")[0] if "/" in path else "."
        if top in excluded:
            continue
        if top not in roots:
            roots.append(top)
    roots.sort(key=lambda item: (item != "src", item != ".", item))
    return tuple(roots[:8])


def _detect_roots(
    paths: list[str], profile: ProjectProfile
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    test_roots = list(profile.test_roots)
    doc_roots = list(profile.doc_roots)
    for path in paths:
        parts = path.lower().split("/")
        if len(parts) > 1 and parts[0] in _TEST_DIRS and parts[0] not in test_roots:
            test_roots.append(parts[0])
        if len(parts) > 1 and parts[0] in _DOC_DIRS and parts[0] not in doc_roots:
            doc_roots.append(parts[0])
    return tuple(test_roots[:4]), tuple(doc_roots[:2])


def _digest(payload: dict[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "digest"}
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def discover_workspace(
    workspace: Workspace | str | Path,
    *,
    max_entries: int = 4_000,
    max_files: int = MAX_SNAPSHOT_FILES,
    profile: ProjectProfile | None = None,
) -> WorkspaceSnapshot:
    """Walk once, classify broadly, and return a bounded snapshot.

    The walk is the workspace's own, so symlinks and ignored directories are
    handled by the same containment rule every read uses. Nothing outside the
    project is inspected, and no file contents are read except manifests.
    """

    project = _workspace(workspace)
    excluded: list[str] = []
    entries = list(project.walk(".", max_entries=max(1, int(max_entries)), excluded=excluded))
    profile = profile or project_intelligence_profile(project)
    names = {entry.name for entry in entries if "/" not in entry.path}
    suffixes = {Path(entry.path).suffix.lower() for entry in entries}
    suffix_counts: dict[str, int] = {}
    for entry in entries:
        suffix = Path(entry.path).suffix.lower()
        suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1

    manifests = _manifest_names(names)
    ecosystems = _ecosystems(manifests, suffixes)
    test_roots, doc_roots = _detect_roots([entry.path for entry in entries], profile)
    source_roots = _source_roots([entry.path for entry in entries], test_roots, doc_roots)

    files: list[SnapshotFile] = []
    total_bytes = 0
    for entry in entries:
        total_bytes += max(0, int(entry.size or 0))
        kind = _file_kind(entry.path)
        language = _LANGUAGE_BY_SUFFIX.get(Path(entry.path).suffix.lower(), "")
        files.append(
            SnapshotFile(
                path=entry.path, size=max(0, int(entry.size or 0)), kind=kind, language=language
            )
        )

    ranked = sorted(files, key=lambda item: (_rank(item.kind), item.path))
    selected = sorted(ranked[: max(0, int(max_files))], key=lambda item: item.path)
    truncated = bool(profile.truncated or len(entries) >= max_entries)
    sampled = len(files) > len(selected)

    unknowns: list[str] = []
    for note in profile.unknowns:
        if "python" in note.lower() and "python" not in ecosystems:
            continue
        if note not in unknowns:
            unknowns.append(note)
    if not manifests:
        unknowns.append("no recognised manifest at the project root")
    if truncated:
        unknowns.append(
            f"the walk stopped at the {max_entries}-entry budget, so this inventory is partial"
        )
    if sampled:
        unknowns.append(
            f"only {len(selected)} of {len(files)} discovered files are included in the snapshot"
        )
    if excluded:
        sample = ", ".join(excluded[:5])
        if len(excluded) > 5:
            sample += f", and {len(excluded) - 5} more"
        unknowns.append(
            f"{len(excluded)} filesystem link(s) were not followed; "
            f"their contents are not represented: {sample}"
        )

    try:
        checks = tuple(discover_checks(project.root))
    except Exception as exc:  # a broken manifest must not make the workspace unreadable
        logger.info("Could not discover workspace checks: %s", exc)
        checks = ()
    if not checks:
        unknowns.append("no declared verification checks were found")

    snapshot = WorkspaceSnapshot(
        workspace=str(project.root),
        project_kind=_project_kind(ecosystems, profile),
        confidence=_confidence(ecosystems, manifests, profile),
        languages=_languages(ecosystems, suffix_counts),
        ecosystems=ecosystems,
        manifests=manifests,
        source_roots=source_roots or profile.source_roots,
        test_roots=test_roots,
        doc_roots=doc_roots,
        checks=checks,
        files=tuple(selected),
        file_count=len(files),
        total_bytes=total_bytes,
        truncated=truncated,
        sampled=sampled,
        max_entries=max(1, int(max_entries)),
        max_files=max(0, int(max_files)),
        unknowns=tuple(unknowns),
    )
    payload = snapshot.to_dict()
    payload["digest"] = _digest(payload)
    return WorkspaceSnapshot.from_dict(payload)


def project_intelligence_profile(workspace: Workspace) -> ProjectProfile:
    """The Python-oriented profile, reused rather than recomputed here.

    Kept as a small function so the snapshot and the shared intelligence agree
    on what ``python-package`` means, including its confidence and unknowns.
    """

    return discover(workspace, max_entries=4_000)


def snapshot(
    workspace: Workspace | str | Path,
    *,
    max_entries: int = 4_000,
    max_files: int = MAX_SNAPSHOT_FILES,
) -> WorkspaceSnapshot:
    """Functional alias for :func:`discover_workspace`."""

    return discover_workspace(workspace, max_entries=max_entries, max_files=max_files)


class WorkspaceIntelligence(ProjectIntelligence):
    """Project intelligence plus a cached, bounded workspace snapshot.

    Subclassing :class:`ProjectIntelligence` is deliberate: tools take that type
    and call its navigation methods, so replacing it with a new, unrelated
    object would either break them or require a second cache of the same files.
    This class adds snapshot questions and invalidates the snapshot on every
    mutation the base class already tracks.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        max_bytes: int = 2_000_000,
        max_entries: int = 4_000,
        max_indexed_files: int = 600,
        max_files: int = MAX_SNAPSHOT_FILES,
    ) -> None:
        super().__init__(
            workspace,
            max_bytes=max_bytes,
            max_entries=max_entries,
            max_indexed_files=max_indexed_files,
        )
        self._max_files = max(0, int(max_files))
        self._snapshot: WorkspaceSnapshot | None = None

    @property
    def snapshot(self) -> WorkspaceSnapshot:
        """The current snapshot, discovered on first use and reused after that."""

        if self._snapshot is None:
            self._snapshot = discover_workspace(
                self.workspace,
                max_entries=self._max_entries,
                max_files=self._max_files,
                profile=self.profile,
            )
        return self._snapshot

    @property
    def cached_snapshot(self) -> WorkspaceSnapshot | None:
        """The snapshot if it has already been built, without walking the disk.

        Widgets use this so painting the project summary never becomes a
        filesystem operation on the GUI thread.
        """

        return self._snapshot

    def refresh_snapshot(self) -> WorkspaceSnapshot:
        """Rebuild the snapshot and the Python profile now.

        Refreshing only the snapshot would pair fresh file facts with a stale
        profile. A user pressing Refresh means "look again", not "look again at
        some of it".
        """

        self.refresh()
        return self.snapshot

    def planning_context(self, max_chars: int = 3_000) -> str:
        """Bounded project context for a planning prompt."""

        return self.snapshot.format_for_prompt(max_chars=max_chars)

    def summary(self) -> str:
        return self.snapshot.summary()

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def max_files(self) -> int:
        return self._max_files

    def note_written(self, path: str) -> None:
        super().note_written(path)
        self._snapshot = None

    def note_created(self, path: str) -> None:
        super().note_created(path)
        self._snapshot = None

    def note_deleted(self, path: str) -> None:
        super().note_deleted(path)
        self._snapshot = None

    def invalidate(self) -> None:
        super().invalidate()
        self._snapshot = None

    def refresh(self) -> ProjectProfile:
        profile = super().refresh()
        self._snapshot = None
        return profile
