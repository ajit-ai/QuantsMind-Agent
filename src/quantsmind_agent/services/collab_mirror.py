"""Collaboration mirrors: git-friendly profile/memory export (WP v1.8).

Mirrors are plain JSON files a team can commit to a shared Git repo.
No network, no central server — collaboration happens through normal Git.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["export_mirror", "import_mirror"]


def export_mirror(
    destination: str | Path,
    *,
    profiles: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    audit_counts: dict[str, int] | None = None,
) -> Path:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "profiles.json").write_text(
        json.dumps({"format": "quantsmind-mirror/1", "profiles": profiles}, indent=2),
        encoding="utf-8",
    )
    (destination / "memory.json").write_text(
        json.dumps({"format": "quantsmind-mirror/1", "memories": memories}, indent=2),
        encoding="utf-8",
    )
    if audit_counts is not None:
        (destination / "audit_counts.json").write_text(
            json.dumps(audit_counts, indent=2), encoding="utf-8"
        )
    return destination


def import_mirror(source: str | Path) -> dict[str, Any]:
    source = Path(source)
    out: dict[str, Any] = {"profiles": [], "memories": []}
    for name in ("profiles.json", "memory.json"):
        path = source / name
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "profiles" in data:
                    out["profiles"] = data["profiles"]
                if "memories" in data:
                    out["memories"] = data["memories"]
    return out
