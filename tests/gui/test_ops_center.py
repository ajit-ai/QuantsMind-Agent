"""Control Center v1.4 ops tests."""

from __future__ import annotations

import pytest

from quantsmind_agent.gui.studio.control_center import ControlCenter

pytestmark = pytest.mark.gui


def test_ops_buttons_emitted(qapp: object) -> None:
    center = ControlCenter()
    seen: list[str] = []
    center.support_pack_requested.connect(lambda: seen.append("support"))
    center.backup_requested.connect(lambda: seen.append("backup"))
    center.restore_requested.connect(lambda: seen.append("restore"))
    center.performance_requested.connect(lambda: seen.append("perf"))
    center.support_pack_requested.emit()
    center.backup_requested.emit()
    center.restore_requested.emit()
    center.performance_requested.emit()
    assert seen == ["support", "backup", "restore", "perf"]
    assert center.ops_status.text()
