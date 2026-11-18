"""v1.3 tests: telemetry + shortcuts."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.diagnostics.counters import CounterStore
from quantsmind_agent.gui.shell.shortcuts import SHORTCUTS, ShortcutMapDialog

pytestmark = pytest.mark.gui


def test_counters_opt_in(tmp_path: Path) -> None:
    store = CounterStore(tmp_path, enabled=False)
    store.increment("cmd_run")
    assert store.snapshot() == {}
    store.set_enabled(True)
    store.increment("cmd_run", 2)
    assert CounterStore(tmp_path, enabled=True).snapshot() == {"cmd_run": 2}


def test_shortcut_map_lists_destinations(qapp: object) -> None:
    assert any("Ctrl+1" in keys for keys, _ in SHORTCUTS)
    dialog = ShortcutMapDialog()
    assert dialog.list.count() == len(SHORTCUTS)
