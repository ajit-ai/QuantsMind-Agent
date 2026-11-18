"""Atomic file replacement and tolerant JSON reads.

Every persisted record in the application is written through this module:
a sibling temporary file is flushed and ``fsync``-ed, then replaced over the
destination. A crash therefore leaves either the previous complete file or the
new complete file, never a truncated one.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = [
    "atomic_write_bytes",
    "atomic_write_json",
    "quarantine_file",
    "read_json_object",
]


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Replace ``path`` with ``data`` atomically.

    The temporary file lives in the destination directory so ``os.replace`` is a
    same-filesystem rename. On POSIX the directory is also fsynced, which is what
    makes the rename itself durable.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                directory = os.open(path.parent, os.O_RDONLY)
            except OSError:  # pragma: no cover - platform dependent
                return
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    """Serialise ``payload`` as UTF-8 JSON and replace ``path`` atomically."""

    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    atomic_write_bytes(Path(path), f"{text}\n".encode())


def read_json_object(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or return ``None`` when it is missing or unusable.

    Corruption is reported by the caller (which knows what the file means), not
    raised as an exception from a UI thread.
    """

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def quarantine_file(path: Path, *, reason: str = "corrupt") -> Path | None:
    """Move an unusable file aside so it is preserved but out of the way.

    The replacement name is unique, so repeated failures cannot overwrite the
    first preserved copy. Returns the new path, or ``None`` if the move failed.
    """

    path = Path(path)
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    target = path.with_name(f"{path.name}.{reason}-{stamp}")
    suffix = 1
    while target.exists():
        target = path.with_name(f"{path.name}.{reason}-{stamp}-{suffix}")
        suffix += 1
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target
