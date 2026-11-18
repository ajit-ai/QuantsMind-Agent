"""Evidence pack export (WP3.1 of v1.2).

One-click read-only ZIP of audit slice + ChangeSet manifests + verification
results. Read-only: never writes to the workspace, never includes secrets.
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from typing import Any

__all__ = ["export_evidence_pack"]


def export_evidence_pack(
    destination: str | Path,
    *,
    audit_events: list[dict[str, Any]],
    changesets: list[dict[str, Any]],
    verification: list[dict[str, Any]],
    title: str = "evidence-pack",
) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": title,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "format": "quantsmind-evidence/1",
    }
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", json.dumps(meta, indent=2))
        zf.writestr("audit.json", json.dumps(audit_events, indent=2))
        zf.writestr("changesets.json", json.dumps(changesets, indent=2))
        zf.writestr("verification.json", json.dumps(verification, indent=2))
        manifest = {
            "files": ["meta.json", "audit.json", "changesets.json", "verification.json"],
            "audit_records": len(audit_events),
            "changesets": len(changesets),
            "checks": len(verification),
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return destination
