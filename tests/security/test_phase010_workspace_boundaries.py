"""Phase 0.10: the workspace boundaries, tested as security invariants.

The IDE adds new ways to reach a project -- explorer operations, search, quick
open, the editor and the assistant context bar. None of them may widen what the
agent or a user action can touch, and none may transmit project content without
an explicit human request. Each test here drives the real service or
:class:`ApplicationContext` and asserts on what actually happened, not on what a
message claims.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes import FakeModelProvider

import quantsmind_agent.security.workspace_trust as workspace_trust_module
from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.app.settings import ConfigManager
from quantsmind_agent.errors import InvalidPathError, QuantsMindError
from quantsmind_agent.persistence.editor_state import (
    EditorStateStore,
    EditorTabState,
    LayoutState,
)
from quantsmind_agent.persistence.paths import AppPaths
from quantsmind_agent.security.audit import AuditEventType
from quantsmind_agent.security.workspace_trust import (
    WorkspaceTrustState,
    WorkspaceTrustStore,
    root_key,
)
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.services.workspace.explorer import ExplorerService, SearchQuery
from quantsmind_agent.services.workspace.operations import FileOperationService

pytestmark = pytest.mark.security


@pytest.fixture
def local_context(
    app_paths: AppPaths,
    config_manager: ConfigManager,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ApplicationContext, FakeModelProvider]:
    """A fully wired context whose provider is scripted and never networked."""

    provider = FakeModelProvider()

    def build_fake(self: ApplicationContext, config: object) -> FakeModelProvider:
        return provider

    monkeypatch.setattr(ApplicationContext, "_build_model_provider", build_fake)
    context = ApplicationContext.create(paths=app_paths, config_manager=config_manager)
    return context, provider


class TestContainmentBoundaries:
    def test_reads_refuse_binary_and_outside_paths(self, workspace: Workspace) -> None:
        with pytest.raises(InvalidPathError):
            workspace.read_text("logo.png")
        with pytest.raises(InvalidPathError):
            workspace.read_text("../outside.txt")
        with pytest.raises(InvalidPathError):
            workspace.read_text(r"C:\Windows\win.ini")
        with pytest.raises(InvalidPathError):
            workspace.read_text(r"\\server\share\secret.txt")

    def test_junction_is_excluded_from_listing_and_walk(
        self, outward_junction_project: Path
    ) -> None:
        space = Workspace(outward_junction_project)
        assert "linked-outside" not in {entry.name for entry in space.list_dir(".")}

        excluded: list[str] = []
        paths = [entry.path for entry in space.walk(".", excluded=excluded)]
        assert "src/safe.py" in paths
        assert "linked-outside" in excluded
        assert not [path for path in paths if path.startswith("linked-outside")]

    def test_junction_cannot_be_read_or_resolved(self, outward_junction_project: Path) -> None:
        space = Workspace(outward_junction_project)
        with pytest.raises(InvalidPathError):
            space.resolve("linked-outside/secret.py")
        with pytest.raises(InvalidPathError):
            space.read_text("linked-outside/secret.py")

    def test_walk_bounds_the_number_of_entries(self, workspace: Workspace) -> None:
        entries = list(workspace.walk(".", max_entries=2))
        assert len(entries) == 2

    def test_search_and_quick_open_stay_inside_the_root(self, workspace: Workspace) -> None:
        explorer = ExplorerService(workspace)
        outcome = explorer.search_text(SearchQuery(text="SECRET"))
        assert outcome.matches == []
        assert explorer.quick_open("..") == []


class TestFileOperationBoundaries:
    def test_operations_through_a_junction_are_refused(
        self, outward_junction_project: Path
    ) -> None:
        operations = FileOperationService(Workspace(outward_junction_project))
        outside = outward_junction_project.parent / "outside_project"

        with pytest.raises(InvalidPathError):
            operations.create_file("linked-outside/new.py", "created outside\n")
        assert not (outside / "new.py").exists()

        with pytest.raises(InvalidPathError):
            operations.rename("src/safe.py", "linked-outside/moved.py")
        assert (outward_junction_project / "src" / "safe.py").is_file()
        assert not (outside / "moved.py").exists()

        with pytest.raises(InvalidPathError):
            operations.delete("linked-outside/secret.py", permanent=True)
        assert (outside / "secret.py").is_file()

    def test_symlink_escape_is_refused_when_the_platform_supports_it(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
        try:
            (root / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available on this platform")

        operations = FileOperationService(Workspace(root))
        with pytest.raises(InvalidPathError):
            operations.create_file("link/new.py", "created outside\n")
        with pytest.raises(InvalidPathError):
            operations.delete("link/secret.py", permanent=True)
        assert not (outside / "new.py").exists()
        assert (outside / "secret.py").is_file()


class TestRestrictedMode:
    def test_restricted_workspace_has_no_agent_access(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, provider = local_context
        workspace = context.open_workspace(sample_project, trusted=False)

        assert context.workspace_restricted
        assert not context.workspace_trusted
        assert context.project_intel is None
        assert context.workspace_snapshot() is None
        assert context.tools.names == []
        assert "read_file" not in context.tools

        prompt = context.context_engine.system_prompt()
        assert "No project is currently open." in prompt
        assert str(workspace.root) not in prompt
        assert "README.md" not in prompt
        assert provider.requests == []
        assert provider.stream_requests == []

    def test_trusting_again_restores_the_tools_and_the_index(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        workspace = context.open_workspace(sample_project, trusted=False)
        context.set_workspace_trust(True)

        assert context.workspace_trusted
        assert "read_file" in context.tools
        assert "write_file" in context.tools
        assert context.project_intel is not None
        assert context.workspace_snapshot() is not None
        assert str(workspace.root) in context.context_engine.system_prompt()

        decisions = [
            event.decision
            for event in context.security_audit.events_of_type(
                AuditEventType.WORKSPACE_TRUST_CHANGED
            )
        ]
        assert decisions == ["restricted", "trusted"]

    def test_trust_changes_are_audited_without_project_content(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        workspace = context.open_workspace(sample_project, trusted=False)
        context.set_workspace_trust(True)

        events = context.security_audit.events_of_type(AuditEventType.WORKSPACE_TRUST_CHANGED)
        assert len(events) == 2
        for event in events:
            assert event.reason_code == "user_decision"
            assert event.detail == str(workspace.root)
            record = repr(event.to_dict())
            assert "DEFAULT_PORT" not in record
            assert "Sample Project" not in record

    def test_revocation_deletes_the_stored_decision(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        context.open_workspace(sample_project, trusted=True)
        assert context.workspace_trusted
        assert context.trust_store.state_for(sample_project) is WorkspaceTrustState.TRUSTED

        context.revoke_workspace_trust()

        assert context.workspace_restricted
        assert context.tools.names == []
        assert context.project_intel is None
        # The UI promises the stored decision is deleted, so the next open is a
        # fresh human question rather than an inherited restriction.
        assert context.trust_store.state_for(sample_project) is WorkspaceTrustState.UNDECIDED
        assert context.trust_store.record_for(sample_project) is None

    def test_opening_a_stored_restriction_is_honoured_without_a_prompt(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        context.open_workspace(sample_project, trusted=False)
        context.close_workspace()

        reopened = context.open_workspace(sample_project)
        assert reopened.root == sample_project.resolve()
        assert context.workspace_restricted
        assert context.tools.names == []


class TestNoUnapprovedTransmission:
    def test_workspace_inspection_never_contacts_the_provider(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, provider = local_context
        workspace = context.open_workspace(sample_project, trusted=True)

        workspace.read_text("src/app/settings.py")
        explorer = ExplorerService(workspace)
        explorer.list_directory("src")
        explorer.search_text(SearchQuery(text="DEFAULT_PORT"))
        explorer.quick_open("main")
        assert context.workspace_snapshot(refresh=True) is not None

        assert provider.requests == []
        assert provider.stream_requests == []

    def test_restricted_workspace_can_still_be_browsed_locally(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, provider = local_context
        workspace = context.open_workspace(sample_project, trusted=False)

        assert "DEFAULT_PORT" in workspace.read_text("src/app/settings.py")
        outcome = ExplorerService(workspace).search_text(SearchQuery(text="DEFAULT_PORT"))
        assert outcome.matches

        assert context.tools.names == []
        assert provider.requests == []
        assert provider.stream_requests == []


class TestTrustRevocation:
    def test_revocation_deletes_a_trusted_decision(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        context.open_workspace(sample_project, trusted=True)
        assert context.trust_store.state_for(sample_project) is WorkspaceTrustState.TRUSTED

        context.revoke_workspace_trust()

        assert context.workspace_restricted
        assert context.trust_store.state_for(sample_project) is WorkspaceTrustState.UNDECIDED
        assert context.trust_store.record_for(sample_project) is None

    def test_revocation_stores_no_restricted_replacement(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        context.open_workspace(sample_project, trusted=True)
        path = context.trust_store.path

        context.revoke_workspace_trust()

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert root_key(sample_project) not in payload["roots"]
        assert all(record["state"] != "restricted" for record in payload["roots"].values())

    def test_revoked_root_reads_as_the_safe_default(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        context.open_workspace(sample_project, trusted=True)
        context.revoke_workspace_trust()

        reloaded = WorkspaceTrustStore(context.trust_store.path)
        assert reloaded.state_for(sample_project) is WorkspaceTrustState.UNDECIDED

    def test_revocation_survives_a_reload(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        context.open_workspace(sample_project, trusted=True)
        path = context.trust_store.path
        context.revoke_workspace_trust()

        reloaded = WorkspaceTrustStore(path)
        assert reloaded.record_for(sample_project) is None
        assert reloaded.records() == []

    def test_repeated_revocation_is_safe(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        context.open_workspace(sample_project, trusted=True)
        context.revoke_workspace_trust()

        context.revoke_workspace_trust()

        assert context.workspace_restricted
        assert context.trust_store.revoke(sample_project) is False
        assert context.trust_store.record_for(sample_project) is None

    def test_revocation_without_a_stored_decision_is_safe(
        self, local_context: tuple[ApplicationContext, FakeModelProvider], sample_project: Path
    ) -> None:
        context, _ = local_context
        context.open_workspace(sample_project)
        assert context.trust_store.record_for(sample_project) is None

        context.revoke_workspace_trust()

        assert context.workspace_restricted
        assert context.trust_store.state_for(sample_project) is WorkspaceTrustState.UNDECIDED

    def test_failed_persistence_is_not_reported_as_revoked(
        self,
        local_context: tuple[ApplicationContext, FakeModelProvider],
        sample_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context, _ = local_context
        context.open_workspace(sample_project, trusted=True)
        path = context.trust_store.path
        assert "read_file" in context.tools

        def fail_write(target: Path, payload: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(workspace_trust_module, "atomic_write_json", fail_write)
        with pytest.raises(QuantsMindError) as info:
            context.revoke_workspace_trust()

        assert "could not be removed" in info.value.user_message.lower()
        assert context.workspace_trusted
        assert "read_file" in context.tools
        assert context.trust_store.state_for(sample_project) is WorkspaceTrustState.TRUSTED
        assert WorkspaceTrustStore(path).state_for(sample_project) is WorkspaceTrustState.TRUSTED

    def test_store_revoke_restores_the_record_when_the_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "security" / "workspace-trust.json"
        store = WorkspaceTrustStore(path)
        store.decide(tmp_path, trusted=True)

        def fail_write(target: Path, payload: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(workspace_trust_module, "atomic_write_json", fail_write)
        with pytest.raises(QuantsMindError):
            store.revoke(tmp_path)

        assert store.state_for(tmp_path) is WorkspaceTrustState.TRUSTED
        assert WorkspaceTrustStore(path).state_for(tmp_path) is WorkspaceTrustState.TRUSTED


class TestPersistenceBoundaries:
    def test_editor_state_records_navigation_only(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        path = tmp_path / "sessions" / "editor-state.json"
        store = EditorStateStore(path)
        store.save_layout(LayoutState(sidebar_width=320, activity="search"))
        store.save_tabs(
            sample_project,
            [EditorTabState(path="src/app/settings.py", cursor_line=3, active=True)],
        )

        stored = path.read_text(encoding="utf-8")
        assert "DEFAULT_PORT" not in stored
        assert "return {" not in stored
        assert "API_KEY" not in stored

        payload = json.loads(stored)
        entry = payload["workspaces"][root_key(sample_project)]
        assert entry["tabs"] == [
            {
                "path": "src/app/settings.py",
                "cursor_line": 3,
                "cursor_column": 1,
                "scroll": 0,
                "pinned": True,
                "active": True,
            }
        ]
        assert entry["root"] == str(sample_project.resolve())

    def test_tampered_tab_paths_are_dropped_before_restore(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        path = tmp_path / "editor-state.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "layout": {},
                    "workspaces": {
                        root_key(sample_project): {
                            "root": str(sample_project),
                            "tabs": [
                                {"path": "../../outside.py"},
                                {"path": "C:/Windows/win.ini"},
                                {"path": r"\\server\share\secret.txt"},
                                {"path": "src/app/settings.py"},
                                {"path": "missing.py"},
                            ],
                            "updated_at": "",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        restorable, missing = EditorStateStore(path).restore_tabs(
            sample_project, lambda relative: (sample_project / relative).is_file()
        )
        assert [tab.path for tab in restorable] == ["src/app/settings.py"]
        assert missing == ["missing.py"]

    def test_trust_file_holds_decisions_not_content(self, tmp_path: Path) -> None:
        from quantsmind_agent.security.workspace_trust import WorkspaceTrustStore

        path = tmp_path / "trust" / "workspace-trust.json"
        WorkspaceTrustStore(path).decide(tmp_path, trusted=False)

        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        records = list(payload["roots"].values())
        assert len(records) == 1
        assert set(records[0]) == {"path", "state", "decided_at"}
        assert records[0]["state"] == "restricted"
        assert records[0]["path"] == str(tmp_path.resolve())

    def test_layout_and_trust_are_separate_files(self, app_paths: AppPaths) -> None:
        assert app_paths.trust_file != app_paths.editor_state_file
        assert app_paths.trust_file.parent != app_paths.editor_state_file.parent
