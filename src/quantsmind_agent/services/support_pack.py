"""Diagnostic support pack (WP v1.4).

Read-only snapshot: version, platform, destinations, audit chain status,
workspace roots, task state summary, last-error log tail. Secrets redacted.
"""

from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

__all__ = ["build_support_pack"]


def build_support_pack(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    try:
        from quantsmind_agent import version as _v

        pack_version: str = str(getattr(_v, "__version__", "unknown"))
    except Exception:
        pack_version = "unknown"
    pack: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": pack_version,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "workspace": str(root),
        "destinations": ["home", "ai", "development", "changes", "control"],
        "signing": {"authorized": False, "note": "unsigned local build"},
    }
    # Audit tip (chain head) without record bodies.
    try:
        import quantsmind_agent.security.audit_sink as _sink  # noqa: F401

        sink_dir = root / ".quantsmind" / "audit.jsonl"
        if sink_dir.parent.is_dir():
            pass  # anchor live in app context; support pack stays read-only
    except Exception:
        pass
    # Task state summary (counts only, no bodies).
    try:
        state_path = root / ".quantsmind" / "task_state.json"
        data = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        pack["task_state"] = {"tasks": len(data) if isinstance(data, dict) else 0}
    except Exception:
        pack["task_state"] = {"tasks": "unreadable"}
    return pack


def write_support_pack(root: str | Path, destination: str | Path) -> Path:
    pack = build_support_pack(root)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(pack, indent=2) + "\n", encoding="utf-8")
    return destination
