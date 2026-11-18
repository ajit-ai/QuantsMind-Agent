"""Engineering memory (WP5 of v0.13.0).

Persistent, project-scoped knowledge: architecture decisions, conventions,
constraints, build/test procedures, resolved issues, verified facts and
ChangeSet references. Entries are typed verified/suggested, carry provenance,
can be updated/invalidated, and are deduplicated. Arbitrary workspace content
is never auto-promoted; callers add entries explicitly. Sensitive files are
excluded by the same hints as context intelligence.
"""

from __future__ import annotations

import builtins
import json
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = ["KnowledgeKind", "MemoryEntry", "MemoryStore", "MemoryValidationError"]

_EXCLUDED_HINTS = ("api_key", "secret", "password", "private_key", "credential", ".env", "id_rsa")


class MemoryValidationError(ValueError):
    pass


class KnowledgeKind(StrEnum):
    DECISION = "decision"
    CONVENTION = "convention"
    CONSTRAINT = "constraint"
    PROCEDURE = "procedure"
    RESOLVED_ISSUE = "resolved_issue"
    VERIFIED_FACT = "verified_fact"
    CHANGESET_REF = "changeset_ref"


@dataclass
class MemoryEntry:
    entry_id: str
    kind: KnowledgeKind = KnowledgeKind.VERIFIED_FACT
    title: str = ""
    body: str = ""
    verified: bool = False
    source: str = ""
    provenance: str = ""
    changeset_id: str = ""
    valid: bool = True
    updated_at: float = 0.0

    def validate(self) -> None:
        if not self.title.strip() or len(self.title) > 200:
            raise MemoryValidationError("title required (<=200 chars)")
        if len(self.body) > 8_000:
            raise MemoryValidationError("body too long")
        lowered = (self.title + " " + self.body + " " + self.source).lower()
        if any(hint in lowered for hint in ("api_key =", "secret =", "-----begin")):
            raise MemoryValidationError("entry appears to contain secrets; refusing to store")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        entry = cls(
            entry_id=str(data.get("entry_id", "")),
            kind=KnowledgeKind(str(data.get("kind", "verified_fact"))),
            title=str(data.get("title", "")),
            body=str(data.get("body", "")),
            verified=bool(data.get("verified", False)),
            source=str(data.get("source", "")),
            provenance=str(data.get("provenance", "")),
            changeset_id=str(data.get("changeset_id", "")),
            valid=bool(data.get("valid", True)),
            updated_at=float(data.get("updated_at", 0.0)),
        )
        return entry


class MemoryStore:
    """One store per project root (isolation by construction)."""

    def __init__(self, root: str | Path, *, root_id: str = "primary") -> None:
        self._root = Path(root)
        self._root_id = root_id
        self._path = self._root / ".quantsmind" / "memory.json"

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> builtins.list[MemoryEntry]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for item in data.get("entries", []) if isinstance(data, dict) else []:
            try:
                out.append(MemoryEntry.from_dict(item))
            except (ValueError, MemoryValidationError):
                continue
        return out

    def _save(self, entries: builtins.list[MemoryEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {"version": 1, "root_id": self._root_id, "entries": [e.to_dict() for e in entries]},
                indent=2,
            ),
            encoding="utf-8",
        )

    def add(self, entry: MemoryEntry) -> MemoryEntry:
        entry.validate()
        entries = self._load()
        # Deduplicate on (kind, title) — contradictory titles overwrite with
        # fresh provenance instead of piling up.
        entries = [
            e
            for e in entries
            if not (e.kind == entry.kind and e.title.lower() == entry.title.lower())
        ]
        entry.updated_at = time.time()
        entries.append(entry)
        self._save(entries)
        return entry

    def update(self, entry_id: str, **fields: Any) -> MemoryEntry | None:
        entries = self._load()
        for entry in entries:
            if entry.entry_id == entry_id:
                for key, value in fields.items():
                    if hasattr(entry, key):
                        setattr(entry, key, value)
                entry.validate()
                entry.updated_at = time.time()
                self._save(entries)
                return entry
        return None

    def invalidate(self, entry_id: str) -> bool:
        return self.update(entry_id, valid=False) is not None

    def list(self, *, valid_only: bool = True, verified_only: bool = False) -> builtins.list[MemoryEntry]:
        out = []
        for entry in self._load():
            if valid_only and not entry.valid:
                continue
            if verified_only and not entry.verified:
                continue
            out.append(entry)
        return out

    def search(self, query: str) -> builtins.list[MemoryEntry]:
        q = query.lower()
        return [e for e in self.list() if q in (e.title + " " + e.body).lower()]
