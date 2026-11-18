"""WP0 integration: the governor is consulted by the work it claims to govern.

Policy 6 of the owner's ruling is that a hook is not a control until production
calls it. These tests drive the real indexing path with a real governor and a
settable memory reading, and assert that the *work* changed -- not that a method
exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.app.context import ApplicationContext
from quantsmind_agent.diagnostics.governance import ResourceGovernor
from quantsmind_agent.diagnostics.memory import BUDGET_BYTES, MemoryPressure, MemorySnapshot
from quantsmind_agent.persistence.paths import AppPaths
from quantsmind_agent.project_intel import ProjectIntelligence
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.workspace_intel import WorkspaceIntelligence

GIB = 1024**3

NORMAL_BYTES = 200 * 1024 * 1024
CRITICAL_BYTES = int(3.1 * GIB)
EXHAUSTED_BYTES = int(3.5 * GIB)


class SettableProbe:
    """A memory probe the test moves between pressure bands."""

    def __init__(self, working_set_bytes: int = NORMAL_BYTES) -> None:
        self.working_set_bytes = working_set_bytes

    def snapshot(self) -> MemorySnapshot:
        used = self.working_set_bytes
        pressure = (
            MemoryPressure.EXHAUSTED
            if used >= int(3.2 * GIB)
            else MemoryPressure.CRITICAL
            if used >= int(3.0 * GIB)
            else MemoryPressure.ELEVATED
            if used >= int(1.5 * GIB)
            else MemoryPressure.NORMAL
        )
        return MemorySnapshot(working_set_bytes=used, budget_bytes=BUDGET_BYTES, pressure=pressure)


def make_governor(used: int) -> tuple[ResourceGovernor, SettableProbe]:
    probe = SettableProbe(used)
    return ResourceGovernor(governor=probe), probe


@pytest.fixture
def many_source_files(sample_project: Path) -> Path:
    """Thirteen candidate modules, so a halved or refused budget is observable."""

    package = sample_project / "src" / "app"
    for index in range(10):
        (package / f"module_{index}.py").write_text(
            f'"""Module {index}."""\n\nVALUE = {index}\n\n\ndef run() -> int:\n'
            f'    """Return the value."""\n    return VALUE\n',
            encoding="utf-8",
        )
    return sample_project


class TestIndexingYieldsToPressure:
    def test_full_pressure_indexes_to_the_requested_cap(
        self, workspace: Workspace, many_source_files: Path
    ) -> None:
        governor, _ = make_governor(NORMAL_BYTES)
        intel = ProjectIntelligence(workspace, max_indexed_files=10, resources=governor)
        assert intel.index() == 10
        assert intel.is_indexed
        assert intel.resource_note == ""

    def test_critical_pressure_halves_the_index(
        self, workspace: Workspace, many_source_files: Path
    ) -> None:
        governor, _ = make_governor(CRITICAL_BYTES)
        intel = ProjectIntelligence(workspace, max_indexed_files=10, resources=governor)
        assert intel.index() == 5, "degradation must be measurable, not prose"
        assert intel.is_indexed
        assert "degraded" in intel.resource_note

    def test_exhausted_pressure_refuses_the_index_and_says_why(
        self, workspace: Workspace, many_source_files: Path
    ) -> None:
        governor, _ = make_governor(EXHAUSTED_BYTES)
        intel = ProjectIntelligence(workspace, max_indexed_files=10, resources=governor)
        assert intel.index() == 0
        assert not intel.is_indexed
        assert "refused" in intel.resource_note

    def test_refusal_is_retracted_once_pressure_returns(
        self, workspace: Workspace, many_source_files: Path
    ) -> None:
        governor, probe = make_governor(EXHAUSTED_BYTES)
        intel = ProjectIntelligence(workspace, max_indexed_files=10, resources=governor)
        assert intel.index() == 0
        probe.working_set_bytes = NORMAL_BYTES
        assert intel.index() == 10
        assert intel.resource_note == ""

    def test_without_a_governor_the_behaviour_is_unchanged(
        self, workspace: Workspace, many_source_files: Path
    ) -> None:
        intel = ProjectIntelligence(workspace, max_indexed_files=10)
        assert intel.index() == 10
        assert intel.resource_note == ""


class TestSnapshotYieldsToBackgroundAnalysis:
    def test_a_degraded_process_keeps_a_smaller_but_still_partial_sample(
        self, workspace: Workspace, many_source_files: Path
    ) -> None:
        governor, _ = make_governor(CRITICAL_BYTES)
        intel = WorkspaceIntelligence(workspace, max_files=20, resources=governor)
        snapshot = intel.snapshot
        assert len(snapshot.files) <= 10
        assert snapshot.file_count > len(snapshot.files)
        # The honest claim: a sample taken from a bigger walk says it is partial.
        assert snapshot.complete is False

    def test_an_exhausted_process_returns_an_empty_sample_and_says_so(
        self, workspace: Workspace, many_source_files: Path
    ) -> None:
        governor, _ = make_governor(EXHAUSTED_BYTES)
        intel = WorkspaceIntelligence(workspace, max_files=20, resources=governor)
        snapshot = intel.snapshot
        assert len(snapshot.files) == 0
        assert snapshot.file_count > 0, "the walk still saw the project"
        assert snapshot.complete is False


class TestContextOwnsTheGovernor:
    def test_the_application_context_exposes_one_governor(self, app_paths: AppPaths) -> None:
        context = ApplicationContext.create(paths=app_paths)
        try:
            assert isinstance(context.resources, ResourceGovernor)
            assert context.resources.status().decisions >= 0
        finally:
            context.shutdown()

    def test_an_opened_workspace_is_given_the_context_governor(
        self, app_paths: AppPaths, many_source_files: Path
    ) -> None:
        context = ApplicationContext.create(paths=app_paths)
        try:
            governor, probe = make_governor(EXHAUSTED_BYTES)
            context.resources = governor
            context.open_workspace(many_source_files, trusted=True)
            intel = context.project_intel
            assert intel is not None
            assert intel.index() == 0, "the intelligence must ask the context's governor"
            assert "refused" in intel.resource_note
            probe.working_set_bytes = NORMAL_BYTES
            # 13 = the 10 added modules plus the 3 the sample project ships with.
            assert intel.index() == 13
            assert intel.is_indexed
        finally:
            context.shutdown()

    def test_governor_pressure_does_not_change_the_tool_gate(
        self, app_paths: AppPaths, many_source_files: Path
    ) -> None:
        """Policy 1: pressure must not silently remove a user's permissions."""

        context = ApplicationContext.create(paths=app_paths)
        try:
            context.open_workspace(many_source_files, trusted=True)
            allowed_at_normal = sorted(context.tools.names)
            governor, _ = make_governor(EXHAUSTED_BYTES)
            context.resources = governor
            context._rebuild()
            assert allowed_at_normal, "a trusted workspace must still expose its tools"
            assert sorted(context.tools.names) == allowed_at_normal
        finally:
            context.shutdown()
