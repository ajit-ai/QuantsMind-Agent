"""WP3 wiring: the events the audit store claims to hold are the events produced.

A sink that works in isolation proves nothing about the application. These tests
drive the real paths -- an approval, a write, a command, a permission denial, a
trust change -- against a real store in a real data directory, and then read the
disk back. They also prove the two properties that make the store a control
rather than a log: an approval that cannot be written is not granted, and no
credential-bearing text reaches the file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.app.context import ApplicationContext
from quantsmind_agent.persistence.paths import AppPaths
from quantsmind_agent.runtime.tools import ToolRegistry
from quantsmind_agent.runtime.tools.builtin.command import build_command_tools
from quantsmind_agent.security.audit import (
    AuditEventType,
    AuditPersistenceError,
    SecurityAuditLog,
)
from quantsmind_agent.security.audit_sink import AUDIT_STORE_NAME, RotatingAuditSink
from quantsmind_agent.security.permissions import (
    ApprovalOutcome,
    Decision,
    Permission,
    PermissionRequest,
)
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.write_tools import build_mutating_tools

CANARY = "sk-canary-wiring-0000000000"
PYTHON = sys.executable


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
    ).ensure()


@pytest.fixture
def context(paths: AppPaths) -> ApplicationContext:
    return ApplicationContext.create(paths=paths)


def _stored(sink: RotatingAuditSink) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for segment in sink.segments():
        for line in segment.read_text(encoding="utf-8").splitlines():
            if line.strip():
                payloads.append(json.loads(line))
    return payloads


def _events_of(sink: RotatingAuditSink, name: str) -> list[dict[str, Any]]:
    return [record for record in _stored(sink) if record.get("event") == name]


class TestStoreIsWired:
    def test_the_context_creates_one_store_and_shares_it_with_the_log(
        self, context: ApplicationContext, paths: AppPaths
    ) -> None:
        assert context.security_audit.sink is context.audit_sink
        assert context.audit_sink.path == paths.audit_log_file
        assert context.audit_sink.anchor_path == paths.audit_anchor_file

    def test_the_witness_sits_outside_the_rotating_set(
        self, context: ApplicationContext, paths: AppPaths
    ) -> None:
        assert paths.audit_anchor_file.parent == paths.data_dir
        assert paths.audit_log_file.parent == paths.data_dir / "audit"
        assert context.audit_sink.anchor_path.parent != context.audit_sink.path.parent

    def test_the_ceiling_is_the_accepted_one(self, context: ApplicationContext) -> None:
        from quantsmind_agent.security.audit_sink import DEFAULT_FILES, DEFAULT_MAX_BYTES

        sink = context.audit_sink
        assert sink.ceiling_bytes == DEFAULT_MAX_BYTES * DEFAULT_FILES == 15_728_640


class TestEventsReachDisk:
    def test_an_approval_decision_is_written_before_the_ui_sees_it(
        self, context: ApplicationContext
    ) -> None:
        request = PermissionRequest(
            permission=Permission.WRITE_WORKSPACE,
            tool="write_file",
            summary="write a.txt",
            target="a.txt",
        )

        context._on_approval(request, ApprovalOutcome.ALLOW_ONCE)

        records = _events_of(context.audit_sink, "approval.decision")
        assert [record["decision"] for record in records] == [ApprovalOutcome.ALLOW_ONCE.value]
        assert records[0]["subject"] == "a.txt"
        assert "write_file" in records[0]["detail"]

    def test_a_denied_permission_is_written(self, context: ApplicationContext) -> None:
        request = PermissionRequest(
            permission=Permission.NETWORK,
            tool="http_get",
            summary="fetch a URL",
            target="https://example.invalid",
        )

        context._on_permission_decision(request, Decision.DENY)

        records = _events_of(context.audit_sink, "security.policy_denied")
        assert records[0]["decision"] == Decision.DENY.value
        assert records[0]["subject"] == "https://example.invalid"

    def test_a_workspace_write_is_written(
        self, context: ApplicationContext, workspace: Workspace
    ) -> None:
        registry = ToolRegistry(permissions=_allowing_broker())
        registry.register_all(build_mutating_tools(workspace, audit=context.security_audit))

        result = registry.execute("write_file", {"path": "audited.txt", "content": "hello\n"})

        assert result.ok
        records = _events_of(context.audit_sink, "file.mutated")
        assert records[0]["subject"] == "audited.txt"
        assert records[0]["outcome"] in {"created", "replaced"}
        assert "sha256" in records[0]["detail"]

    def test_an_undo_is_written_as_a_mutation(
        self, context: ApplicationContext, workspace: Workspace
    ) -> None:
        from quantsmind_agent.revisions import RevisionJournal
        from quantsmind_agent.write_tools import build_undo_tool

        registry = ToolRegistry(permissions=_allowing_broker())
        journal = RevisionJournal()
        registry.register_all(
            build_mutating_tools(workspace, journal=journal, audit=context.security_audit)
        )
        registry.register_all([build_undo_tool(workspace, journal, audit=context.security_audit)])

        registry.execute("write_file", {"path": "undone.txt", "content": "one\n"})
        registry.execute("write_file", {"path": "undone.txt", "content": "two\n"})
        result = registry.execute("undo_file", {"path": "undone.txt"})

        assert result.ok
        mutations = _events_of(context.audit_sink, "file.mutated")
        assert [record["outcome"] for record in mutations][-1].startswith("undid_")

    def test_a_command_execution_is_written(
        self, context: ApplicationContext, workspace: Workspace
    ) -> None:
        registry = ToolRegistry(permissions=_allowing_broker())
        registry.register_all(
            build_command_tools(workspace, default_timeout=30.0, audit=context.security_audit)
        )

        result = registry.execute("run_command", {"command": f'{PYTHON} -c "print(1)"', "cwd": "."})

        assert result.ok
        records = _events_of(context.audit_sink, "command.executed")
        assert len(records) == 1
        assert records[0]["outcome"] == "succeeded"
        assert "exit=0" in records[0]["detail"]

    def test_a_trust_decision_is_written(
        self, context: ApplicationContext, sample_project: Path
    ) -> None:
        context.open_workspace(sample_project, trusted=None)

        context.set_workspace_trust(True)

        records = _events_of(context.audit_sink, "workspace.trust_changed")
        assert records


class TestFailClosed:
    def test_an_approval_that_cannot_be_recorded_does_not_reach_the_transcript(
        self, context: ApplicationContext, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        context.security_audit = SecurityAuditLog(
            sink=RotatingAuditSink(
                blocker / "audit" / AUDIT_STORE_NAME,
                anchor_path=tmp_path / "witness.json",
            )
        )
        emitted: list[Any] = []
        context.events.subscribe_all(lambda event: emitted.append(event))

        request = PermissionRequest(
            permission=Permission.WRITE_WORKSPACE,
            tool="write_file",
            summary="write a.txt",
            target="a.txt",
        )
        with pytest.raises(AuditPersistenceError):
            context._on_approval(request, ApprovalOutcome.ALLOW_ONCE)

        assert emitted == []


class TestNothingSecretReachesDisk:
    def test_a_credential_in_a_record_is_scrubbed_before_writing(
        self, context: ApplicationContext
    ) -> None:
        context.security_audit.record(
            AuditEventType.CREDENTIAL_CREATED,
            provider_id="openrouter",
            detail=f"stored api_key={CANARY}",
            subject=f"bearer {CANARY}",
        )

        written = "".join(
            segment.read_text(encoding="utf-8") for segment in context.audit_sink.segments()
        )
        assert CANARY not in written
        assert "openrouter" in written

    def test_a_command_argument_is_scrubbed(
        self, context: ApplicationContext, workspace: Workspace
    ) -> None:
        registry = ToolRegistry(permissions=_allowing_broker())
        registry.register_all(
            build_command_tools(workspace, default_timeout=30.0, audit=context.security_audit)
        )

        registry.execute(
            "run_command", {"command": f"{PYTHON} -c \"print('{CANARY}')\"", "cwd": "."}
        )

        written = "".join(
            segment.read_text(encoding="utf-8") for segment in context.audit_sink.segments()
        )
        assert CANARY not in written
        assert _events_of(context.audit_sink, "command.executed")

    def test_the_store_holds_no_credential_values_from_a_whole_session(
        self, context: ApplicationContext, workspace: Workspace
    ) -> None:
        registry = ToolRegistry(permissions=_allowing_broker())
        registry.register_all(build_mutating_tools(workspace, audit=context.security_audit))
        registry.execute("write_file", {"path": "s.txt", "content": f"key {CANARY}\n"})

        written = "".join(
            segment.read_text(encoding="utf-8") for segment in context.audit_sink.segments()
        )

        assert CANARY not in written
        assert "file.mutated" in written


def _allowing_broker() -> Any:
    from quantsmind_agent.security.permissions import PermissionBroker, StaticPermissionProvider

    return PermissionBroker(
        StaticPermissionProvider.for_v0_2(),
        approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
    )
