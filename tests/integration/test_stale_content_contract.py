"""Contract tests for the stale-content protection of the mutating tools.

``write_file``, ``edit_file`` and ``delete_file`` all accept an optional
``expected_sha256``: the fingerprint of the file as the model last read it, and
the write is refused when the file changed since then.

For that protection to be usable, the *read* path has to hand the model a
fingerprint it can pass back. Before M2B it did not: ``read_file`` returned
the content and the line counts, and no digest, so the parameter could never
be satisfied by anything the model had actually seen. The protection was
advertised and unreachable, and a file the user edited between the model's read
and the agent's write was silently overwritten.

These tests pin the contract end to end: read, then an external change, then a
write that carries the fingerprint from the read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.runtime.tools import ToolRegistry
from quantsmind_agent.security.permissions import (
    ApprovalOutcome,
    PermissionBroker,
    StaticPermissionProvider,
)
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.workspace_tools import build_default_tools
from quantsmind_agent.write_tools import build_mutating_tools

pytestmark = pytest.mark.integration


@pytest.fixture
def registry(workspace: Workspace) -> ToolRegistry:
    broker = PermissionBroker(
        StaticPermissionProvider.for_v0_2(),
        approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
    )
    tools = ToolRegistry(permissions=broker)
    tools.register_all(build_default_tools(workspace))
    tools.register_all(build_mutating_tools(workspace))
    return tools


def test_read_file_exposes_the_fingerprint_the_write_tools_expect(
    registry: ToolRegistry, workspace: Workspace
) -> None:
    """The read result must carry the digest the mutating tools ask for."""

    (workspace.root / "notes.txt").write_text("original\n", encoding="utf-8")

    result = registry.execute("read_file", {"path": "notes.txt"})

    assert result.ok
    assert "sha256" in result.data, (
        "read_file does not expose a content fingerprint, so expected_sha256 "
        "cannot be satisfied by anything the model has seen"
    )
    assert result.data["sha256"] == workspace.fingerprint("notes.txt").sha256


def test_a_write_after_an_external_change_is_refused(
    registry: ToolRegistry, workspace: Workspace
) -> None:
    """The user's edit must survive a write that carries the stale fingerprint."""

    target = workspace.root / "notes.txt"
    target.write_text("original\n", encoding="utf-8")

    read = registry.execute("read_file", {"path": "notes.txt"})
    assert read.ok
    fingerprint = read.data.get("sha256")

    # The user edits the file while the agent is thinking.
    target.write_text("the user changed this\n", encoding="utf-8")

    write = registry.execute(
        "write_file",
        {
            "path": "notes.txt",
            "content": "the agent replaced this\n",
            "expected_sha256": fingerprint,
        },
    )

    assert not write.ok, "the write was allowed over a file that had changed"
    assert "changed since you last read it" in (write.error or "")
    assert target.read_text(encoding="utf-8") == "the user changed this\n"


def test_a_delete_after_an_external_change_is_refused(
    registry: ToolRegistry, workspace: Workspace
) -> None:
    """A delete must not remove a file the user changed after the read."""

    target = workspace.root / "notes.txt"
    target.write_text("original\n", encoding="utf-8")

    read = registry.execute("read_file", {"path": "notes.txt"})
    assert read.ok
    fingerprint = read.data.get("sha256")

    target.write_text("the user changed this\n", encoding="utf-8")

    delete = registry.execute(
        "delete_file",
        {"path": "notes.txt", "expected_sha256": fingerprint},
    )

    assert not delete.ok, "the delete was allowed over a file that had changed"
    assert target.exists(), "the file the user changed was deleted"


def test_a_write_of_an_unchanged_file_is_still_allowed(
    registry: ToolRegistry, workspace: Workspace
) -> None:
    """The protection must not block a legitimate write."""

    target = workspace.root / "notes.txt"
    target.write_text("original\n", encoding="utf-8")

    read = registry.execute("read_file", {"path": "notes.txt"})
    fingerprint = read.data.get("sha256")

    write = registry.execute(
        "write_file",
        {
            "path": "notes.txt",
            "content": "the agent replaced this\n",
            "expected_sha256": fingerprint,
        },
    )

    assert write.ok, write.error
    assert target.read_text(encoding="utf-8") == "the agent replaced this\n"


def test_the_read_result_still_carries_the_content(
    registry: ToolRegistry, workspace: Workspace
) -> None:
    """Adding the fingerprint must not disturb the existing payload."""

    (workspace.root / "notes.txt").write_text("line one\nline two\n", encoding="utf-8")

    result = registry.execute("read_file", {"path": "notes.txt"})

    assert result.ok
    assert "line one" in result.data["content"]
    assert result.data["total_lines"] == 2
    assert result.data["path"] == "notes.txt"


def test_a_missing_file_is_still_refused(registry: ToolRegistry) -> None:
    """The fingerprint must not turn a missing file into a readable one."""

    result = registry.execute("read_file", {"path": "absent.txt"})

    assert not result.ok
    assert "sha256" not in result.data


def test_the_fingerprint_is_not_the_digest_of_the_returned_window(
    registry: ToolRegistry, workspace: Workspace
) -> None:
    """A partial read must still fingerprint the whole file."""

    (workspace.root / "notes.txt").write_text("a\nb\nc\n", encoding="utf-8")

    result = registry.execute("read_file", {"path": "notes.txt", "start_line": 1, "max_lines": 1})

    assert result.ok
    assert result.data["returned_lines"] == 1
    assert result.data["sha256"] == workspace.fingerprint("notes.txt").sha256
    assert Path(workspace.root / "notes.txt").read_text(encoding="utf-8") == "a\nb\nc\n"
