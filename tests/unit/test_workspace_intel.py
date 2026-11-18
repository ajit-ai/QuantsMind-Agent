"""Workspace snapshots: bounded, multi-language, and honest about coverage.

A snapshot is the only project-wide claim the agent makes before planning. The
tests here are about the edges that claim depends on: a non-Python repository is
still classified, an oversized repository says it was sampled, serialisation
survives a round trip, and a prompt never leaks the absolute workspace path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.verification import discover_checks
from quantsmind_agent.workspace_intel import (
    MAX_SNAPSHOT_FILES,
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotFile,
    WorkspaceIntelligence,
    WorkspaceSnapshot,
    discover_workspace,
)


def _workspace(root: Path) -> Workspace:
    return Workspace(root)


@pytest.fixture
def node_project(tmp_path: Path) -> Workspace:
    (tmp_path / "package.json").write_text(
        '{"name": "acme-web", "scripts": {"test": "vitest", "lint": "eslint ."}}',
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const n = 1;\n", encoding="utf-8")
    (tmp_path / "src" / "app.test.ts").write_text("test('n', () => {});\n", encoding="utf-8")
    return _workspace(tmp_path)


@pytest.fixture
def python_project(tmp_path: Path) -> Workspace:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "acme"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "acme").mkdir(parents=True)
    (tmp_path / "src" / "acme" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    return _workspace(tmp_path)


class TestClassification:
    def test_a_node_repository_is_named_as_one(self, node_project: Workspace) -> None:
        snapshot = discover_workspace(node_project)

        assert snapshot.ecosystems == ("node",)
        assert snapshot.project_kind == "node-package"
        assert "typescript" in snapshot.languages
        assert "package.json" in snapshot.manifests

    def test_python_keeps_the_richer_python_classification(self, python_project: Workspace) -> None:
        snapshot = discover_workspace(python_project)

        assert snapshot.project_kind == "python-package"
        assert snapshot.confidence >= 0.75
        assert "src" in snapshot.source_roots
        assert "tests" in snapshot.test_roots

    def test_two_ecosystems_are_reported_as_polyglot(self, node_project: Workspace) -> None:
        Path(node_project.root, "pyproject.toml").write_text(
            '[project]\nname = "mixed"\n', encoding="utf-8"
        )

        snapshot = discover_workspace(node_project)

        assert snapshot.ecosystems == ("python", "node")
        assert snapshot.project_kind == "polyglot"

    def test_a_repository_with_no_manifest_says_what_it_does_not_know(self, tmp_path: Path) -> None:
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")

        snapshot = discover_workspace(tmp_path)

        assert snapshot.project_kind == "unknown"
        assert snapshot.confidence == 0.0
        assert any("manifest" in note for note in snapshot.unknowns)
        assert any("no declared verification checks" in note for note in snapshot.unknowns)


class TestDeclaredChecks:
    def test_only_scripts_the_node_manifest_declares_are_discovered(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")

        checks = {check.id: check for check in discover_checks(tmp_path)}

        assert set(checks) == {"npm-test"}
        assert checks["npm-test"].argv == ("npm", "run", "test")
        assert checks["npm-test"].source == "package.json"

    def test_a_lock_file_selects_the_package_manager(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9'\n", encoding="utf-8")

        checks = discover_checks(tmp_path)

        assert checks[0].argv[:3] == ("pnpm", "run", "test")

    def test_a_cargo_manifest_declares_the_rust_suite(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "acme"\n', encoding="utf-8")

        checks = {check.id for check in discover_checks(tmp_path)}

        assert "cargo-test" in checks

    def test_a_snapshot_carries_the_declared_checks(self, python_project: Workspace) -> None:
        snapshot = discover_workspace(python_project)

        assert "pytest" in snapshot.check_ids


class TestBoundsAreVisible:
    def test_a_sampled_snapshot_says_it_is_sampled(self, tmp_path: Path) -> None:
        for index in range(20):
            (tmp_path / f"module_{index}.py").write_text("VALUE = 1\n", encoding="utf-8")

        snapshot = discover_workspace(tmp_path, max_files=5)

        assert snapshot.file_count == 20
        assert len(snapshot.files) == 5
        assert snapshot.sampled is True
        assert snapshot.complete is False
        assert any("only 5 of 20" in note for note in snapshot.unknowns)

    def test_an_entry_budget_is_reported_as_truncation(self, tmp_path: Path) -> None:
        for index in range(12):
            (tmp_path / f"module_{index}.py").write_text("VALUE = 1\n", encoding="utf-8")

        snapshot = discover_workspace(tmp_path, max_entries=4)

        assert snapshot.truncated is True
        assert snapshot.file_count == 4
        assert any("entry budget" in note for note in snapshot.unknowns)

    def test_the_default_file_budget_is_bounded(self) -> None:
        assert 0 < MAX_SNAPSHOT_FILES < 10_000


class TestLinks:
    def test_an_outward_junction_is_skipped_and_reported(
        self, outward_junction_project: Path
    ) -> None:
        snapshot = discover_workspace(outward_junction_project)

        paths = {entry.path for entry in snapshot.files}

        assert "src/safe.py" in paths
        assert not any("linked-outside" in path or "secret" in path for path in paths)
        assert any("link" in note and "not followed" in note for note in snapshot.unknowns)


class TestPromptRendering:
    def test_the_prompt_never_contains_the_absolute_workspace(
        self, python_project: Workspace
    ) -> None:
        snapshot = discover_workspace(python_project)

        rendered = snapshot.format_for_prompt()

        assert str(python_project.root) not in rendered
        assert "python-package" in rendered
        assert "pytest" in rendered
        assert "tests/test_app.py" in rendered

    def test_the_prompt_is_cut_to_its_character_budget(self, python_project: Workspace) -> None:
        rendered = discover_workspace(python_project).format_for_prompt(max_chars=80)

        assert len(rendered) <= 80
        assert rendered.endswith("...")


class TestSerialization:
    def test_a_snapshot_round_trips(self, node_project: Workspace) -> None:
        original = discover_workspace(node_project)

        decoded = WorkspaceSnapshot.from_dict(original.to_dict())

        assert decoded == original
        assert decoded.digest == original.digest
        assert decoded.check_ids == original.check_ids

    def test_a_future_snapshot_schema_is_refused(self) -> None:
        with pytest.raises(ValueError, match="newer"):
            WorkspaceSnapshot.from_dict(
                {"schema_version": SNAPSHOT_SCHEMA_VERSION + 1, "workspace": "/x"}
            )

    def test_a_file_entry_round_trips(self) -> None:
        entry = SnapshotFile(path="src/app.ts", size=12, kind="source", language="typescript")

        assert SnapshotFile.from_dict(entry.to_dict()) == entry


class TestIntelligence:
    def test_the_snapshot_is_cached_until_the_workspace_changes(
        self, python_project: Workspace
    ) -> None:
        intelligence = WorkspaceIntelligence(python_project)

        first = intelligence.snapshot
        assert intelligence.cached_snapshot is first

        (python_project.root / "src" / "acme" / "extra.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        intelligence.note_created("src/acme/extra.py")

        assert intelligence.cached_snapshot is None

    def test_refresh_rebuilds_the_snapshot(self, python_project: Workspace) -> None:
        intelligence = WorkspaceIntelligence(python_project)
        first = intelligence.snapshot

        refreshed = intelligence.refresh_snapshot()

        assert refreshed is not first
        assert refreshed.digest == first.digest

    def test_planning_context_is_the_bounded_rendering(self, python_project: Workspace) -> None:
        intelligence = WorkspaceIntelligence(python_project)

        rendered = intelligence.planning_context(max_chars=120)

        assert rendered == intelligence.snapshot.format_for_prompt(max_chars=120)
        assert str(python_project.root) not in rendered
