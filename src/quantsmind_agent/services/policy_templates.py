"""Enterprise policy templates (WP v1.5)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["PolicyTemplate", "PolicyTemplateError", "PolicyTemplateLibrary"]


class PolicyTemplateError(ValueError):
    pass


@dataclass
class PolicyTemplate:
    name: str
    description: str = ""
    require_approval_for_writes: bool = True
    require_approval_for_commands: bool = True
    max_timeout_seconds: int = 120
    allowed_permissions: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.name.strip():
            raise PolicyTemplateError("template name required")
        if not (1 <= self.max_timeout_seconds <= 600):
            raise PolicyTemplateError("max_timeout_seconds must be 1..600")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PolicyTemplateLibrary:
    def __init__(self, root: str | Path) -> None:
        self._path = Path(root) / ".quantsmind" / "policy_templates.json"

    def list(self) -> list[PolicyTemplate]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for item in data.get("templates", []) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get("name"):
                out.append(
                    PolicyTemplate(
                        **{
                            k: v
                            for k, v in item.items()
                            if k
                            in (
                                "name",
                                "description",
                                "require_approval_for_writes",
                                "require_approval_for_commands",
                                "max_timeout_seconds",
                                "allowed_permissions",
                            )
                        }
                    )
                )
        return out

    def save(self, template: PolicyTemplate) -> PolicyTemplate:
        template.validate()
        items = [t for t in self.list() if t.name != template.name] + [template]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"version": 1, "templates": [t.to_dict() for t in items]}, indent=2),
            encoding="utf-8",
        )
        return template

    def apply(self, name: str) -> PolicyTemplate | None:
        return next((t for t in self.list() if t.name == name), None)
