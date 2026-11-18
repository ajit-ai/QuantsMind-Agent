"""Unit tests for import parsing and workspace-relative resolution.

The resolver's job is to turn an import statement into a real file, and -- just
as important -- to say "I could not" instead of guessing. A wrong resolution
would send the model to edit the wrong file, so every case here is pinned,
including the ones that cannot be resolved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.code_imports import (
    ResolvedImport,
    module_name_for,
    parse_imports,
    resolve_import,
)
from quantsmind_agent.services.workspace.containment import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "store.py").write_text("class Store:\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "deep.py").write_text("", encoding="utf-8")
    (tmp_path / "top.py").write_text("", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# not python\n", encoding="utf-8")
    return Workspace(tmp_path)


class TestParseImports:
    def test_a_plain_import_binds_its_top_level_name(self) -> None:
        records = parse_imports("import os\nimport pkg.store\n")

        assert [r.module for r in records] == ["os", "pkg.store"]
        assert records[0].is_from is False
        assert records[0].names == (("os", None),)
        assert records[1].names == (("pkg", None),)

    def test_a_from_import_records_every_name(self) -> None:
        records = parse_imports("from pkg.store import Store, Loader as L\n")

        assert records[0].module == "pkg.store"
        assert records[0].is_from is True
        assert records[0].names == (("Store", None), ("Loader", "L"))
        assert records[0].line == 1

    def test_a_relative_import_keeps_its_level(self) -> None:
        records = parse_imports("from . import sibling\nfrom ..other import thing\n")

        assert records[0].level == 1
        assert records[1].level == 2
        assert records[1].module == "other"

    def test_line_numbers_are_recorded(self) -> None:
        records = parse_imports("x = 1\nimport os\n")

        assert records[0].line == 2

    def test_a_syntax_error_is_raised_for_the_caller(self) -> None:
        with pytest.raises(SyntaxError):
            parse_imports("def broken(:\n")

    def test_a_file_without_imports_parses_to_nothing(self) -> None:
        assert parse_imports("x = 1\n") == []


class TestModuleNames:
    def test_a_module_file_maps_to_its_dotted_name(self, workspace: Workspace) -> None:
        assert module_name_for(workspace, "pkg/store.py") == "pkg.store"

    def test_an_init_maps_to_its_package(self, workspace: Workspace) -> None:
        assert module_name_for(workspace, "pkg/__init__.py") == "pkg"

    def test_a_top_level_module(self, workspace: Workspace) -> None:
        assert module_name_for(workspace, "top.py") == "top"

    def test_a_non_python_file_has_no_module(self, workspace: Workspace) -> None:
        assert module_name_for(workspace, "notes.md") == ""

    def test_a_path_outside_the_workspace_has_no_module(self, workspace: Workspace) -> None:
        assert module_name_for(workspace, "../escape.py") == ""


class TestResolution:
    def _resolve(self, workspace: Workspace, source: str, importer: str) -> list[ResolvedImport]:
        records = parse_imports(source)
        return [
            binding for record in records for binding in resolve_import(workspace, record, importer)
        ]

    def test_an_absolute_from_import_resolves_to_the_module_file(
        self, workspace: Workspace
    ) -> None:
        bindings = self._resolve(workspace, "from pkg.store import Store\n", "top.py")

        assert [(b.path, b.bound_name, b.imported_name) for b in bindings] == [
            ("pkg/store.py", "Store", "Store")
        ]

    def test_an_alias_binds_the_local_name(self, workspace: Workspace) -> None:
        bindings = self._resolve(workspace, "from pkg.store import Store as S\n", "top.py")

        assert bindings[0].bound_name == "S"
        assert bindings[0].imported_name == "Store"

    def test_a_package_import_resolves_to_its_init(self, workspace: Workspace) -> None:
        # ``from pkg import Registry`` where no submodule of that name exists --
        # deliberately a name that cannot collide with ``store`` by case, which on
        # a case-insensitive filesystem would otherwise find ``pkg/store.py``. The
        # name has to come from the package marker, the only file that can bind it.
        bindings = self._resolve(workspace, "from pkg import Registry\n", "top.py")

        assert bindings[0].path == "pkg/__init__.py"

    def test_a_package_import_prefers_the_submodule_it_names(self, workspace: Workspace) -> None:
        # ``from pkg import store`` names a submodule. Python imports it, and the
        # agent needs that file rather than the package marker.
        bindings = self._resolve(workspace, "from pkg import store\n", "top.py")

        assert bindings[0].path == "pkg/store.py"
        assert bindings[0].kind == "submodule"

    def test_a_relative_package_import_prefers_the_submodule(self, workspace: Workspace) -> None:
        bindings = self._resolve(workspace, "from . import store\n", "pkg/app.py")

        assert bindings[0].path == "pkg/store.py"

    def test_a_plain_import_resolves_the_module_file(self, workspace: Workspace) -> None:
        bindings = self._resolve(workspace, "import pkg.store\n", "top.py")

        assert bindings[0].path == "pkg/store.py"
        assert bindings[0].kind == "module"
        assert bindings[0].bound_name == "pkg"

    def test_a_relative_import_resolves_inside_the_package(self, workspace: Workspace) -> None:
        bindings = self._resolve(workspace, "from .store import Store\n", "pkg/reader.py")

        assert bindings[0].path == "pkg/store.py"

    def test_a_relative_import_from_a_package_init(self, workspace: Workspace) -> None:
        bindings = self._resolve(workspace, "from .store import Store\n", "pkg/__init__.py")

        assert bindings[0].path == "pkg/store.py"

    def test_a_double_relative_import_goes_up_one_package(self, workspace: Workspace) -> None:
        bindings = self._resolve(workspace, "from ..store import Store\n", "pkg/sub/app.py")

        assert bindings[0].path == "pkg/store.py"

    def test_a_relative_import_inside_a_subpackage_resolves(self, workspace: Workspace) -> None:
        bindings = self._resolve(workspace, "from .deep import thing\n", "pkg/sub/app.py")

        assert bindings[0].path == "pkg/sub/deep.py"

    def test_a_relative_import_that_climbs_above_the_root_yields_nothing(
        self, workspace: Workspace
    ) -> None:
        # ``pkg/other.py`` is one level below the root, so ``..`` aims above it.
        assert self._resolve(workspace, "from ..sub.deep import thing\n", "pkg/other.py") == []

    def test_a_star_import_binds_nothing(self, workspace: Workspace) -> None:
        # ``from x import *`` creates no name we can name, so reporting a binding
        # for it would be fiction.
        bindings = self._resolve(workspace, "from pkg.store import *\n", "top.py")

        assert bindings == []

    def test_an_unresolvable_import_yields_nothing(self, workspace: Workspace) -> None:
        assert self._resolve(workspace, "from third_party import Thing\n", "top.py") == []

    def test_a_standard_library_import_yields_nothing(self, workspace: Workspace) -> None:
        assert self._resolve(workspace, "import json\n", "top.py") == []

    def test_a_relative_import_above_the_root_yields_nothing(self, workspace: Workspace) -> None:
        assert self._resolve(workspace, "from ... import nope\n", "pkg/store.py") == []

    def test_several_bindings_from_one_statement(self, workspace: Workspace) -> None:
        bindings = self._resolve(workspace, "from pkg.store import Store, Loader\n", "top.py")

        assert [b.bound_name for b in bindings] == ["Store", "Loader"]
        assert {b.line for b in bindings} == {1}
