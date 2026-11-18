"""Engineering profiles (WP2 of v0.13.0).

Project-scoped profile: identity, languages/frameworks, build/test commands,
formatting/linting, architecture/conventions, instructions, validation rules.
Persisted as JSON at ``<root>/.quantsmind/profiles.json`` scoped per root.
Commands are data only — never executed automatically; execution requires the
existing permission/approval path.
"""

from __future__ import annotations

import builtins
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["EngineeringProfile", "ProfileStore", "ProfileValidationError"]


class ProfileValidationError(ValueError):
    pass


@dataclass
class EngineeringProfile:
    profile_id: str = "default"
    title: str = ""
    description: str = ""
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    format_commands: list[str] = field(default_factory=list)
    lint_commands: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    instructions: str = ""
    validation: list[str] = field(default_factory=list)
    root_id: str = "primary"

    def validate(self) -> None:
        if not self.profile_id or not self.profile_id.strip():
            raise ProfileValidationError("profile_id is required")
        if len(self.title) > 200 or len(self.instructions) > 20_000:
            raise ProfileValidationError("title/instructions too long")
        for name in ("languages", "frameworks", "conventions", "validation"):
            items = getattr(self, name)
            if not isinstance(items, list) or len(items) > 50:
                raise ProfileValidationError(f"{name} must be a list of at most 50 strings")
            if any(not isinstance(x, str) or not x.strip() for x in items):
                raise ProfileValidationError(f"{name} entries must be non-empty strings")
        for name in ("build_commands", "test_commands", "format_commands", "lint_commands"):
            items = getattr(self, name)
            if not isinstance(items, list) or len(items) > 20:
                raise ProfileValidationError(f"{name} must be a list of at most 20 strings")
            if any(not isinstance(x, str) or not x.strip() or "\x00" in x for x in items):
                raise ProfileValidationError(f"{name} entries must be non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EngineeringProfile:
        known = set(cls.__dataclass_fields__)
        profile = cls(**{k: v for k, v in data.items() if k in known})
        profile.validate()
        return profile


class ProfileStore:
    """One store per project root (isolation by construction)."""

    def __init__(self, root: str | Path, *, root_id: str = "primary") -> None:
        self._root = Path(root)
        self._root_id = root_id
        self._path = self._root / ".quantsmind" / "profiles.json"

    @property
    def path(self) -> Path:
        return self._path

    def list(self) -> builtins.list[EngineeringProfile]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for item in data.get("profiles", []) if isinstance(data, dict) else []:
            try:
                out.append(EngineeringProfile.from_dict(item))
            except ProfileValidationError:
                continue
        return out

    def get(self, profile_id: str) -> EngineeringProfile | None:
        for p in self.list():
            if p.profile_id == profile_id:
                return p
        return None

    def save(self, profile: EngineeringProfile) -> EngineeringProfile:
        profile.validate()
        profile.root_id = self._root_id
        items = [p for p in self.list() if p.profile_id != profile.profile_id]
        items.append(profile)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"version": 1, "profiles": [p.to_dict() for p in items]}, indent=2),
            encoding="utf-8",
        )
        return profile

    def delete(self, profile_id: str) -> bool:
        items = [p for p in self.list() if p.profile_id != profile_id]
        if len(items) == len(self.list()):
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"version": 1, "profiles": [p.to_dict() for p in items]}, indent=2),
            encoding="utf-8",
        )
        return True

    # Profiles never execute their own commands.
    def describe_commands(self, profile: EngineeringProfile) -> builtins.list[str]:
        return [
            *profile.build_commands,
            *profile.test_commands,
            *profile.format_commands,
            *profile.lint_commands,
        ]
