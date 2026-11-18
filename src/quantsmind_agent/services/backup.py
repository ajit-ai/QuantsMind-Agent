"""Backup/restore for workspace .quantsmind state (WP v1.4)."""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

__all__ = ["backup_workspace", "restore_workspace"]

_STATE_NAMES = ("profiles.json", "memory.json", "task_state.json", "onboarding.json")


def backup_workspace(root: str | Path, destination: str | Path) -> Path:
    root = Path(root)
    state_dir = root / ".quantsmind"
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
        meta = {
            "root": str(root),
            "backed_up_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "version": 1,
        }
        zf.writestr("meta.json", json.dumps(meta, indent=2))
        if state_dir.is_dir():
            for name in _STATE_NAMES:
                path = state_dir / name
                if path.is_file():
                    zf.write(path, arcname=name)
    return destination


def restore_workspace(archive: str | Path, root: str | Path) -> list[str]:
    archive = Path(archive)
    root = Path(root)
    restored: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        for name in _STATE_NAMES:
            try:
                zf.extract(name, root / ".quantsmind")
                restored.append(name)
            except KeyError:
                continue
    return restored
