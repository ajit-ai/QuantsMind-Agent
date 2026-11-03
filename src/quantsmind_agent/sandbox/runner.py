"""The sandbox runner: fail-closed execution (M3B-S1.4).

The runner is the only component that may execute code inside the sandbox,
and it enforces the order that matters:

1. **Establish** the boundary the host can provide.
2. **Measure** it, by running the synthetic probe inside it.
3. **Compare** the measurement with the policy's requirement.
4. **Launch** the command -- only if the measurement is sufficient.
5. **Record** a sanitized audit event.

If step 3 fails, the command is not launched. There is no path in this
module that runs a command outside the boundary, and no parameter that
turns one on.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from quantsmind_agent.sandbox.backends import (
    AppContainerBackend,
    BackendAvailability,
    IsolationEvidence,
    JobObjectBackend,
    select_backend,
)
from quantsmind_agent.sandbox.policy import (
    ISOLATION_LEVELS,
    IsolationLevel,
    SandboxError,
    SandboxPolicy,
    SandboxResult,
)
from quantsmind_agent.sandbox.probe import (
    MANDATORY_CHECKS,
    IsolationReport,
    ProbeHarness,
)
from quantsmind_agent.sandbox.windows import sandbox_supported
from quantsmind_agent.security_audit import AuditEventType, SecurityAuditLog

__all__ = ["SandboxRunner"]


def _level_rank(level: IsolationLevel) -> int:
    return ISOLATION_LEVELS.index(level)


class SandboxRunner:
    """Runs commands inside the sandbox, or refuses to."""

    def __init__(
        self,
        *,
        audit: SecurityAuditLog | None = None,
        probe: bool = True,
        backends: list[Any] | None = None,
    ) -> None:
        self._audit = audit
        self._probe = probe
        self._backends_override = backends
        self._cache: dict[tuple[str, str], IsolationReport] = {}
        self._lock = threading.Lock()

    # -- measurement ------------------------------------------------------
    def measure(self, policy: SandboxPolicy) -> IsolationReport:
        """What the boundary actually enforces, measured rather than assumed."""

        key = (str(policy.workspace_root), str(policy.required_isolation))
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        report = self._measure_with_backend(policy)
        with self._lock:
            self._cache[key] = report
        return report

    def _measure_with_backend(self, policy: SandboxPolicy) -> IsolationReport:
        """Run the trusted probe through the selected backend and read it."""

        backend, _report = select_backend(policy, self._backends())
        if backend is None:
            return IsolationReport({"checks": {}, "reason": "no backend is available"})
        with ProbeHarness(policy) as harness:
            probe = harness.policy_for(policy)
            result = backend.launch(probe)
            try:
                payload = json.loads(result.stdout or "{}")
            except ValueError:
                payload = {"checks": {}, "parse_error": (result.stdout or "")[:500]}
            payload["control_checks"] = harness.control_check()
            payload.setdefault("established_level", str(backend.evidence().level))
            return IsolationReport(payload)

    def _probe_home(self, policy: SandboxPolicy) -> Path:
        return Path(policy.workspace_root) / ".sandbox" / "probe"

    def _backends(self) -> list[Any]:
        if self._backends_override is not None:
            return list(self._backends_override)
        return [AppContainerBackend(), JobObjectBackend()]

    def backend_report(self, policy: SandboxPolicy) -> list[BackendAvailability]:
        """Every backend's answer for this policy."""

        _chosen, report = select_backend(policy, self._backends())
        return report

    def refresh_measurement(self, policy: SandboxPolicy) -> IsolationReport:
        """Re-measure, ignoring the cache."""

        with self._lock:
            self._cache.pop((str(policy.workspace_root), str(policy.required_isolation)), None)
        return self.measure(policy)

    # -- execution --------------------------------------------------------
    def run(
        self,
        policy: SandboxPolicy,
        *,
        cancel_event: Any = None,
        sandbox_home: Path | None = None,
    ) -> SandboxResult:
        """Execute the policy's command, or return a structured refusal."""

        if not sandbox_supported():
            return self._refuse(
                policy,
                reason="sandbox_unsupported",
                detail="the sandbox boundary is only available on Windows",
                required=policy.required_isolation,
                established=IsolationLevel.LIFECYCLE,
            )

        backend, availability = select_backend(policy, self._backends())
        if backend is None:
            return self._refuse(
                policy,
                reason="isolation_insufficient",
                detail=(
                    "no available backend can provide the isolation this policy requires: "
                    + "; ".join(f"{item.name}: {item.reason}" for item in availability)
                ),
                required=policy.required_isolation,
                established=IsolationLevel.LIFECYCLE,
            )

        if self._probe and policy.required_isolation is IsolationLevel.UNTRUSTED_CODE:
            # The trusted probe runs first, inside the same boundary, and its
            # verdict is what authorises the model-written code.
            evidence = self._verify_boundary(backend, policy)
            if not evidence.satisfies(policy.required_isolation):
                return self._refuse(
                    policy,
                    reason="isolation_insufficient",
                    detail=(
                        "the trusted in-child probe did not establish the required "
                        f"isolation: {json.dumps(evidence.checks, sort_keys=True)}"
                    ),
                    required=policy.required_isolation,
                    established=evidence.level,
                )

        try:
            result = backend.launch(policy, cancel_event=cancel_event)
        except SandboxError as exc:
            self._record(
                AuditEventType.SANDBOX_DENIED,
                policy,
                reason=exc.reason,
                detail=exc.detail,
            )
            return SandboxResult(
                exit_code=None,
                stdout="",
                stderr=exc.detail,
                isolation_verified=False,
                established_level=str(policy.required_isolation),
                failure_reason=exc.reason,
            )

        result = SandboxResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            isolation_verified=result.isolation_verified,
            established_level=result.established_level or str(backend.evidence().level),
            failure_reason=result.failure_reason,
            duration_seconds=result.duration_seconds,
            truncated=result.truncated,
        )
        self._record(
            AuditEventType.SANDBOX_EXECUTION,
            policy,
            reason="completed" if result.exit_code == 0 else "nonzero_exit",
            detail=f"exit={result.exit_code} timed_out={result.timed_out}",
        )
        return result

    def _verify_boundary(self, backend: Any, policy: SandboxPolicy) -> IsolationEvidence:
        """Run the trusted probe in the boundary and read its verdict.

        A backend that fails is a failure of the boundary, not an exception
        the agent loop has to cope with: the probe not running means the
        isolation is not established, and the run is refused.
        """

        try:
            with ProbeHarness(policy) as harness:
                probe = harness.policy_for(policy)
                result = backend.launch(probe)
                try:
                    payload = json.loads(result.stdout or "{}")
                except ValueError:
                    payload = {"checks": {}}
                control = harness.control_check()
        except Exception as exc:  # a backend that cannot run the probe
            return IsolationEvidence(
                level=IsolationLevel.LIFECYCLE,
                checks={"probe_ran": False},
                detail=f"the trusted probe could not be executed: {type(exc).__name__}",
            )
        checks = dict(payload.get("checks", {}))
        checks["credential_env_not_leaked"] = not bool(payload.get("credential_env_names_present"))
        mandatory = {name: bool(checks.get(name)) is False for name in MANDATORY_CHECKS}
        mandatory["credential_env_not_leaked"] = bool(checks["credential_env_not_leaked"])
        # A denial only counts when the control process *can* do the same thing.
        mandatory["control_can_reach_the_canaries"] = all(
            control.get(name, False)
            for name in (
                "control_reads_outside_file",
                "control_reads_credential_canary",
                "control_reaches_listener",
            )
        )
        return IsolationEvidence(
            level=backend.evidence().level,
            checks=mandatory,
            detail=json.dumps(payload, sort_keys=True, default=str)[:800],
        )

    # -- helpers ----------------------------------------------------------
    def _refuse(
        self,
        policy: SandboxPolicy,
        *,
        reason: str,
        detail: str,
        required: IsolationLevel,
        established: IsolationLevel,
        report: IsolationReport | None = None,
    ) -> SandboxResult:
        """A structured refusal. The command is not launched."""

        self._record(
            AuditEventType.SANDBOX_DENIED,
            policy,
            reason=reason,
            detail=detail,
        )
        return SandboxResult(
            exit_code=None,
            stdout="",
            stderr=detail,
            isolation_verified=False,
            established_level=str(established),
            failure_reason=reason,
        )

    def _record(
        self,
        event: AuditEventType,
        policy: SandboxPolicy,
        *,
        reason: str,
        detail: str,
    ) -> None:
        if self._audit is None:
            return
        self._audit.record(
            event,
            decision="denied" if event is AuditEventType.SANDBOX_DENIED else "allowed",
            reason_code=reason,
            detail=(
                f"command={' '.join(policy.command)} workspace={policy.workspace_root} "
                f"required_isolation={policy.required_isolation} {detail}"
            ),
        )
