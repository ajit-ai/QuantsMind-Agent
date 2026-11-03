"""M3B-S1.4A activation preflight/verification tests (read-only)."""

from __future__ import annotations

import ctypes
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

from quantsmind_agent.sandbox import activate
from quantsmind_agent.sandbox.activate import (
    create_profile,
    derived_sid,
    preflight,
    verify_activation,
)
from quantsmind_agent.sandbox.activate import (
    main as activate_main,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the Windows isolation primitives are Windows-only"
)


class TestActivationPreflight:
    def test_the_preflight_is_read_only_and_idempotent(self, tmp_path: Path) -> None:
        first = preflight(tmp_path).to_dict()
        second = preflight(tmp_path).to_dict()
        assert first == second

    def test_the_preflight_names_the_required_checks(self, tmp_path: Path) -> None:
        names = {check.name for check in preflight(tmp_path).checks}
        assert {
            "windows_supported",
            "appcontainer_apis",
            "profile_registered",
            "elevated_context",
            "workspace_exists",
            "workspace_writable",
            "runtime_readable",
        } <= names

    def test_an_unregistered_profile_is_reported(self, tmp_path: Path) -> None:
        result = preflight(tmp_path)
        if derived_sid() is None:
            assert "profile_registered" in result.missing()
            assert result.ready is False
        else:
            assert "profile_registered" not in result.missing()

    def test_a_missing_workspace_is_reported(self) -> None:
        result = preflight(None)
        assert "workspace_supplied" in result.missing()

    def test_verify_returns_nonzero_until_activation_is_complete(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        if verify_activation(tmp_path).ready:
            pytest.skip("this host is already activated")
        code = activate_main(["verify", "--workspace", str(tmp_path)])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ready"] is False
        assert payload["missing"]

    def test_preflight_prints_json_and_changes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = activate_main(["preflight", "--workspace", str(tmp_path)])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema"] == "quantsmind.sandbox_activation_preflight/1"
        assert payload["profile_name"] == "quantsmind-agent.sandbox"


class _FakeApi:
    def __init__(self, handler: Any) -> None:
        self.restype: Any = None
        self.argtypes: Any = None
        self._handler = handler

    def __call__(self, *args: Any) -> Any:
        return self._handler(*args)


class _CtypesShim:
    def __init__(self, dll: Any) -> None:
        self._dll = dll

    def WinDLL(self, name: str, **kwargs: Any) -> Any:
        return self._dll

    def __getattr__(self, name: str) -> Any:
        return getattr(ctypes, name)


class TestProfileCreationArguments:
    """The native prototype must match the six-argument Userenv contract."""

    def test_creation_passes_the_capability_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: dict[str, Any] = {}

        def _create(*args: Any) -> int:
            recorded["args"] = args
            return 0

        class _Userenv:
            CreateAppContainerProfile = _FakeApi(_create)
            DeriveAppContainerSidFromAppContainerName = _FakeApi(lambda *args: 1)

        monkeypatch.setattr(activate, "ctypes", _CtypesShim(_Userenv()))
        monkeypatch.setattr(activate, "_sid_text", lambda sid: "S-1-15-2-999")
        assert create_profile("qm-test") == "S-1-15-2-999"
        assert len(recorded["args"]) == 6
        assert recorded["args"][4] == 0

    def test_creation_failures_are_not_masked_by_a_derived_sid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        derived = {"called": False}

        def _derive(*args: Any) -> int:
            derived["called"] = True
            return 1

        class _Userenv:
            CreateAppContainerProfile = _FakeApi(lambda *args: 0x80070005)
            DeriveAppContainerSidFromAppContainerName = _FakeApi(_derive)

        monkeypatch.setattr(activate, "ctypes", _CtypesShim(_Userenv()))
        with pytest.raises(OSError) as caught:
            create_profile("qm-test")
        assert derived["called"] is False
        assert "could not be created" in str(caught.value)
