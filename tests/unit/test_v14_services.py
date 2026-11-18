"""v1.4 tests: support pack, backup/restore, performance governor."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from quantsmind_agent.services.backup import backup_workspace, restore_workspace
from quantsmind_agent.services.performance import govern_budget
from quantsmind_agent.services.support_pack import build_support_pack, write_support_pack


def test_support_pack(tmp_path: Path) -> None:
    pack = build_support_pack(tmp_path)
    assert pack["destinations"] == ["home", "ai", "development", "changes", "control"]
    assert pack["signing"]["authorized"] is False
    out = write_support_pack(tmp_path, tmp_path / "support.json")
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == pack["version"]


def test_backup_restore_roundtrip(tmp_path: Path) -> None:
    (tmp_path / ".quantsmind").mkdir(parents=True)
    (tmp_path / ".quantsmind" / "profiles.json").write_text("{}")
    archive = backup_workspace(tmp_path, tmp_path / "backup.zip")
    with zipfile.ZipFile(archive) as zf:
        assert "meta.json" in zf.namelist()
        assert "profiles.json" in zf.namelist()
    (tmp_path / ".quantsmind" / "profiles.json").unlink()
    restored = restore_workspace(archive, tmp_path)
    assert restored == ["profiles.json"]
    assert (tmp_path / ".quantsmind" / "profiles.json").exists()


def test_govern_budget_clamps() -> None:
    low = govern_budget(timeout_seconds=120, pressure="low")
    assert low.timeout_seconds == 120.0
    critical = govern_budget(timeout_seconds=120, max_output_chars=20000, pressure="critical")
    assert critical.timeout_seconds == 12.0
    assert critical.max_output_chars == 5000
    unknown = govern_budget(pressure="unknown")
    assert unknown.timeout_seconds == 120.0
