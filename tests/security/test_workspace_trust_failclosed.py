"""Security invariants: trust that was never granted must not be inherited.

Workspace trust decides whether the agent may read a project, build its project
index and receive workspace content in a prompt. An undecided, missing, corrupt
or future-schema trust record must never be promoted to trusted implicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import quantsmind_agent.app.cli as cli_module
import quantsmind_agent.security.workspace_trust as workspace_trust_module
from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.app.settings import ConfigManager
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.persistence.paths import AppPaths
from quantsmind_agent.security.workspace_trust import (
    WorkspaceTrustState,
    WorkspaceTrustStore,
)

pytestmark = pytest.mark.security


def _context(app_paths: AppPaths, config_manager: ConfigManager) -> ApplicationContext:
    return ApplicationContext.create(paths=app_paths, config_manager=config_manager)


def test_a_new_workspace_without_a_decision_opens_restricted(
    app_paths: AppPaths, config_manager: ConfigManager, sample_project: Path
) -> None:
    context = _context(app_paths, config_manager)

    context.open_workspace(sample_project)

    assert not context.workspace_trusted
    assert context.workspace_restricted
    assert context.tools.names == []
    assert context.project_intel is None


def test_an_explicit_trust_decision_opens_tools(
    app_paths: AppPaths, config_manager: ConfigManager, sample_project: Path
) -> None:
    context = _context(app_paths, config_manager)

    context.open_workspace(sample_project, trusted=True)

    assert context.workspace_trusted
    assert "read_file" in context.tools.names
    assert context.project_intel is not None


def test_an_explicit_restricted_decision_opens_no_tools(
    app_paths: AppPaths, config_manager: ConfigManager, sample_project: Path
) -> None:
    context = _context(app_paths, config_manager)

    context.open_workspace(sample_project, trusted=False)

    assert context.workspace_restricted
    assert context.tools.names == []


def test_a_persisted_trust_decision_survives_a_restore(
    app_paths: AppPaths, config_manager: ConfigManager, sample_project: Path
) -> None:
    first = _context(app_paths, config_manager)
    first.open_workspace(sample_project, trusted=True)

    restored = _context(app_paths, config_manager)

    assert restored.workspace_trusted
    assert "read_file" in restored.tools.names


def test_restore_without_a_trust_record_is_restricted(
    app_paths: AppPaths, config_manager: ConfigManager, sample_project: Path
) -> None:
    first = _context(app_paths, config_manager)
    first.open_workspace(sample_project, trusted=True)
    app_paths.trust_file.unlink()

    restored = _context(app_paths, config_manager)

    assert restored.workspace_restricted
    assert restored.tools.names == []


def test_a_corrupt_trust_store_fails_closed(
    app_paths: AppPaths, config_manager: ConfigManager, sample_project: Path
) -> None:
    first = _context(app_paths, config_manager)
    first.open_workspace(sample_project, trusted=True)
    app_paths.trust_file.parent.mkdir(parents=True, exist_ok=True)
    app_paths.trust_file.write_bytes(b"\x00 not json \xff")

    restored = _context(app_paths, config_manager)

    assert restored.workspace_restricted
    assert restored.tools.names == []
    assert (
        WorkspaceTrustStore(app_paths.trust_file).state_for(sample_project)
        is WorkspaceTrustState.UNDECIDED
    )


def test_a_future_schema_trust_store_fails_closed(
    app_paths: AppPaths, config_manager: ConfigManager, sample_project: Path
) -> None:
    first = _context(app_paths, config_manager)
    first.open_workspace(sample_project, trusted=True)
    app_paths.trust_file.parent.mkdir(parents=True, exist_ok=True)
    app_paths.trust_file.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "roots": {
                    "future": {
                        "path": str(sample_project.resolve()),
                        "state": "trusted",
                        "decided_at": "2999-01-01T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    restored = _context(app_paths, config_manager)

    assert restored.workspace_restricted
    assert restored.tools.names == []


def test_an_undecided_cli_workspace_is_not_trusted(
    tmp_path: Path, sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool | None] = []
    real_open = ApplicationContext.open_workspace

    def spy(self: ApplicationContext, path: str, *, trusted: bool | None = None) -> object:
        calls.append(trusted)
        return real_open(self, path, trusted=trusted)

    monkeypatch.setattr(ApplicationContext, "open_workspace", spy)
    base = tmp_path / "cli-profile"

    rc = cli_module.main(
        [
            "--config-dir",
            str(base),
            "--workspace",
            str(sample_project),
            "--no-resume",
            "--check-config",
        ]
    )

    assert rc == 1
    assert calls == [None]


def test_an_explicit_cli_trust_flag_persists_the_decision(
    tmp_path: Path, sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool | None] = []
    real_open = ApplicationContext.open_workspace

    def spy(self: ApplicationContext, path: str, *, trusted: bool | None = None) -> object:
        calls.append(trusted)
        return real_open(self, path, trusted=trusted)

    monkeypatch.setattr(ApplicationContext, "open_workspace", spy)
    base = tmp_path / "cli-profile"

    rc = cli_module.main(
        [
            "--config-dir",
            str(base),
            "--workspace",
            str(sample_project),
            "--trust-workspace",
            "--no-resume",
            "--check-config",
        ]
    )

    assert rc == 1
    assert calls == [True]
    trust_files = list(base.rglob("workspace-trust.json"))
    assert trust_files, "--trust-workspace must persist a trust decision"
    assert (
        WorkspaceTrustStore(trust_files[0]).state_for(sample_project) is WorkspaceTrustState.TRUSTED
    )


def test_a_failed_trust_write_is_not_treated_as_trusted(
    app_paths: AppPaths,
    config_manager: ConfigManager,
    sample_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(target: Path, payload: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(workspace_trust_module, "atomic_write_json", fail_write)
    context = _context(app_paths, config_manager)

    with pytest.raises(QuantsMindError):
        context.open_workspace(sample_project, trusted=True)

    assert context.workspace is None
    assert not context.workspace_trusted
    assert (
        WorkspaceTrustStore(app_paths.trust_file).state_for(sample_project)
        is WorkspaceTrustState.UNDECIDED
    )


def test_a_future_schema_refuses_an_explicit_trust_decision(
    app_paths: AppPaths, config_manager: ConfigManager, sample_project: Path
) -> None:
    app_paths.trust_file.parent.mkdir(parents=True, exist_ok=True)
    app_paths.trust_file.write_text('{"schema_version": 999, "roots": {}}', encoding="utf-8")
    context = _context(app_paths, config_manager)

    with pytest.raises(QuantsMindError):
        context.open_workspace(sample_project, trusted=True)

    assert context.workspace is None
    assert not context.workspace_trusted
    assert (
        WorkspaceTrustStore(app_paths.trust_file).state_for(sample_project)
        is WorkspaceTrustState.UNDECIDED
    )


def test_revocation_returns_the_root_to_undecided(
    app_paths: AppPaths, config_manager: ConfigManager, sample_project: Path
) -> None:
    context = _context(app_paths, config_manager)
    context.open_workspace(sample_project, trusted=True)

    context.revoke_workspace_trust()

    assert context.workspace_restricted
    assert context.tools.names == []
    assert (
        WorkspaceTrustStore(app_paths.trust_file).state_for(sample_project)
        is WorkspaceTrustState.UNDECIDED
    )
