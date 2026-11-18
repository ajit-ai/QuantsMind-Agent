"""Scheduled audit reports (WP v1.5).

Generates a periodic plaintext/JSON summary (counts only, no bodies) for
archival. Scheduling itself stays with the OS/task scheduler; this service
only builds the report deterministically from audit counts.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

__all__ = ["build_audit_report"]


def build_audit_report(counts: dict[str, int], *, period: str = "weekly") -> dict[str, Any]:
    return {
        "period": period,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": dict(counts),
        "total": sum(counts.values()),
    }


def write_audit_report(root: str | Path, counts: dict[str, int], *, period: str = "weekly") -> Path:
    report = build_audit_report(counts, period=period)
    out = Path(root) / ".quantsmind" / "audit_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return out
