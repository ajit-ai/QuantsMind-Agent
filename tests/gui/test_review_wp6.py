"""WP6 advanced review GUI tests."""

from __future__ import annotations

import pytest

from quantsmind_agent.gui.studio.changesets import (
    ChangeCheck,
    ChangeFile,
    ChangeSetRecord,
    EngineeringChangeSets,
)

pytestmark = pytest.mark.gui


def _record(**kw) -> ChangeSetRecord:
    base = {
        "task_id": "t1",
        "objective": "Do thing",
        "status": "approved",
        "approval": "approved",
        "verification": "pass",
        "files": (
            ChangeFile("new.py", "create", 0, 10, False, True),
            ChangeFile("mod.py", "edit", 100, 120, True, True),
            ChangeFile("old.py", "delete", 50, 0, True, False),
        ),
        "checks": (ChangeCheck("pytest", "pass", 0), ChangeCheck("lint", "fail", 1, "ruff error")),
        "recovery": ("journal restored",),
    }
    base.update(kw)
    return ChangeSetRecord(**base)


class TestReview:
    def test_file_kinds_and_counts(self, qapp: object) -> None:
        w = EngineeringChangeSets()
        w.set_records((_record(),))
        d = w.describe()
        assert d["added"] == 1 and d["modified"] == 1 and d["deleted"] == 1
        assert d["failed_checks"] == 1
        assert d["recovery_notes"] == 1
        assert EngineeringChangeSets.file_kind(ChangeFile("a", "create", 0, 1, False, True)) == "A"
        assert EngineeringChangeSets.file_kind(ChangeFile("b", "edit", 1, 2, True, True)) == "M"
        assert EngineeringChangeSets.file_kind(ChangeFile("c", "delete", 1, 0, True, False)) == "D"

    def test_no_auto_approval(self, qapp: object) -> None:
        w = EngineeringChangeSets()  # no context: engine unwired
        w.set_records((_record(),))
        assert w.describe()["apply_enabled"] is False
        assert w.describe()["rollback_enabled"] is False

    def test_diff_navigation(self, qapp: object) -> None:
        w = EngineeringChangeSets()
        seen: list[str] = []
        w.file_selected.connect(seen.append)
        w.set_records((_record(),))
        w.next_file()
        w.previous_file()
        assert seen  # navigation emitted file paths

    def test_loading_and_error_states(self, qapp: object) -> None:
        w = EngineeringChangeSets()
        w.set_loading(True)
        # Loading placeholder present in detail layout.
        labels = [lbl.text() for lbl in w.detail_host.findChildren(object) if hasattr(lbl, "text")]
        assert any("Loading" in str(t) for t in labels) or w.detail_layout.count() > 0
        w.set_error("boom")
        assert w.describe()["records"] == 0

    def test_empty_state(self, qapp: object) -> None:
        w = EngineeringChangeSets()
        w.set_records(())
        assert w.describe()["records"] == 0
