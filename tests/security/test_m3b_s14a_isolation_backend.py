"""M3B-S1.4A tests: the isolation backend, the AppContainer path and the
activation.

The tests are separated by what they actually prove:

* **OS-enforced** tests use the real Windows boundary;
* **mocked** tests exercise the API sequence and the failure paths with a
  stub, and are labelled as such;
* **host-blocked** tests assert the *refusal*, which is negative safety
  evidence, and are labelled as such.

A mocked test is never presented as proof of OS isolation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ``quantsmind_agent.sandbox`` imports ``ctypes.wintypes`` at module scope,
# which raises on non-Windows. Decide the platform before importing it so
# collection succeeds everywhere and this module is reported as skipped.
if sys.platform != "win32":
    pytest.skip("the Windows isolation primitives are Windows-only", allow_module_level=True)

from quantsmind_agent.sandbox import IsolationLevel, SandboxPolicy
from quantsmind_agent.sandbox.activate import (
    CONFIRMATION_PHRASE,
    activation_plan,
    is_elevated,
)
from quantsmind_agent.sandbox.activate import (
    main as activate_main,
)
from quantsmind_agent.sandbox.appcontainer import (
    APP_CONTAINER_NAME,
    detect_appcontainer_capability,
    token_privileges,
)
from quantsmind_agent.sandbox.backends import (
    BackendAvailability,
    JobObjectBackend,
    select_backend,
)
from quantsmind_agent.sandbox.runner import SandboxRunner

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the Windows isolation primitives are Windows-only"
)


def _policy(
    root: Path, command: tuple[str, ...] = ("placeholder",), **kwargs: Any
) -> SandboxPolicy:
    options: dict[str, Any] = {
        "workspace_root": root,
        "allowed_command": command,
        "timeout_seconds": 30.0,
        "max_output_bytes": 100_000,
        "max_child_processes": 4,
        "max_memory_bytes": 512 * 1024 * 1024,
    }
    options.update(kwargs)
    return SandboxPolicy(**options)


class TestBackendSelection:
    """S14A-G2, S14A-G11: the backend is chosen by capability, never by hope."""

    def test_the_job_object_backend_cannot_satisfy_untrusted_code(self, tmp_path: Path) -> None:
        backend = JobObjectBackend()
        availability = backend.availability(_policy(tmp_path))
        assert availability.available is True
        assert availability.level is IsolationLevel.LIFECYCLE

    def test_no_insecure_fallback_is_selected(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, required_isolation=IsolationLevel.UNTRUSTED_CODE)
        chosen, report = select_backend(policy)
        assert chosen is None or chosen.name == "appcontainer"
        for item in report:
            if item.name == "job_object":
                assert item.level is IsolationLevel.LIFECYCLE

    def test_the_selection_reports_every_backend(self, tmp_path: Path) -> None:
        _chosen, report = select_backend(_policy(tmp_path))
        names = {item.name for item in report}
        assert "appcontainer" in names
        assert "job_object" in names

    def test_an_available_backend_is_selected_for_lifecycle(self, tmp_path: Path) -> None:
        chosen, _report = select_backend(_policy(tmp_path))
        assert chosen is not None
        assert chosen.name == "job_object"


class TestAppContainerDetection:
    """S14A-G3: the detection is read-only and it is honest."""

    def test_the_detection_changes_nothing(self) -> None:
        first = detect_appcontainer_capability()
        second = detect_appcontainer_capability()
        assert first.to_dict() == second.to_dict()

    def test_the_apis_are_present_on_this_host(self) -> None:
        capability = detect_appcontainer_capability()
        assert capability.api_present is True

    def test_the_token_privileges_are_enumerated(self) -> None:
        privileges = token_privileges()
        assert "SeChangeNotifyPrivilege" in privileges

    def test_the_detection_names_what_is_missing(self) -> None:
        capability = detect_appcontainer_capability()
        if capability.available:
            assert capability.missing_requirements == ()
        else:
            assert capability.reason
            assert capability.missing_requirements


class TestActivation:
    """S14A-G15: the activation is non-destructive by default."""

    def test_the_plan_is_a_dry_run(self, tmp_path: Path) -> None:
        plan = activation_plan(tmp_path)
        assert plan["profile_name"] == APP_CONTAINER_NAME
        assert plan["changes"]
        assert "the owner's credentials and the credential store" in plan["not_changed"]
        for change in plan["changes"]:
            assert change["reversible"]

    def test_apply_without_the_confirmation_phrase_changes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = activate_main(["apply", "--workspace", str(tmp_path)])
        assert code == 2
        assert "REFUSED" in capsys.readouterr().err

    def test_apply_with_the_phrase_but_without_elevation_changes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        if is_elevated():
            pytest.skip("this host is elevated; the refusal path cannot be exercised")
        code = activate_main(
            ["apply", "--workspace", str(tmp_path), "--confirm", CONFIRMATION_PHRASE]
        )
        assert code == 2
        assert "elevated" in capsys.readouterr().err

    def test_apply_without_a_workspace_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = activate_main(["apply", "--confirm", CONFIRMATION_PHRASE])
        assert code == 2
        assert "--workspace" in capsys.readouterr().err

    def test_check_prints_the_plan_and_changes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = activate_main(["check", "--workspace", str(tmp_path)])
        assert code == 0
        payload = json.loads(capsys.readouterr().out.split("This is a dry run")[0])
        assert payload["profile_name"] == APP_CONTAINER_NAME


class TestFailClosed:
    """S14A-G11: without the boundary, nothing runs."""

    def test_untrusted_code_is_refused_on_this_host(self, tmp_path: Path) -> None:
        policy = _policy(
            tmp_path,
            (sys.executable, "-c", "print('must not run')"),
            required_isolation=IsolationLevel.UNTRUSTED_CODE,
        )
        result = SandboxRunner().run(policy)
        assert result.isolation_verified is False
        assert "must not run" not in result.stdout

    def test_the_refusal_names_the_missing_requirements(self, tmp_path: Path) -> None:
        policy = _policy(
            tmp_path,
            (sys.executable, "-c", "pass"),
            required_isolation=IsolationLevel.UNTRUSTED_CODE,
        )
        runner = SandboxRunner()
        report = runner.backend_report(policy)
        if not any(
            item.available and item.level is IsolationLevel.UNTRUSTED_CODE for item in report
        ):
            result = runner.run(policy)
            assert result.failure_reason == "isolation_insufficient"
            assert result.stderr

    def test_a_fake_backend_cannot_claim_the_boundary(self, tmp_path: Path) -> None:
        """A backend that only *claims* the level is not selected."""

        class _FakeBackend:
            name = "fake"

            def availability(self, policy: SandboxPolicy) -> BackendAvailability:
                return BackendAvailability(
                    name=self.name,
                    available=True,
                    level=IsolationLevel.UNTRUSTED_CODE,
                    reason="claimed, not proven",
                )

            def evidence(self) -> Any:
                from quantsmind_agent.sandbox.backends import IsolationEvidence

                return IsolationEvidence(
                    level=IsolationLevel.UNTRUSTED_CODE, checks={}, detail="claimed"
                )

            def launch(self, policy: SandboxPolicy, *, cancel_event: Any = None) -> Any:
                raise AssertionError("a claimed boundary must not run anything")

        policy = _policy(
            tmp_path,
            (sys.executable, "-c", "pass"),
            required_isolation=IsolationLevel.UNTRUSTED_CODE,
        )
        runner = SandboxRunner(backends=[_FakeBackend()])
        result = runner.run(policy)
        # The trusted probe is what authorises the execution, and it failed
        # to establish anything, so the run is refused.
        assert result.isolation_verified is False
