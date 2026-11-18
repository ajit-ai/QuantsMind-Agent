"""Discovery and classification of a project.

The interesting cases are the awkward ones: a project with no packaging metadata,
a docs folder full of Python, a directory of scripts that is not a package. A
classifier that guesses confidently through those is worse than one that says it
does not know, because the model acts on what it is told.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.project_intel import discover, package_aliases
from quantsmind_agent.services.workspace.containment import Workspace


def _project(root: Path) -> Workspace:
    return Workspace(root)


@pytest.fixture
def src_layout(tmp_path: Path) -> Workspace:
    (tmp_path / "src" / "acme" / "cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "conf.py").write_text("project = 'acme'\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "acme"\n\n[project.scripts]\nacme = "acme.cli.app:main"\n',
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Acme\n", encoding="utf-8")
    (tmp_path / "src" / "acme" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "acme" / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "acme" / "cli" / "app.py").write_text(
        "from acme.cli import runner\n\n\ndef main():\n    return runner.run()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "acme" / "cli" / "runner.py").write_text(
        "def run():\n    return 0\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_runner.py").write_text(
        "def test_run():\n    assert True\n", encoding="utf-8"
    )
    return _project(tmp_path)


@pytest.fixture
def flat_scripts(tmp_path: Path) -> Workspace:
    (tmp_path / "tool.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    return _project(tmp_path)


class TestClassification:
    def test_a_packaged_src_project_is_a_python_package(self, src_layout: Workspace) -> None:
        profile = discover(src_layout)

        assert profile.kind == "python-package"
        assert profile.confidence >= 0.75
        assert profile.is_python

    def test_the_src_layout_is_reported(self, src_layout: Workspace) -> None:
        profile = discover(src_layout)

        assert profile.layout == "src"
        assert "src" in profile.source_roots
        assert profile.primary_source_root == "src"

    def test_loose_scripts_are_an_application_not_a_package(self, flat_scripts: Workspace) -> None:
        profile = discover(flat_scripts)

        assert profile.kind == "python-application"
        # Fewer agreeing signals, and it says so rather than reading as certain.
        assert profile.confidence < 0.75

    def test_a_project_with_no_python_is_reported_as_unknown(self, tmp_path: Path) -> None:
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")

        profile = discover(_project(tmp_path))

        assert profile.kind == "unknown"
        assert profile.confidence == 0.0
        assert any("does not look like a Python project" in note for note in profile.unknowns)

    def test_missing_metadata_is_an_explicit_unknown(self, flat_scripts: Workspace) -> None:
        profile = discover(flat_scripts)

        assert any("build or packaging file" in note for note in profile.unknowns)

    def test_signals_are_listed_for_the_record(self, src_layout: Workspace) -> None:
        profile = discover(src_layout)

        assert any("pyproject.toml" in signal for signal in profile.signals)


class TestLayout:
    def test_test_roots_are_found(self, src_layout: Workspace) -> None:
        assert discover(src_layout).test_roots == ("tests",)

    def test_a_docs_directory_is_found_and_not_treated_as_source(
        self, src_layout: Workspace
    ) -> None:
        profile = discover(src_layout)

        assert profile.doc_roots == ("docs",)
        assert "docs/conf.py" not in profile.packages

    def test_packages_require_an_importable_directory(self, tmp_path: Path) -> None:
        (tmp_path / "loose").mkdir()
        (tmp_path / "loose" / "thing.py").write_text("X = 1\n", encoding="utf-8")
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "__init__.py").write_text("", encoding="utf-8")

        profile = discover(_project(tmp_path))

        assert "real" in profile.packages
        assert "loose" not in profile.packages

    def test_a_scripts_folder_is_not_a_package(self, tmp_path: Path) -> None:
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "deploy.py").write_text("print(1)\n", encoding="utf-8")

        assert "scripts" not in discover(_project(tmp_path)).packages

    def test_the_test_folder_is_not_a_source_root(self, src_layout: Workspace) -> None:
        assert "tests" not in discover(src_layout).source_roots


class TestBuildFilesAndEntryPoints:
    def test_the_packaging_file_is_recorded_by_role(self, src_layout: Workspace) -> None:
        profile = discover(src_layout)

        assert profile.build_file("packaging") == "pyproject.toml"
        assert profile.build_file("testing") == ""

    def test_setup_py_is_recognised(self, tmp_path: Path) -> None:
        (tmp_path / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("X = 1\n", encoding="utf-8")

        assert discover(_project(tmp_path)).build_file("packaging") == "setup.py"

    def test_requirements_are_found_by_prefix(self, tmp_path: Path) -> None:
        (tmp_path / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("X = 1\n", encoding="utf-8")

        profile = discover(_project(tmp_path))

        assert profile.build_file("dependencies") == "requirements-dev.txt"

    def test_console_scripts_are_read_from_pyproject(self, src_layout: Workspace) -> None:
        assert ("acme", "acme.cli.app:main") in discover(src_layout).entry_points

    def test_console_scripts_are_read_from_setup_cfg(self, tmp_path: Path) -> None:
        (tmp_path / "setup.cfg").write_text(
            "[metadata]\nname = x\n\n[console_scripts]\nthing = pkg.app:main\n",
            encoding="utf-8",
        )
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")

        assert ("thing", "pkg.app:main") in discover(_project(tmp_path)).entry_points

    def test_a_main_module_is_an_entry_point(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "pkg" / "__main__.py").write_text("print(1)\n", encoding="utf-8")

        entry_points = discover(_project(tmp_path)).entry_points

        assert any("python -m pkg" in name for name, _ in entry_points)

    def test_the_run_command_uses_the_imported_module_name(self, tmp_path: Path) -> None:
        # src/acme/__main__.py is imported as `acme`, not `src.acme`, so the
        # command has to say so or it will not run.
        (tmp_path / "src" / "acme").mkdir(parents=True)
        (tmp_path / "src" / "acme" / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "src" / "acme" / "__main__.py").write_text("print(1)\n", encoding="utf-8")

        entry_points = discover(_project(tmp_path)).entry_points

        assert any(name == "python -m acme" for name, _ in entry_points)

    def test_a_broken_pyproject_does_not_stop_discovery(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project\nname = broken", encoding="utf-8")
        (tmp_path / "app.py").write_text("X = 1\n", encoding="utf-8")

        profile = discover(_project(tmp_path))

        assert profile.file_count >= 1
        assert profile.kind


class TestAliases:
    def test_a_src_layout_maps_the_import_name_to_the_directory(
        self, src_layout: Workspace
    ) -> None:
        assert package_aliases(discover(src_layout))["acme"] == "src/acme"

    def test_a_flat_project_needs_no_aliases(self, flat_scripts: Workspace) -> None:
        assert package_aliases(discover(flat_scripts)) == {}

    def test_a_namespace_package_is_mapped_too(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "ns").mkdir(parents=True)
        (tmp_path / "src" / "ns" / "mod.py").write_text("X = 1\n", encoding="utf-8")

        assert package_aliases(discover(_project(tmp_path)))["ns"] == "src/ns"


class TestBoundedness:
    def test_a_huge_tree_is_walked_within_budget(self, tmp_path: Path) -> None:
        for index in range(120):
            (tmp_path / f"mod_{index}.py").write_text(f"X = {index}\n", encoding="utf-8")

        profile = discover(_project(tmp_path), max_entries=40)

        assert profile.truncated is True
        assert any("budget" in note for note in profile.unknowns)

    def test_a_small_tree_is_not_reported_as_truncated(self, src_layout: Workspace) -> None:
        assert discover(src_layout).truncated is False


class TestProfileSerialisation:
    def test_the_profile_round_trips_through_a_dict(self, src_layout: Workspace) -> None:
        data = discover(src_layout).to_dict()

        assert data["kind"] == "python-package"
        assert data["layout"] == "src"
        assert data["entry_points"][0]["command"] == "acme"
        assert isinstance(data["confidence"], float)

    def test_an_absent_build_file_is_an_empty_string_not_an_error(
        self, flat_scripts: Workspace
    ) -> None:
        assert discover(flat_scripts).build_file("packaging") == ""
