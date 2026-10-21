"""Unit tests for the workspace abstraction and path validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.errors import InvalidPathError, InvalidWorkspaceError
from quantsmind_agent.workspace import DEFAULT_IGNORED_DIRECTORIES, Workspace, is_within


class TestConstruction:
    def test_accepts_existing_directory(self, sample_project: Path) -> None:
        space = Workspace(sample_project)
        assert space.root == sample_project.resolve()

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidWorkspaceError) as info:
            Workspace(tmp_path / "nope")
        assert "could not be opened" in info.value.user_message

    def test_file_is_not_a_workspace(self, sample_project: Path) -> None:
        with pytest.raises(InvalidWorkspaceError) as info:
            Workspace(sample_project / "README.md")
        assert "is a file" in info.value.user_message

    def test_repr_and_fspath(self, sample_project: Path) -> None:
        space = Workspace(sample_project)
        assert "Workspace(root=" in repr(space)
        assert Path(str(space)) == space.root


class TestPathValidation:
    @pytest.mark.parametrize(
        "candidate",
        [
            "../secrets.txt",
            "..",
            "src/../../outside.txt",
            "src/..",
            "./../etc/passwd",
        ],
    )
    def test_rejects_parent_traversal(self, workspace: Workspace, candidate: str) -> None:
        with pytest.raises(InvalidPathError):
            workspace.resolve(candidate)

    @pytest.mark.parametrize(
        "candidate",
        ["/etc/passwd", "C:/Windows/System32/config", r"C:\Windows\win.ini", r"\\server\share"],
    )
    def test_rejects_absolute_paths(self, workspace: Workspace, candidate: str) -> None:
        with pytest.raises(InvalidPathError):
            workspace.resolve(candidate)

    @pytest.mark.parametrize("candidate", ["~/notes.md", "~", ""])
    def test_rejects_home_and_empty(self, workspace: Workspace, candidate: str) -> None:
        with pytest.raises(InvalidPathError):
            workspace.resolve(candidate)

    def test_rejects_null_byte(self, workspace: Workspace) -> None:
        with pytest.raises(InvalidPathError):
            workspace.resolve("src/app\x00.py")

    def test_accepts_nested_relative_path(self, workspace: Workspace) -> None:
        resolved = workspace.resolve("src/app/main.py")
        assert resolved == workspace.root / "src" / "app" / "main.py"

    def test_accepts_windows_separators(self, workspace: Workspace) -> None:
        assert workspace.resolve(r"src\app\main.py") == workspace.resolve("src/app/main.py")

    def test_accepts_quoted_path(self, workspace: Workspace) -> None:
        assert workspace.resolve('"README.md"') == workspace.root / "README.md"

    def test_relative_roundtrip(self, workspace: Workspace) -> None:
        assert workspace.relative("src/app/main.py") == "src/app/main.py"
        assert workspace.relative(".") == "."

    def test_symlink_escape_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        try:
            (root / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("symlinks are not available on this platform")
        space = Workspace(root)
        with pytest.raises(InvalidPathError):
            space.resolve("link/secret.txt")

    def test_denial_message_is_actionable(self, workspace: Workspace) -> None:
        with pytest.raises(InvalidPathError) as info:
            workspace.resolve("../../secret")
        assert "outside the current workspace" in info.value.user_message

    def test_is_within(self, tmp_path: Path) -> None:
        assert is_within(tmp_path, tmp_path / "a" / "b")
        assert is_within(tmp_path, tmp_path)
        assert not is_within(tmp_path / "a", tmp_path / "b")


class TestInspection:
    def test_list_dir_sorts_directories_first(self, workspace: Workspace) -> None:
        names = [entry.name for entry in workspace.list_dir(".")]
        assert names.index("src") < names.index("README.md")

    def test_ignored_directories_are_hidden(self, workspace: Workspace) -> None:
        names = {entry.name for entry in workspace.list_dir(".")}
        assert "node_modules" in DEFAULT_IGNORED_DIRECTORIES
        assert "node_modules" not in names
        assert ".git" not in names

    def test_entries_expose_metadata(self, workspace: Workspace) -> None:
        entry = next(e for e in workspace.list_dir(".") if e.name == "README.md")
        assert entry.kind == "file"
        assert entry.size > 0
        assert entry.modified

    def test_walk_skips_binary_and_ignored(self, workspace: Workspace) -> None:
        paths = {entry.path for entry in workspace.walk(".")}
        assert "src/app/main.py" in paths
        assert "logo.png" not in paths
        assert not any(path.startswith("node_modules") for path in paths)
        assert not any(path.startswith(".git") for path in paths)

    def test_walk_respects_max_entries(self, workspace: Workspace) -> None:
        entries = list(workspace.walk(".", max_entries=2))
        assert len(entries) <= 2

    def test_list_dir_rejects_file(self, workspace: Workspace) -> None:
        with pytest.raises(InvalidPathError):
            workspace.list_dir("README.md")

    def test_count_children(self, workspace: Workspace) -> None:
        assert workspace.count_children(".") >= 4
        assert workspace.count_children("missing") == 0


class TestJunctions:
    def test_outward_junction_is_not_listed_or_walked(self, outward_junction_project: Path) -> None:
        space = Workspace(outward_junction_project)

        names = {entry.name for entry in space.list_dir(".")}
        paths = {entry.path for entry in space.walk(".")}

        assert "linked-outside" not in names
        assert "src" in names
        assert "src/safe.py" in paths
        assert not any("linked-outside" in path or "secret" in path for path in paths)

    def test_outward_junction_read_is_still_refused(self, outward_junction_project: Path) -> None:
        space = Workspace(outward_junction_project)

        with pytest.raises(InvalidPathError):
            space.read_text("linked-outside/secret.py")


class TestReading:
    def test_read_text(self, workspace: Workspace) -> None:
        content = workspace.read_text("README.md")
        assert "Sample Project" in content

    def test_read_text_missing_file(self, workspace: Workspace) -> None:
        with pytest.raises(InvalidPathError) as info:
            workspace.read_text("does/not/exist.py")
        assert "does not exist" in info.value.user_message

    def test_refuses_binary(self, workspace: Workspace) -> None:
        with pytest.raises(InvalidPathError) as info:
            workspace.read_text("logo.png")
        assert "binary" in info.value.user_message

    def test_read_text_honours_limit(self, workspace: Workspace) -> None:
        assert len(workspace.read_text("README.md", max_bytes=5)) == 5

    def test_read_text_outside_root_is_impossible(
        self, workspace: Workspace, escape_target: Path
    ) -> None:
        with pytest.raises(InvalidPathError):
            workspace.read_text("../outside_secret.txt")
        assert escape_target.exists()

    def test_file_size(self, workspace: Workspace) -> None:
        assert workspace.file_size("README.md") > 0

    def test_predicates_are_false_for_escapes(self, workspace: Workspace) -> None:
        assert workspace.exists("../nope") is False
        assert workspace.is_dir("../nope") is False
        assert workspace.is_file("../nope") is False


class TestIgnoresAreConfigurable:
    def test_custom_ignore_list(self, sample_project: Path) -> None:
        space = Workspace(sample_project, ignored_directories=["docs"])
        names = {entry.name for entry in space.list_dir(".")}
        assert "docs" not in names
        assert "src" in names
