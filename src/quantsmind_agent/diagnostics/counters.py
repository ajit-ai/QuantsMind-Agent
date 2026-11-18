"""Local, opt-in usage counters (WP4.3 of v1.3)."""

from __future__ import annotations

import json
import time
from pathlib import Path

__all__ = ["CounterStore"]


class CounterStore:
    """JSONL counters at ``<config>/counters.jsonl``; never leaves the device.

    Collection requires explicit opt-in (``enabled=True`` set by user in
    Settings). No network, no identifiers beyond coarse feature names.
    """

    def __init__(self, root: str | Path, *, enabled: bool = False) -> None:
        self._path = Path(root) / "counters.jsonl"
        self._enabled = enabled
        self._counts: dict[str, int] = {}
        self.load()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def load(self) -> None:
        self._counts = {}
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    data = json.loads(line)
                    name = str(data.get("name", ""))
                    if name:
                        self._counts[name] = self._counts.get(name, 0) + int(data.get("n", 1))
        except (OSError, json.JSONDecodeError):
            pass

    def increment(self, name: str, n: int = 1) -> None:
        if not self._enabled or not name:
            return
        self._counts[name] = self._counts.get(name, 0) + n
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"name": name, "n": n, "at": time.time()}) + "\n")

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)
