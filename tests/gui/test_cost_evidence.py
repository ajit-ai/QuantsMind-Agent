"""Cost meter + evidence export tests (v1.2 wiring)."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QPushButton

from quantsmind_agent.gui.studio.changesets import (
    ChangeFile,
    ChangeSetRecord,
    EngineeringChangeSets,
)
from quantsmind_agent.gui.task.panel import TaskPanel

pytestmark = pytest.mark.gui


def test_cost_meter_shows_estimate(qapp: object) -> None:
    panel = TaskPanel()
    panel.set_cost_meter(1000)
    assert "$" in panel.cost_meter.text() or "USD" in panel.cost_meter.text()


def test_evidence_button_present(qapp: object) -> None:
    w = EngineeringChangeSets()
    w.set_records(
        [
            ChangeSetRecord(
                task_id="t1",
                objective="o",
                status="approved",
                approval="approved",
                files=(ChangeFile("a.py", "edit", 10, 20, True, True),),
            )
        ]
    )
    labels = [b.text() for b in w.findChildren(QPushButton)]
    assert any("Export evidence pack" in t for t in labels)
