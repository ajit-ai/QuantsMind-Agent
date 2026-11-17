"""Acceptance tests closing partial-coverage gaps for product-critical services.

Features covered:
- F23 CostRouter / estimate_cost (routing failure, budget, boundaries)
- F76 Evidence export (read-only ZIP, manifest, no secrets)
- F77 Support pack (redaction, counts only)
- F78 Backup/restore workspace state (round-trip, missing files, errors)
- F79 Performance governor (clamping, pressure degradation, boundaries)

These tests verify observable behaviour including success, failure, boundary
and negative cases — not mere instantiation.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from quantsmind_agent.providers.cost_router import CostRouter, ModelTier, estimate_cost
from quantsmind_agent.services.backup import backup_workspace, restore_workspace
from quantsmind_agent.services.evidence import export_evidence_pack
from quantsmind_agent.services.performance import GovernedBudget, govern_budget
from quantsmind_agent.services.support_pack import build_support_pack, write_support_pack


# -- F23 Cost Router ----------------------------------------------------------
class TestCostRouter:
    def test_estimate_cost_boundaries(self) -> None:
        tier = ModelTier("fast", 0.10, 800, 1)
        assert estimate_cost(0, tier) == 0.0
        assert estimate_cost(1000, tier) == pytest.approx(0.10)
        assert estimate_cost(2500, tier) == pytest.approx(0.25)

    def test_choose_respects_budget(self) -> None:
        router = CostRouter()
        # Tiny budget forces the cheapest tier.
        chosen = router.choose(10_000, budget_usd=0.5)
        assert chosen.name == "fast"

    def test_choose_respects_min_quality(self) -> None:
        router = CostRouter()
        chosen = router.choose(1000, budget_usd=100.0, min_quality=3)
        assert chosen.quality >= 3
        assert chosen.name == "strong"

    def test_choose_falls_back_when_no_tier_fits_budget(self) -> None:
        router = CostRouter()
        # Budget of zero cannot be met; must still return a tier (cheapest).
        chosen = router.choose(1000, budget_usd=0.0)
        assert chosen.name == "fast"

    def test_choose_empty_tier_list_falls_back(self) -> None:
        # An empty tiers list is falsy, so the router installs its defaults.
        router = CostRouter(tiers=[])
        chosen = router.choose(1000)
        assert isinstance(chosen, ModelTier)
        assert len(router.tiers) == 3

    def test_meter_reports_cost_and_latency(self) -> None:
        router = CostRouter()
        tier = ModelTier("fast", 0.10, 800, 1)
        meter = router.meter(2000, tier)
        assert meter["tier"] == "fast"
        assert meter["cost_usd"] == pytest.approx(0.2)
        assert meter["latency_ms"] == 800


# -- F76 Evidence Export ------------------------------------------------------
class TestEvidenceExport:
    def test_export_produces_read_only_zip_with_manifest(self, tmp_path: Path) -> None:
        dest = tmp_path / "evidence.zip"
        returned = export_evidence_pack(
            dest,
            audit_events=[{"event": "a"}],
            changesets=[{"id": "c"}],
            verification=[{"check": "v"}],
        )
        assert returned == dest
        assert zipfile.is_zipfile(dest)
        with zipfile.ZipFile(dest) as zf:
            names = set(zf.namelist())
            assert {
                "meta.json",
                "audit.json",
                "changesets.json",
                "verification.json",
                "manifest.json",
            } <= names
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["audit_records"] == 1
            assert manifest["changesets"] == 1
            assert manifest["checks"] == 1
            meta = json.loads(zf.read("meta.json"))
            assert meta["format"] == "quantsmind-evidence/1"

    def test_export_never_writes_to_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        before = sorted(p.name for p in workspace.iterdir())
        dest = tmp_path / "out" / "evidence.zip"
        export_evidence_pack(dest, audit_events=[], changesets=[], verification=[])
        after = sorted(p.name for p in workspace.iterdir())
        assert before == after

    def test_export_handles_empty_inputs(self, tmp_path: Path) -> None:
        dest = tmp_path / "empty.zip"
        export_evidence_pack(dest, audit_events=[], changesets=[], verification=[])
        with zipfile.ZipFile(dest) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["audit_records"] == 0


# -- F77 Support Pack ---------------------------------------------------------
class TestSupportPack:
    def test_build_pack_reports_version_destinations_and_no_signing(self, tmp_path: Path) -> None:
        pack = build_support_pack(tmp_path)
        assert pack["version"] == "0.9.0"
        assert pack["destinations"] == ["home", "ai", "development", "changes", "control"]
        assert pack["signing"]["authorized"] is False

    def test_build_pack_includes_task_counts_only(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".quantsmind"
        state_dir.mkdir()
        (state_dir / "task_state.json").write_text(
            json.dumps({"t1": {}, "t2": {}}), encoding="utf-8"
        )
        pack = build_support_pack(tmp_path)
        assert pack["task_state"]["tasks"] == 2
        # No task bodies leaked into the pack.
        assert "t1" not in json.dumps(pack)

    def test_write_support_pack_creates_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "support" / "pack.json"
        returned = write_support_pack(tmp_path, dest)
        assert returned == dest
        assert dest.is_file()
        assert json.loads(dest.read_text(encoding="utf-8"))["version"] == "0.9.0"


# -- F78 Backup / Restore -----------------------------------------------------
class TestBackupRestore:
    def test_round_trip_preserves_state_files(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        state_dir = root / ".quantsmind"
        state_dir.mkdir(parents=True)
        (state_dir / "profiles.json").write_text('{"profiles": []}', encoding="utf-8")
        (state_dir / "memory.json").write_text('{"memories": []}', encoding="utf-8")
        archive = tmp_path / "backup.zip"
        backup_workspace(root, archive)
        assert zipfile.is_zipfile(archive)

        # Restore into a fresh root.
        restore_root = tmp_path / "restored"
        restored = restore_workspace(archive, restore_root)
        assert "profiles.json" in restored
        assert "memory.json" in restored
        assert (restore_root / ".quantsmind" / "profiles.json").read_text(
            encoding="utf-8"
        ) == '{"profiles": []}'

    def test_backup_with_no_state_dir_produces_valid_archive(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        archive = tmp_path / "backup.zip"
        backup_workspace(root, archive)
        with zipfile.ZipFile(archive) as zf:
            assert "meta.json" in zf.namelist()

    def test_restore_missing_files_returns_empty(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        state_dir = root / ".quantsmind"
        state_dir.mkdir(parents=True)
        archive = tmp_path / "backup.zip"
        backup_workspace(root, archive)
        restored = restore_workspace(archive, tmp_path / "fresh")
        assert restored == []


# -- F79 Performance Governor -------------------------------------------------
class TestPerformanceGovernor:
    def test_low_pressure_keeps_full_budget(self) -> None:
        budget = govern_budget(timeout_seconds=120.0, max_output_chars=20000, pressure="low")
        assert budget.timeout_seconds == 120.0
        assert budget.max_output_chars == 20000
        assert isinstance(budget, GovernedBudget)

    def test_critical_pressure_degrades_budget(self) -> None:
        budget = govern_budget(timeout_seconds=120.0, max_output_chars=20000, pressure="critical")
        assert budget.timeout_seconds < 120.0
        assert budget.max_output_chars < 20000

    def test_timeout_clamped_to_bounds(self) -> None:
        low = govern_budget(timeout_seconds=0.1, pressure="critical")
        assert low.timeout_seconds >= 1.0
        high = govern_budget(timeout_seconds=100000.0, pressure="low")
        assert high.timeout_seconds <= 600.0

    def test_output_clamped_to_bounds(self) -> None:
        low = govern_budget(max_output_chars=1, pressure="critical")
        assert low.max_output_chars >= 2000

    def test_unknown_pressure_defaults_to_no_degradation(self) -> None:
        budget = govern_budget(timeout_seconds=120.0, pressure="unknown")
        assert budget.timeout_seconds == 120.0
