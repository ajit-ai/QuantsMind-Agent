"""WP0 unit tests: the resource governor is a control, not a probe.

Every policy the governor claims is asserted here as a measured decision, in the
four pressure conditions and for each protected/optional/heavy class of action.
"""

from __future__ import annotations

import threading

import pytest

from quantsmind_agent.diagnostics.governance import (
    DEGRADATION_FACTOR,
    ResourceAction,
    ResourceGovernor,
    ResourceStatus,
)
from quantsmind_agent.diagnostics.memory import (
    BUDGET_BYTES,
    DEGRADE_BYTES,
    REFUSE_BYTES,
    TYPICAL_BYTES,
    MemoryPressure,
    MemorySnapshot,
)

GIB = 1024**3


class FakeProbe:
    """A memory probe whose reading the test sets, and which counts samples."""

    def __init__(self, working_set_bytes: int = 100 * 1024 * 1024) -> None:
        self.working_set_bytes = working_set_bytes
        self.calls = 0

    def snapshot(self) -> MemorySnapshot:
        self.calls += 1
        used = self.working_set_bytes
        pressure = (
            MemoryPressure.EXHAUSTED
            if used >= REFUSE_BYTES
            else MemoryPressure.CRITICAL
            if used >= DEGRADE_BYTES
            else MemoryPressure.ELEVATED
            if used >= TYPICAL_BYTES
            else MemoryPressure.NORMAL
        )
        return MemorySnapshot(working_set_bytes=used, budget_bytes=BUDGET_BYTES, pressure=pressure)


def governor_at(used: int) -> ResourceGovernor:
    probe = FakeProbe(used)
    return ResourceGovernor(governor=probe)


NORMAL = 200 * 1024 * 1024
ELEVATED = int(1.6 * GIB)
CRITICAL = int(3.1 * GIB)
EXHAUSTED = int(3.5 * GIB)


class TestPressureClassification:
    def test_normal_pressure_allows_everything_at_full_budget(self) -> None:
        governor = governor_at(NORMAL)
        for action in ResourceAction:
            decision = governor.decide(action)
            assert decision.allowed, action
            assert not decision.degraded, action
            assert decision.pressure is MemoryPressure.NORMAL

    def test_elevated_pressure_is_reported_but_not_degrading(self) -> None:
        decision = governor_at(ELEVATED).decide(ResourceAction.WORKSPACE_INDEX)
        assert decision.pressure is MemoryPressure.ELEVATED
        assert decision.allowed
        assert not decision.degraded

    def test_critical_pressure_degrades_optional_work_only(self) -> None:
        governor = governor_at(CRITICAL)
        optional = governor.decide(ResourceAction.WORKSPACE_INDEX)
        assert optional.allowed and optional.degraded
        heavy_required = governor.decide(ResourceAction.COMMAND_RUN)
        assert heavy_required.allowed and not heavy_required.degraded

    def test_exhausted_pressure_refuses_heavy_work(self) -> None:
        governor = governor_at(EXHAUSTED)
        for action in (
            ResourceAction.HEAVY_FEATURE,
            ResourceAction.WORKSPACE_INDEX,
            ResourceAction.BACKGROUND_ANALYSIS,
            ResourceAction.COMMAND_RUN,
            ResourceAction.AUDIT_EXPORT,
        ):
            decision = governor.decide(action)
            assert not decision.allowed, action
            assert "refuse" in decision.reason or "refused" in decision.reason, action

    def test_exhausted_pressure_still_allows_light_optional_work(self) -> None:
        governor = governor_at(EXHAUSTED)
        assert governor.decide(ResourceAction.EDITOR_BUFFER).allowed
        assert governor.decide(ResourceAction.DIFF_RENDER).allowed


class TestProtectedWorkNeverYields:
    """Policy 1 and 4: security, integrity, recovery and user data hold."""

    @pytest.mark.parametrize(
        "action",
        [
            ResourceAction.CHANGESET_APPLY,
            ResourceAction.CHANGESET_RECOVERY,
            ResourceAction.AUDIT_PERSISTENCE,
        ],
    )
    def test_protected_actions_are_allowed_at_max_pressure(self, action: ResourceAction) -> None:
        decision = governor_at(6 * GIB).decide(action)
        assert decision.allowed
        assert not decision.degraded
        assert decision.pressure is MemoryPressure.EXHAUSTED

    def test_the_whole_protected_table_is_allowed_under_extreme_pressure(self) -> None:
        governor = governor_at(6 * GIB)
        protected = [a for a in ResourceAction if governor.policy(a).protected]
        assert protected, "the policy table must protect something"
        for action in protected:
            assert governor.decide(action).allowed, action


class TestEstimatedWorkload:
    def test_a_heavy_estimate_that_would_cross_refuse_is_declared_before_start(self) -> None:
        # 2.5 GiB used, 800 MiB estimated: the estimate is what trips the gate.
        governor = governor_at(int(2.5 * GIB))
        decision = governor.decide(
            ResourceAction.WORKSPACE_INDEX, estimated_bytes=800 * 1024 * 1024
        )
        assert not decision.allowed
        assert "would cross the refuse threshold" in decision.reason

    def test_a_small_estimate_under_the_same_pressure_is_allowed(self) -> None:
        governor = governor_at(int(2.5 * GIB))
        decision = governor.decide(ResourceAction.WORKSPACE_INDEX, estimated_bytes=1024 * 1024)
        assert decision.allowed

    def test_estimates_are_ignored_for_protected_work(self) -> None:
        decision = governor_at(NORMAL).decide(
            ResourceAction.CHANGESET_APPLY, estimated_bytes=100 * GIB
        )
        assert decision.allowed


class TestDegradedBudgets:
    def test_bounds_are_unchanged_at_normal_pressure(self) -> None:
        bounds = governor_at(NORMAL).bounds(max_files=600, max_bytes=2_000_000)
        assert (bounds.max_files, bounds.max_bytes) == (600, 2_000_000)
        assert not bounds.degraded and bounds.usable

    def test_bounds_shrink_by_the_documented_factor_when_degraded(self) -> None:
        bounds = governor_at(CRITICAL).bounds(max_files=600, max_bytes=2_000_000)
        assert bounds.degraded
        assert bounds.max_files == round(600 * DEGRADATION_FACTOR)
        assert bounds.max_bytes == round(2_000_000 * DEGRADATION_FACTOR)
        assert bounds.usable

    def test_bounds_are_zeroed_when_the_work_is_refused(self) -> None:
        bounds = governor_at(EXHAUSTED).bounds(max_files=600, max_bytes=2_000_000)
        assert bounds.refused
        assert bounds.max_files == 0 and bounds.max_bytes == 0
        assert not bounds.usable

    def test_a_degraded_budget_never_collapses_to_zero(self) -> None:
        bounds = governor_at(CRITICAL).bounds(max_files=1, max_bytes=1)
        assert bounds.max_files >= 1 and bounds.max_bytes >= 1

    def test_editor_buffer_budget_follows_pressure(self) -> None:
        assert governor_at(NORMAL).editor_buffer_budget(normal=50, degraded=20) == 50
        assert governor_at(CRITICAL).editor_buffer_budget(normal=50, degraded=20) == 20


class TestSamplingAndObservation:
    def test_samples_are_cached_within_the_interval(self) -> None:
        probe = FakeProbe(NORMAL)
        clock = _SteppingClock(0.0)
        governor = ResourceGovernor(governor=probe, sample_seconds=1.0, clock=clock)
        for _ in range(5):
            clock.advance(0.1)
            governor.decide(ResourceAction.EDITOR_BUFFER)
        assert probe.calls == 1

    def test_samples_are_refreshed_once_the_interval_expires(self) -> None:
        probe = FakeProbe(NORMAL)
        clock = _SteppingClock(0.0)
        governor = ResourceGovernor(governor=probe, sample_seconds=1.0, clock=clock)
        governor.decide(ResourceAction.EDITOR_BUFFER)
        clock.advance(1.5)
        governor.decide(ResourceAction.EDITOR_BUFFER)
        assert probe.calls == 2

    def test_force_rereads_immediately(self) -> None:
        probe = FakeProbe(NORMAL)
        governor = ResourceGovernor(governor=probe, sample_seconds=10_000)
        governor.snapshot()
        governor.snapshot(force=True)
        assert probe.calls == 2

    def test_status_counts_decisions_and_refusals(self) -> None:
        governor = governor_at(EXHAUSTED)
        governor.decide(ResourceAction.WORKSPACE_INDEX)
        governor.decide(ResourceAction.EDITOR_BUFFER)
        status = governor.status()
        assert status.decisions == 2
        assert status.refusals == 1
        assert status.last_refusal.startswith("workspace_index: refused")
        assert status.pressure is MemoryPressure.EXHAUSTED

    def test_status_names_the_actions_currently_running_degraded(self) -> None:
        governor = governor_at(CRITICAL)
        governor.decide(ResourceAction.BACKGROUND_ANALYSIS)
        assert "background_analysis" in governor.status().degradable_running
        governor.decide(ResourceAction.WORKSPACE_INDEX)
        assert "workspace_index" in governor.status().degradable_running

    def test_a_listener_sees_the_pressure_transition(self) -> None:
        probe = FakeProbe(NORMAL)
        governor = ResourceGovernor(governor=probe, sample_seconds=0.0)
        seen: list[ResourceStatus] = []
        governor.subscribe(seen.append)
        governor.decide(ResourceAction.EDITOR_BUFFER)  # establishes the baseline
        assert seen == []
        probe.working_set_bytes = CRITICAL
        governor.decide(ResourceAction.EDITOR_BUFFER)
        assert len(seen) == 1
        assert seen[0].pressure is MemoryPressure.CRITICAL

    def test_a_failing_listener_cannot_break_a_decision(self) -> None:
        probe = FakeProbe(NORMAL)
        governor = ResourceGovernor(governor=probe, sample_seconds=0.0)

        def explode(_status: ResourceStatus) -> None:
            raise RuntimeError("listener failed")

        governor.subscribe(explode)
        governor.decide(ResourceAction.EDITOR_BUFFER)
        probe.working_set_bytes = CRITICAL
        assert governor.decide(ResourceAction.EDITOR_BUFFER).degraded

    def test_describe_is_human_readable(self) -> None:
        text = governor_at(CRITICAL).decide(ResourceAction.WORKSPACE_INDEX).describe()
        assert text.startswith("workspace_index: allowed (degraded)")


class TestThreadSafety:
    def test_concurrent_decisions_do_not_lose_samples_or_counters(self) -> None:
        governor = ResourceGovernor(governor=FakeProbe(CRITICAL), sample_seconds=0.0)
        errors: list[BaseException] = []

        def hammer() -> None:
            try:
                for _ in range(200):
                    decision = governor.decide(ResourceAction.WORKSPACE_INDEX)
                    assert decision.allowed
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert governor.status().decisions == 6 * 200


class _SteppingClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float) -> None:
        self._now = start
        self._lock = threading.Lock()

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def __call__(self) -> float:
        return self._now
