"""Resource governance: the authority that decides what work may run.

:mod:`quantsmind_agent.diagnostics.memory` measures process memory. Measuring is
not governing, so this module owns the decisions: which operations may start,
which shrink under pressure, and which are refused rather than run unsafely.
Every expensive operation asks this object instead of reading thresholds
itself, so that one change of policy lands in exactly one place.

The six rules this module implements:

1. Memory pressure never degrades permission, trust, approval, containment or
   data-integrity behaviour. Those actions are `protected` and always allowed.
2. Optional indexing, background analysis and non-essential intelligence degrade.
3. When safety cannot be maintained, heavy work is refused rather than degraded.
4. Recovery paths and essential user data are preserved under pressure.
5. Thresholds and decisions are measurable: every decision is a value, and every
   decision the governor makes is counted and inspectable.
6. A hook is only a control once production calls it -- so the call sites live in
   the workspace indexer, the editor buffer manager and the application context,
   not in prose.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from quantsmind_agent.diagnostics.memory import (
    BUDGET_BYTES,
    REFUSE_BYTES,
    MemoryGovernor,
    MemoryPressure,
    MemorySnapshot,
)

__all__ = [
    "DEGRADATION_FACTOR",
    "MemoryProbe",
    "ResourceAction",
    "ResourceBounds",
    "ResourceDecision",
    "ResourceGovernor",
    "ResourceStatus",
]

#: How much an optional, degradable budget shrinks once pressure is CRITICAL.
DEGRADATION_FACTOR = 0.5

#: A sample older than this is re-read. The Windows probe is a syscall, and the
#: governor is consulted from the GUI thread as well as from workers.
_DEFAULT_SAMPLE_SECONDS = 1.0


class ResourceAction(StrEnum):
    """Every kind of work the governor is asked about."""

    HEAVY_FEATURE = "heavy_feature"
    WORKSPACE_INDEX = "workspace_index"
    BACKGROUND_ANALYSIS = "background_analysis"
    EDITOR_BUFFER = "editor_buffer"
    COMMAND_RUN = "command_run"
    DIFF_RENDER = "diff_render"
    CONTEXT_ASSEMBLY = "context_assembly"
    AUDIT_EXPORT = "audit_export"
    CHANGESET_APPLY = "changeset_apply"
    CHANGESET_RECOVERY = "changeset_recovery"
    AUDIT_PERSISTENCE = "audit_persistence"


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    """How one action behaves under pressure."""

    action: ResourceAction
    label: str
    #: Rule 2: the work may shrink rather than stop.
    optional: bool
    #: Rule 3: the work is large enough that it must be refused, not degraded,
    #: when the budget can no longer hold it.
    heavy: bool
    #: Rules 1 and 4: security, integrity, recovery and user data never yield
    #: to memory pressure.
    protected: bool = False


_POLICY_TABLE: tuple[ResourcePolicy, ...] = (
    ResourcePolicy(ResourceAction.HEAVY_FEATURE, "new optional subsystem", True, True),
    ResourcePolicy(ResourceAction.WORKSPACE_INDEX, "workspace indexing", True, True),
    ResourcePolicy(ResourceAction.BACKGROUND_ANALYSIS, "background analysis", True, True),
    ResourcePolicy(ResourceAction.EDITOR_BUFFER, "editor buffer retention", True, False),
    ResourcePolicy(ResourceAction.COMMAND_RUN, "command execution", False, True),
    ResourcePolicy(ResourceAction.DIFF_RENDER, "large diff rendering", True, False),
    ResourcePolicy(ResourceAction.CONTEXT_ASSEMBLY, "prompt context assembly", False, False),
    ResourcePolicy(ResourceAction.AUDIT_EXPORT, "audit export", True, True),
    ResourcePolicy(ResourceAction.CHANGESET_APPLY, "ChangeSet apply", False, False, protected=True),
    ResourcePolicy(
        ResourceAction.CHANGESET_RECOVERY, "transaction recovery", False, False, protected=True
    ),
    ResourcePolicy(
        ResourceAction.AUDIT_PERSISTENCE, "audit persistence", False, False, protected=True
    ),
)

POLICIES: dict[ResourceAction, ResourcePolicy] = {p.action: p for p in _POLICY_TABLE}


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    """The governor's answer to one request."""

    action: ResourceAction
    allowed: bool
    degraded: bool
    pressure: MemoryPressure
    reason: str
    working_set_bytes: int
    budget_bytes: int = BUDGET_BYTES

    @property
    def working_set_mib(self) -> int:
        return round(self.working_set_bytes / (1024 * 1024))

    def describe(self) -> str:
        verdict = "allowed" if self.allowed else "refused"
        state = " (degraded)" if self.degraded else ""
        return f"{self.action.value}: {verdict}{state} - {self.reason}"


@dataclass(frozen=True, slots=True)
class ResourceBounds:
    """A degradable set of work limits, as the governor returns them."""

    max_files: int
    max_bytes: int
    degraded: bool
    refused: bool

    @property
    def usable(self) -> bool:
        return not self.refused and self.max_files > 0


@dataclass(frozen=True, slots=True)
class ResourceStatus:
    """What Control Center and the status bar show."""

    pressure: MemoryPressure
    working_set_bytes: int
    budget_bytes: int
    decisions: int
    refusals: int
    degradable_running: tuple[str, ...] = ()
    last_refusal: str = ""

    @property
    def working_set_mib(self) -> int:
        return round(self.working_set_bytes / (1024 * 1024))

    @property
    def budget_mib(self) -> int:
        return round(self.budget_bytes / (1024 * 1024))

    def describe(self) -> str:
        return f"{self.working_set_mib} MiB of {self.budget_mib} MiB ({self.pressure.value})"


class MemoryProbe(Protocol):
    """The one reading the governor needs, named so it can be verified.

    :class:`~quantsmind_agent.diagnostics.memory.MemoryGovernor` satisfies it.
    Naming the seam rather than the class keeps the governor's dependency a
    measurement, not an implementation, and lets a test hand it a pressure it
    chooses without subclassing anything.
    """

    def snapshot(self) -> MemorySnapshot: ...


class ResourceGovernor:
    """Samples memory, applies the policy table, and remembers what it decided.

    Thread-safe: the probe is cached behind a lock, listeners are called outside
    it, and the counters use atomic-ish increments under the same lock.
    """

    def __init__(
        self,
        *,
        governor: MemoryProbe | None = None,
        sample_seconds: float = _DEFAULT_SAMPLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = governor or MemoryGovernor()
        self._sample_seconds = max(0.0, sample_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._sample: MemorySnapshot | None = None
        self._sampled_at = float("-inf")
        self._listeners: list[Callable[[ResourceStatus], None]] = []
        self._last_pressure: MemoryPressure | None = None
        self._decisions = 0
        self._refusals = 0
        self._degraded: set[ResourceAction] = set()
        self._last_refusal = ""

    # -- measurement -------------------------------------------------------
    def snapshot(self, *, force: bool = False) -> MemorySnapshot:
        """The most recent sample, re-read when it has gone stale."""

        with self._lock:
            now = self._clock()
            if force or self._sample is None or (now - self._sampled_at) >= self._sample_seconds:
                self._sample = self._probe.snapshot()
                self._sampled_at = now
            return self._sample

    def pressure(self) -> MemoryPressure:
        return self.snapshot().pressure

    # -- decisions ---------------------------------------------------------
    def policy(self, action: ResourceAction) -> ResourcePolicy:
        return POLICIES[action]

    def decide(
        self,
        action: ResourceAction,
        *,
        estimated_bytes: int = 0,
    ) -> ResourceDecision:
        """Whether ``action`` may start, and whether it must run degraded.

        ``estimated_bytes`` is the caller's own claim about what it is about to
        allocate. A heavy operation that would cross the refuse threshold is
        refused before it starts, which is the only moment refusal is cheap.
        """

        policy = POLICIES[action]
        sample = self.snapshot()
        if sample.pressure in (MemoryPressure.CRITICAL, MemoryPressure.EXHAUSTED):
            # A refusal or a degraded budget is a consequential answer, so it is
            # never given on a stale reading: re-measure before saying no.
            sample = self.snapshot(force=True)
        used = sample.working_set_bytes
        pressure = sample.pressure

        if policy.protected:
            decision = ResourceDecision(
                action=action,
                allowed=True,
                degraded=False,
                pressure=pressure,
                reason="protected by the security and recovery rules; pressure does not apply",
                working_set_bytes=used,
                budget_bytes=sample.budget_bytes,
            )
            self._record(decision)
            return decision

        degraded = pressure is MemoryPressure.CRITICAL and policy.optional
        allowed = True
        reason = f"pressure is {pressure.value}"

        if pressure is MemoryPressure.EXHAUSTED and policy.heavy:
            allowed = False
            reason = (
                "the process is at or past the refuse threshold; this is heavy work, "
                "so it is refused rather than run unsafely"
            )
        elif policy.heavy and estimated_bytes > 0 and (used + estimated_bytes) >= REFUSE_BYTES:
            allowed = False
            reason = (
                f"the estimate for this operation ({round(estimated_bytes / (1024 * 1024))} MiB) "
                "would cross the refuse threshold"
            )
        elif degraded:
            reason = "pressure is critical; optional work runs with a reduced budget"
        elif pressure is MemoryPressure.ELEVATED:
            reason = "pressure is elevated; work runs at full budget"

        decision = ResourceDecision(
            action=action,
            allowed=allowed,
            degraded=degraded,
            pressure=pressure,
            reason=reason,
            working_set_bytes=used,
            budget_bytes=sample.budget_bytes,
        )
        self._record(decision)
        return decision

    def is_allowed(self, action: ResourceAction, *, estimated_bytes: int = 0) -> bool:
        return self.decide(action, estimated_bytes=estimated_bytes).allowed

    def bounds(
        self,
        *,
        max_files: int,
        max_bytes: int,
        action: ResourceAction = ResourceAction.WORKSPACE_INDEX,
    ) -> ResourceBounds:
        """The budgets optional background work should actually use.

        Degradation halves the budgets; refusal returns zeros so a caller that
        ignores the decision still cannot walk the whole tree.
        """

        decision = self.decide(action)
        if not decision.allowed:
            return ResourceBounds(max_files=0, max_bytes=0, degraded=False, refused=True)
        if not decision.degraded:
            return ResourceBounds(
                max_files=max_files, max_bytes=max_bytes, degraded=False, refused=False
            )
        return ResourceBounds(
            max_files=max(1, round(max_files * DEGRADATION_FACTOR)),
            max_bytes=max(1, round(max_bytes * DEGRADATION_FACTOR)),
            degraded=True,
            refused=False,
        )

    def editor_buffer_budget(self, *, normal: int, degraded: int) -> int:
        """How many open editor buffers to retain right now.

        Dirty buffers are never dropped by this budget; the caller enforces that.
        """

        decision = self.decide(ResourceAction.EDITOR_BUFFER)
        return degraded if decision.degraded else normal

    # -- observation -------------------------------------------------------
    def status(self) -> ResourceStatus:
        with self._lock:
            sample = self.snapshot()
            return ResourceStatus(
                pressure=sample.pressure,
                working_set_bytes=sample.working_set_bytes,
                budget_bytes=sample.budget_bytes,
                decisions=self._decisions,
                refusals=self._refusals,
                degradable_running=tuple(sorted(a.value for a in self._degraded)),
                last_refusal=self._last_refusal,
            )

    def subscribe(self, listener: Callable[[ResourceStatus], None]) -> None:
        """Called whenever the measured pressure changes classification."""

        with self._lock:
            self._listeners.append(listener)

    def reset_for_tests(self) -> None:
        """Forget the cached sample and the counters (used by tests and reopens)."""

        with self._lock:
            self._sample = None
            self._sampled_at = float("-inf")
            self._decisions = 0
            self._refusals = 0
            self._degraded.clear()
            self._last_refusal = ""
            self._last_pressure = None

    # -- internals ---------------------------------------------------------
    def _record(self, decision: ResourceDecision) -> None:
        listeners: list[Callable[[ResourceStatus], None]] = []
        with self._lock:
            self._decisions += 1
            if decision.degraded:
                self._degraded.add(decision.action)
            else:
                self._degraded.discard(decision.action)
            if not decision.allowed:
                self._refusals += 1
                self._last_refusal = decision.describe()
            current = self.snapshot().pressure
            if self._last_pressure is not None and current is not self._last_pressure:
                listeners = list(self._listeners)
            self._last_pressure = current
            status = ResourceStatus(
                pressure=current,
                working_set_bytes=decision.working_set_bytes,
                budget_bytes=decision.budget_bytes,
                decisions=self._decisions,
                refusals=self._refusals,
                degradable_running=tuple(sorted(a.value for a in self._degraded)),
                last_refusal=self._last_refusal,
            )
        for listener in listeners:
            # A listener that throws must never change a governance decision.
            with contextlib.suppress(Exception):
                listener(status)
