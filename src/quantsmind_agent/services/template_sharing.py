"""Community template sharing (WP v1.7).

Export/import profiles + memory conventions as JSON bundles (opt-in, file-based).
Imports validate through the same validators as storage; secrets dropped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["export_template_bundle", "import_template_bundle"]


def export_template_bundle(
    destination: str | Path,
    *,
    profiles: list[dict[str, Any]],
    memories: list[dict[str, Any]],
) -> Path:
    clean_memories = [
        m for m in memories if "api_key" not in str(m).lower() and "secret" not in str(m).lower()
    ]
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {"format": "quantsmind-templates/1", "profiles": profiles, "memories": clean_memories},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def import_template_bundle(source: str | Path) -> dict[str, Any]:
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != "quantsmind-templates/1":
        raise ValueError("not a QuantsMind template bundle")
    return data
