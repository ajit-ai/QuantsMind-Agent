"""Phase 0.10 workspace service tests.

Covers the services the IDE shell depends on: containment, explorer listing,
text search, quick open, user-initiated file operations and the polling watcher.
None of these tests need Qt.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from quantsmind_agent.errors import InvalidPathError, QuantsMindError
from quantsmind_agent.services.workspace.containment import (
    DEFAULT_BINARY_SUFFIXES,
    DEFAULT_IGNORED_DIRECTORIES,
    Workspace,
    WorkspacePath,
    workspace_root_id,
)
from quantsmind_agent.services.workspace.explorer import (
    ExplorerService,
    SearchQuery,
)
from quantsmind_agent.services.workspace.operations import (
    FileOperationError,
    FileOperationService,
    TrashUnavailable,
)
from quantsmind_agent.services.workspace.watcher import WorkspaceWatcher


class TestContainment:
    def test_root_is_canonical(self, sample_project: Path) -> None:
        assert Workspace(sample_project).root == sample_project.resolve()

    @pytest.mark.parametrize(
        "candidate",
        [
            "../outside.txt",
            "/etc/passwd",
            r"C:\Windows\win.ini",
            r"\\server\share\file",
            "~/notes.md",
            "",
            "src/\x00main.py",
        ],
    )
    def test_refuses_paths_that_leave_the_root(self, workspace: Workspace, candidate: str) -> None:
        with pytest.raises(InvalidPathError):
            workspace.resolve(candidate)

    def test_symlinked_child_cannot_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        try:
            (root / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("symlinks are not available on this platform")
        with pytest.raises(InvalidPathError):
            Workspace(root).resolve("link/secret.txt")

    def test_windows_junction_cannot_escape(self, outward_junction_project: Path) -> None:
        with pytest.raises(InvalidPathError):
            Workspace(outward_junction_project).resolve("linked-outside/secret.py")

    def test_relative_roundtrip(self, workspace: Workspace) -> None:
        absolute = workspace.resolve("src/app/main.py")
        assert workspace.relative_to_root(absolute) == "src/app/main.py"
        assert workspace.relative("src/app/main.py") == "src/app/main.py"

    def test_binary_suffix_detection(self, workspace: Workspace) -> None:
        assert workspace.is_binary("logo.png")
        assert not workspace.is_binary("README.md")
        assert ".png" in DEFAULT_BINARY_SUFFIXES

    def test_missing_entries_are_reported_false(self, workspace: Workspace) -> None:
        assert not workspace.is_file("no/such/file.txt")
        assert not workspace.is_dir("no/such/directory")
        assert not workspace.exists("../outside")


class TestWorkspaceIdentity:
    def test_root_id_is_stable_across_instances_and_spellings(
        self, workspace: Workspace, sample_project: Path
    ) -> None:
        assert workspace.root_id == workspace_root_id(sample_project)
        assert workspace.root_id == Workspace(str(sample_project)).root_id
        assert len(workspace.root_id) == 64

    def test_different_roots_have_different_ids(
        self, workspace: Workspace, sample_project: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "other-root"
        other.mkdir()
        assert Workspace(other).root_id != workspace.root_id
        assert workspace_root_id(other) != workspace_root_id(sample_project)

    def test_identify_qualifies_a_contained_path(self, workspace: Workspace) -> None:
        identity = workspace.identify("./src/app/main.py")
        assert identity == WorkspacePath(workspace.root_id, "src/app/main.py")
        assert identity.to_dict() == {
            "root_id": workspace.root_id,
            "path": "src/app/main.py",
        }

    def test_identify_rejects_a_path_outside_the_root(self, workspace: Workspace) -> None:
        with pytest.raises(InvalidPathError):
            workspace.identify("../outside.py")

    def test_resolve_for_accepts_the_own_root_id(self, workspace: Workspace) -> None:
        identity = workspace.identify("src/app/main.py")
        assert workspace.resolve_for(identity.root_id, identity.relative) == workspace.resolve(
            "src/app/main.py"
        )

    def test_resolve_for_refuses_a_foreign_root_id(
        self, workspace: Workspace, tmp_path: Path
    ) -> None:
        other = tmp_path / "other-root"
        other.mkdir()
        foreign = Workspace(other).root_id
        with pytest.raises(InvalidPathError) as caught:
            workspace.resolve_for(foreign, "src/app/main.py")
        assert caught.value.details["active_root_id"] == workspace.root_id


class TestExplorerListing:
    def test_hides_dotfiles_and_ignored_directories(self, workspace: Workspace) -> None:
        (workspace.root / ".env").write_text("TOKEN=1\n", encoding="utf-8")
        entries = ExplorerService(workspace).list_directory(".")
        names = {entry.name for entry in entries}
        assert ".env" not in names
        assert ".git" not in names
        assert "node_modules" not in names
        assert "src" in names

    def test_include_hidden_reveals_dotfiles(self, workspace: Workspace) -> None:
        (workspace.root / ".env").write_text("TOKEN=1\n", encoding="utf-8")
        entries = ExplorerService(workspace).list_directory(".", include_hidden=True)
        assert ".env" in {entry.name for entry in entries}

    def test_listing_puts_directories_first(self, workspace: Workspace) -> None:
        entries = ExplorerService(workspace).list_directory(".")
        directories = [entry.is_dir for entry in entries]
        assert directories == sorted(directories, reverse=True)

    def test_listing_marks_hidden_entries(self, workspace: Workspace) -> None:
        (workspace.root / ".cache").write_text("x", encoding="utf-8")
        entries = ExplorerService(workspace).list_directory(".", include_hidden=True)
        hidden = {entry.name: entry.hidden for entry in entries}
        assert hidden[".cache"] is True
        assert hidden["README.md"] is False

    def test_ignored_directory_is_never_listed(self, workspace: Workspace) -> None:
        names = {entry.name for entry in ExplorerService(workspace).list_directory(".")}
        assert not (names & set(DEFAULT_IGNORED_DIRECTORIES))


class TestTextSearch:
    def _service(self, workspace: Workspace) -> ExplorerService:
        return ExplorerService(workspace)

    def test_finds_matches_with_line_and_column(self, workspace: Workspace) -> None:
        outcome = self._service(workspace).search_text(SearchQuery(text="DEFAULT_PORT"))
        assert not outcome.truncated
        assert outcome.matches
        match = outcome.matches[0]
        assert match.path == "src/app/settings.py"
        assert match.line == 3
        assert match.column == 1
        assert "DEFAULT_PORT" in match.preview

    def test_case_insensitive_by_default(self, workspace: Workspace) -> None:
        outcome = self._service(workspace).search_text(SearchQuery(text="default_port"))
        assert outcome.matches

    def test_case_sensitive_misses_different_case(self, workspace: Workspace) -> None:
        outcome = self._service(workspace).search_text(
            SearchQuery(text="default_port", case_sensitive=True)
        )
        assert outcome.matches == []

    def test_whole_word(self, workspace: Workspace) -> None:
        service = self._service(workspace)
        # ``ample`` only occurs inside ``Sample``/``sample``.
        assert service.search_text(SearchQuery(text="ample", whole_word=True)).matches == []
        assert service.search_text(SearchQuery(text="ample", whole_word=False)).matches

    def test_regex_search(self, workspace: Workspace) -> None:
        outcome = self._service(workspace).search_text(
            SearchQuery(text=r"DEFAULT_\w+", use_regex=True)
        )
        assert outcome.matches

    def test_invalid_regex_is_reported(self, workspace: Workspace) -> None:
        with pytest.raises(QuantsMindError) as info:
            self._service(workspace).search_text(SearchQuery(text="(", use_regex=True))
        assert "not valid" in info.value.user_message

    def test_include_filter(self, workspace: Workspace) -> None:
        outcome = self._service(workspace).search_text(SearchQuery(text="Sample", include="*.md"))
        assert outcome.matches
        assert {match.path for match in outcome.matches} == {"README.md"}

    def test_exclude_filter(self, workspace: Workspace) -> None:
        outcome = self._service(workspace).search_text(SearchQuery(text="Sample", exclude="*.md"))
        assert outcome.matches
        assert "README.md" not in {match.path for match in outcome.matches}

    def test_results_are_capped_and_marked_truncated(self, workspace: Workspace) -> None:
        (workspace.root / "many.txt").write_text("needle\n" * 20, encoding="utf-8")
        outcome = self._service(workspace).search_text(SearchQuery(text="needle", max_results=3))
        assert len(outcome.matches) == 3
        assert outcome.truncated is True

    def test_cancellation_stops_the_search(self, workspace: Workspace) -> None:
        cancel = threading.Event()
        cancel.set()
        outcome = self._service(workspace).search_text(SearchQuery(text="needle"), cancel=cancel)
        assert outcome.cancelled is True
        assert outcome.matches == []

    def test_ignored_directories_are_not_searched(self, workspace: Workspace) -> None:
        (workspace.root / ".git" / "leak.txt").write_text("needle\n", encoding="utf-8")
        (workspace.root / "node_modules" / "leak.js").write_text("needle\n", encoding="utf-8")
        outcome = self._service(workspace).search_text(SearchQuery(text="needle"))
        assert outcome.matches == []

    def test_binary_suffixes_are_not_searched(self, workspace: Workspace) -> None:
        (workspace.root / "asset.bin").write_bytes(b"needle\n")
        outcome = self._service(workspace).search_text(SearchQuery(text="needle"))
        assert outcome.matches == []

    def test_oversized_files_are_skipped(self, workspace: Workspace) -> None:
        big = workspace.root / "big.txt"
        big.write_text("needle\n" * 100, encoding="utf-8")
        service = ExplorerService(workspace, search_file_limit=10)
        outcome = service.search_text(SearchQuery(text="needle"))
        assert outcome.matches == []

    def test_empty_query_returns_nothing(self, workspace: Workspace) -> None:
        assert self._service(workspace).search_text(SearchQuery(text="")).matches == []


class TestQuickOpen:
    def test_exact_name_scores_highest(self, workspace: Workspace) -> None:
        results = ExplorerService(workspace).quick_open("main.py")
        assert results[0].path == "src/app/main.py"
        assert results[0].score >= 600

    def test_prefix_and_substring_matches(self, workspace: Workspace) -> None:
        paths = [result.path for result in ExplorerService(workspace).quick_open("main")]
        assert "src/app/main.py" in paths
        paths = [result.path for result in ExplorerService(workspace).quick_open("usage")]
        assert "docs/usage.md" in paths

    def test_fuzzy_subsequence_match(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        (root / "quantsmind").mkdir(parents=True)
        (root / "quantsmind" / "workbench_window.py").write_text("x\n", encoding="utf-8")
        results = ExplorerService(Workspace(root)).quick_open("qmw")
        assert results
        assert results[0].path == "quantsmind/workbench_window.py"

    def test_limit_is_respected(self, workspace: Workspace) -> None:
        results = ExplorerService(workspace).quick_open("", limit=2)
        assert len(results) == 2

    def test_recent_paths_are_ranked_first(self, workspace: Workspace) -> None:
        results = ExplorerService(workspace).quick_open("", recent=["docs/usage.md"])
        assert results[0].path == "docs/usage.md"

    def test_cancelled_quick_open_returns_nothing(self, workspace: Workspace) -> None:
        cancel = threading.Event()
        cancel.set()
        assert ExplorerService(workspace).quick_open("", cancel=cancel) == []

    def test_no_match_returns_empty(self, workspace: Workspace) -> None:
        assert ExplorerService(workspace).quick_open("zzzz-no-such-file") == []


class TestFileOperations:
    def _operations(self, workspace: Workspace) -> FileOperationService:
        return FileOperationService(workspace)

    def test_create_file_and_directory(self, workspace: Workspace) -> None:
        operations = self._operations(workspace)
        assert operations.create_file("docs/new.md", "hello\n") == "docs/new.md"
        assert (workspace.root / "docs" / "new.md").read_text(encoding="utf-8") == "hello\n"
        assert operations.create_directory("docs/notes") == "docs/notes"
        assert (workspace.root / "docs" / "notes").is_dir()

    def test_create_never_overwrites(self, workspace: Workspace) -> None:
        operations = self._operations(workspace)
        with pytest.raises(FileOperationError):
            operations.create_file("README.md", "replaced")
        assert "Sample Project" in (workspace.root / "README.md").read_text(encoding="utf-8")

    def test_create_requires_an_existing_parent(self, workspace: Workspace) -> None:
        with pytest.raises(FileOperationError):
            self._operations(workspace).create_file("missing/new.txt")

    @pytest.mark.parametrize("target", ["../escape.txt", "/tmp/escape.txt", r"C:\escape.txt"])
    def test_create_refuses_paths_outside_the_workspace(
        self, workspace: Workspace, target: str
    ) -> None:
        with pytest.raises(InvalidPathError):
            self._operations(workspace).create_file(target)

    @pytest.mark.parametrize("name", ["", "   ", ".", "..", "bad\x00name"])
    def test_validate_name_rejects_unsafe_names(self, workspace: Workspace, name: str) -> None:
        with pytest.raises(FileOperationError):
            self._operations(workspace).validate_name(name)

    def test_validate_name_rejects_control_characters(self, workspace: Workspace) -> None:
        with pytest.raises(FileOperationError):
            self._operations(workspace).validate_name("bad\nname")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific name rules")
    @pytest.mark.parametrize("name", ["CON", "con.txt", "LPT1.log", "name."])
    def test_validate_name_rejects_windows_reserved_names(
        self, workspace: Workspace, name: str
    ) -> None:
        with pytest.raises(FileOperationError):
            self._operations(workspace).validate_name(name)

    def test_validate_name_accepts_ordinary_names(self, workspace: Workspace) -> None:
        assert self._operations(workspace).validate_name(" notes.txt ") == "notes.txt"

    def test_rename(self, workspace: Workspace) -> None:
        operations = self._operations(workspace)
        assert operations.rename("docs/usage.md", "docs/guide.md") == "docs/guide.md"
        assert not (workspace.root / "docs" / "usage.md").exists()
        assert (workspace.root / "docs" / "guide.md").is_file()

    def test_rename_refuses_existing_target(self, workspace: Workspace) -> None:
        with pytest.raises(FileOperationError):
            self._operations(workspace).rename("README.md", "pyproject.toml")

    def test_rename_refuses_the_root(self, workspace: Workspace) -> None:
        with pytest.raises(FileOperationError):
            self._operations(workspace).rename(".", "renamed")

    def test_rename_refuses_to_leave_the_workspace(self, workspace: Workspace) -> None:
        with pytest.raises(InvalidPathError):
            self._operations(workspace).rename("README.md", "../README.md")

    def test_permanent_delete(self, workspace: Workspace) -> None:
        operations = self._operations(workspace)
        result = operations.delete("docs/usage.md", permanent=True)
        assert result.method == "permanent"
        assert result.recovered is False
        assert not (workspace.root / "docs" / "usage.md").exists()

    def test_delete_missing_is_an_error(self, workspace: Workspace) -> None:
        with pytest.raises(FileOperationError):
            self._operations(workspace).delete("docs/missing.md", permanent=True)

    def test_delete_refuses_the_root(self, workspace: Workspace) -> None:
        with pytest.raises(FileOperationError):
            self._operations(workspace).delete(".", permanent=True)

    def test_delete_refuses_to_leave_the_workspace(self, workspace: Workspace) -> None:
        with pytest.raises(InvalidPathError):
            self._operations(workspace).delete("../outside", permanent=True)

    def test_unavailable_trash_is_not_silently_permanent(
        self, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(self: FileOperationService, target: Path) -> str:
            raise TrashUnavailable(
                "trash unavailable",
                user_message="The Recycle Bin is unavailable.",
            )

        monkeypatch.setattr(FileOperationService, "_move_to_trash", refuse)
        target = workspace.root / "docs" / "usage.md"
        with pytest.raises(TrashUnavailable):
            self._operations(workspace).delete("docs/usage.md")
        assert target.is_file()


class TestWorkspaceWatcher:
    def test_poll_reports_changes_once(self, workspace: Workspace) -> None:
        reports: list[list[str]] = []
        watcher = WorkspaceWatcher(workspace, reports.append, interval=60.0, settle=0.0)
        watcher.set_directories(["."])
        # Create the file in the watched directory itself: detection must not
        # depend on the OS propagating a child directory's mtime right away.
        target = workspace.root / "new.txt"
        target.write_text("new\n", encoding="utf-8")
        assert watcher.poll_once() == ["."]
        assert watcher.poll_once() == []

    def test_only_registered_directories_are_polled(self, workspace: Workspace) -> None:
        watcher = WorkspaceWatcher(workspace, lambda _changed: None, interval=60.0)
        watcher.set_directories(["docs"])
        (workspace.root / "src" / "app" / "added.py").write_text("x = 1\n", encoding="utf-8")
        assert watcher.poll_once() == []
        (workspace.root / "docs" / "added.md").write_text("# added\n", encoding="utf-8")
        assert watcher.poll_once() == ["docs"]

    def test_invalid_directories_are_dropped(self, workspace: Workspace) -> None:
        watcher = WorkspaceWatcher(workspace, lambda _changed: None, interval=60.0)
        watcher.set_directories(["../outside", "docs"])
        (workspace.root / "docs" / "added.md").write_text("# added\n", encoding="utf-8")
        assert watcher.poll_once() == ["docs"]

    def test_root_not_watched_when_not_registered(self, workspace: Workspace) -> None:
        """A root-level change is reported only when "." is explicitly registered.

        Regression: set_directories() used to inject "." implicitly, so a write
        at the workspace root produced a spurious "." report even when only a
        subdirectory was registered.
        """

        watcher = WorkspaceWatcher(workspace, lambda _changed: None, interval=60.0)
        watcher.set_directories(["docs"])
        (workspace.root / "root-only.txt").write_text("new\n", encoding="utf-8")
        assert watcher.poll_once() == []

    def test_nested_child_mtime_does_not_report_root(self, workspace: Workspace) -> None:
        """A nested write must never surface the root as a changed directory.

        On POSIX, adding a file under docs/ updates the docs/ directory mtime,
        which is part of the root snapshot. With implicit "." registration that
        produced a spurious "." alongside "docs". Whether the nested change is
        itself detected is OS-dependent, but the root must never be reported
        when "." is not explicitly registered.
        """

        watcher = WorkspaceWatcher(workspace, lambda _changed: None, interval=60.0)
        watcher.set_directories(["docs"])
        nested = workspace.root / "docs" / "nested"
        nested.mkdir()
        first = watcher.poll_once()
        (nested / "deep.md").write_text("# deep\n", encoding="utf-8")
        second = watcher.poll_once()
        assert "." not in first
        assert "." not in second
        # A direct write into the registered directory is always detected.
        (workspace.root / "docs" / "direct.md").write_text("# direct\n", encoding="utf-8")
        assert watcher.poll_once() == ["docs"]

    def test_explicit_root_registration_still_watches_root(self, workspace: Workspace) -> None:
        """Explicit "." registration preserves root watching (GUI relies on it)."""

        watcher = WorkspaceWatcher(workspace, lambda _changed: None, interval=60.0)
        watcher.set_directories([".", "docs"])
        (workspace.root / "root-only.txt").write_text("new\n", encoding="utf-8")
        assert watcher.poll_once() == ["."]

    def test_set_directories_replaces_previous_set(self, workspace: Workspace) -> None:
        """set_directories() replaces the watched set rather than accumulating it."""

        watcher = WorkspaceWatcher(workspace, lambda _changed: None, interval=60.0)
        watcher.set_directories(["docs"])
        watcher.set_directories(["src"])
        # docs is no longer watched after replacement.
        (workspace.root / "docs" / "replaced.md").write_text("# x\n", encoding="utf-8")
        assert watcher.poll_once() == []
        # src is now watched.
        (workspace.root / "src" / "watched.py").write_text("x = 1\n", encoding="utf-8")
        assert watcher.poll_once() == ["src"]

    def test_repeated_writes_are_reported_each_time(self, workspace: Workspace) -> None:
        """Each write/poll cycle reports the watched directory exactly once."""

        watcher = WorkspaceWatcher(workspace, lambda _changed: None, interval=60.0)
        watcher.set_directories(["docs"])
        for name in ("first.md", "second.md", "third.md"):
            (workspace.root / "docs" / name).write_text("# x\n", encoding="utf-8")
            assert watcher.poll_once() == ["docs"]
            # No phantom report when nothing changed between polls.
            assert watcher.poll_once() == []

    def test_start_and_stop(self, workspace: Workspace) -> None:
        watcher = WorkspaceWatcher(workspace, lambda _changed: None, interval=60.0)
        initial = watcher.running
        assert initial is False
        watcher.start()
        try:
            running_after_start = watcher.running
        finally:
            watcher.stop()
        assert running_after_start is True
        assert watcher.running is False
        watcher.stop()  # idempotent

    def test_absolute_resolution(self, workspace: Workspace) -> None:
        watcher = WorkspaceWatcher(workspace, lambda _changed: None)
        assert watcher.absolute("docs") == workspace.resolve("docs")
        with pytest.raises(InvalidPathError):
            watcher.absolute("../outside")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_unreadable_file_is_skipped_by_search(workspace: Workspace, tmp_path: Path) -> None:
    secret = workspace.root / "secret.txt"
    secret.write_text("needle\n", encoding="utf-8")
    secret.chmod(0)
    try:
        outcome = ExplorerService(workspace).search_text(SearchQuery(text="needle"))
        assert "secret.txt" not in {match.path for match in outcome.matches}
    finally:
        secret.chmod(0o600)
