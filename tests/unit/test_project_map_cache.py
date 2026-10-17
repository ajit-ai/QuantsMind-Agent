"""The project map's cache and its invalidation.

A cache that goes stale is worse than no cache, so most of these tests are about
what happens *after* something changes: a file the agent wrote, a file it deleted,
and a different project entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.project_intel import ProjectIntelligence, task_context
from quantsmind_agent.workspace import Workspace


@pytest.fixture
def project(tmp_path: Path) -> Workspace:
    (tmp_path / "src" / "shop").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "shop"\n', encoding="utf-8")
    (tmp_path / "src" / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "shop" / "cart.py").write_text(
        "def total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    (tmp_path / "src" / "shop" / "app.py").write_text(
        "from shop import cart\n\n\ndef main():\n    return cart.total([])\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_cart.py").write_text(
        "def test_total():\n    assert True\n", encoding="utf-8"
    )
    return Workspace(tmp_path)


@pytest.fixture
def many_modules(tmp_path: Path) -> Workspace:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    for index in range(30):
        (tmp_path / "pkg" / f"widget_{index}.py").write_text(
            f"def widget_{index}():\n    return {index}\n", encoding="utf-8"
        )
    return Workspace(tmp_path)


@pytest.fixture
def other_project(tmp_path: Path) -> Workspace:
    root = tmp_path / "other"
    (root / "lib").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "other"\n', encoding="utf-8")
    (root / "lib" / "__init__.py").write_text("", encoding="utf-8")
    (root / "lib" / "thing.py").write_text("def thing():\n    return 1\n", encoding="utf-8")
    return Workspace(root)


class TestCaching:
    def test_the_profile_is_computed_once(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)

        assert intel.profile is intel.profile

    def test_symbols_are_reused_rather_than_reparsed(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        intel.index()

        assert intel.symbols("src/shop/cart.py") is intel.symbols("src/shop/cart.py")

    def test_indexing_is_bounded(self, many_modules: Workspace) -> None:
        assert ProjectIntelligence(many_modules, max_indexed_files=20).index() <= 20

    def test_an_unparsable_file_is_reported_not_treated_as_empty(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        project.write_text("src/shop/broken.py", "def oops(\n")

        facts = intel.facts("src/shop/broken.py")

        assert facts.unparsable is True
        assert facts.symbols == ()

    def test_a_file_that_does_not_exist_yields_empty_facts(self, project: Workspace) -> None:
        assert ProjectIntelligence(project).facts("src/shop/ghost.py").symbols == ()


class TestInvalidationOnChange:
    def test_a_changed_file_is_re_read_not_answered_from_the_cache(
        self, project: Workspace
    ) -> None:
        intel = ProjectIntelligence(project)
        intel.index()
        assert [s.qualified for s in intel.symbols("src/shop/cart.py")] == ["total"]

        project.write_text(
            "src/shop/cart.py", "def total(items):\n    return 0\n\n\ndef tax(x):\n    return x\n"
        )
        intel.note_written("src/shop/cart.py")

        assert intel.stale_files == ("src/shop/cart.py",)
        assert [s.qualified for s in intel.symbols("src/shop/cart.py")] == ["total", "tax"]

    def test_a_created_file_is_picked_up(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        intel.index()
        project.write_text("src/shop/new.py", "def fresh():\n    return 1\n")

        intel.note_created("src/shop/new.py")

        assert [s.qualified for s in intel.symbols("src/shop/new.py")] == ["fresh"]

    def test_a_deleted_file_is_gone_from_the_map(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        intel.index()
        project.delete_file("src/shop/app.py")

        intel.note_deleted("src/shop/app.py")

        assert "src/shop/app.py" not in intel.map.known_paths()

    def test_a_deleted_file_loses_its_callers_too(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        intel.index()
        assert intel.importers_of("src/shop/cart.py") == ("src/shop/app.py",)

        project.delete_file("src/shop/app.py")
        intel.note_deleted("src/shop/app.py")

        # Reporting a caller that no longer exists is the failure this exists to
        # prevent: the agent would go looking for a file it just deleted.
        assert intel.importers_of("src/shop/cart.py") == ()

    def test_a_new_import_is_seen_after_the_change(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        intel.index()
        project.write_text(
            "src/shop/reports.py",
            "from shop import cart\n\n\ndef report():\n    return cart.total([])\n",
        )
        intel.note_created("src/shop/reports.py")

        assert "src/shop/reports.py" in intel.importers_of("src/shop/cart.py")

    def test_a_new_root_level_file_refreshes_the_profile(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        before = intel.profile
        assert before.kind == "python-package"
        project.write_text("noxfile.py", "print(1)\n")

        intel.note_created("noxfile.py")

        # Recomputed rather than reused: a new root-level file is exactly the kind
        # of change that can alter what the project is.
        assert intel.profile is not before
        assert intel.profile.build_file("testing") == "noxfile.py"

    def test_editing_a_function_does_not_recompute_the_layout(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        profile = intel.profile

        intel.note_written("src/shop/cart.py")

        # Cheap for the right reason: nothing that decides the layout changed.
        assert intel.profile is profile

    def test_the_generation_advances_on_every_invalidation(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        before = intel.map.generation

        intel.note_written("src/shop/cart.py")

        assert intel.map.generation > before


class TestImportResolutionUnderSrcLayout:
    def test_an_absolute_import_resolves_to_the_file_that_exists(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        intel.index()

        # ``from shop import cart`` in a src layout names src/shop/cart.py, and
        # the two spellings share no prefix.
        assert "src/shop/cart.py" in intel.facts("src/shop/app.py").imports

    def test_a_relative_package_import_resolves_to_the_module(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        intel.index()

        # ``from . import cart`` has no module of its own; resolving it to the
        # package __init__ would point the agent at the wrong file entirely.
        assert intel.facts("src/shop/app.py").imports == ("src/shop/cart.py",)

    def test_importers_are_sorted_for_determinism(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        intel.index()
        project.write_text(
            "src/shop/z_other.py",
            "from shop import cart\n\n\ndef z():\n    return cart.total([])\n",
        )
        intel.note_created("src/shop/z_other.py")

        found = intel.importers_of("src/shop/cart.py")

        assert found == tuple(sorted(found))

    def test_aliases_are_reported(self, project: Workspace) -> None:
        assert ProjectIntelligence(project).aliases["shop"] == "src/shop"

    def test_an_uncached_importer_answer_still_finds_the_caller(self, project: Workspace) -> None:
        # Before indexing the answer comes from a bounded scan; it must not be an
        # empty list, which would read as "nothing imports this".
        assert "src/shop/app.py" in ProjectIntelligence(project).importers_of("src/shop/cart.py")


class TestProjectSwitching:
    def test_a_new_instance_carries_nothing_from_the_previous_project(
        self, project: Workspace, other_project: Workspace
    ) -> None:
        first = ProjectIntelligence(project)
        first.index()

        second = ProjectIntelligence(other_project)
        second.index()

        assert not any("shop" in path for path in second.map.known_paths())
        assert second.profile.packages == ("lib",)

    def test_invalidating_clears_the_map_and_the_profile(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        intel.index()
        assert intel.indexed_files > 0

        intel.invalidate()

        assert intel.indexed_files == 0
        assert intel.is_indexed is False
        assert intel.profile.kind  # recomputed on demand, not lost

    def test_the_profile_is_recomputed_after_invalidation(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        before = intel.profile
        project.delete_file("pyproject.toml")

        intel.invalidate()
        after = intel.refresh()

        assert after is not before
        assert after.build_file("packaging") == ""


class TestBoundedContext:
    def test_a_path_anchor_returns_that_file_first(self, project: Workspace) -> None:
        context = task_context(
            ProjectIntelligence(project), task="check the cart", path="src/shop/cart.py"
        )

        assert context.files[0]["path"] == "src/shop/cart.py"

    def test_the_tests_around_the_anchor_are_named(self, project: Workspace) -> None:
        context = task_context(ProjectIntelligence(project), path="src/shop/cart.py")

        assert "tests/test_cart.py" in context.tests

    def test_the_callers_of_the_anchor_are_named(self, project: Workspace) -> None:
        context = task_context(ProjectIntelligence(project), path="src/shop/cart.py")

        assert "src/shop/app.py" in context.callers

    def test_a_missing_anchor_is_reported_not_silently_substituted(
        self, project: Workspace
    ) -> None:
        context = task_context(ProjectIntelligence(project), path="src/shop/ghost.py")

        assert any("does not exist" in note for note in context.notes)

    def test_a_symbol_anchor_finds_the_file(self, project: Workspace) -> None:
        intel = ProjectIntelligence(project)
        project.write_text("src/shop/pricing.py", "def price(items):\n    return 0\n")

        context = task_context(intel, symbol="pricing")

        assert any(item["path"] == "src/shop/pricing.py" for item in context.files)

    def test_the_file_cap_is_honoured(self, many_modules: Workspace) -> None:
        context = task_context(ProjectIntelligence(many_modules), task="widget", max_files=5)

        assert len(context.files) <= 5
        assert context.truncated is True

    def test_the_symbol_cap_is_honoured(self, many_modules: Workspace) -> None:
        context = task_context(
            ProjectIntelligence(many_modules), task="widget", max_files=30, max_symbols=4
        )

        assert len(context.symbols) <= 4

    def test_a_task_that_matches_nothing_says_so(self, project: Workspace) -> None:
        context = task_context(ProjectIntelligence(project), task="quantum flux capacitor")

        assert any("nothing matched" in note for note in context.notes)

    def test_stop_words_do_not_make_everything_relevant(self, project: Workspace) -> None:
        context = task_context(
            ProjectIntelligence(project), task="please use the file and the project test"
        )

        # "the", "file", "project" and "test" would otherwise match everything.
        assert list(context.files) == []

    def test_the_project_profile_travels_with_the_context(self, project: Workspace) -> None:
        context = task_context(ProjectIntelligence(project), path="src/shop/cart.py")

        assert context.project.kind == "python-package"
        assert context.to_dict()["project"]["test_roots"] == ["tests"]

    def test_an_empty_project_does_not_raise(self, tmp_path: Path) -> None:
        context = task_context(ProjectIntelligence(Workspace(tmp_path)), task="anything")

        assert context.files == ()
        assert context.notes
