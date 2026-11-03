"""M3B-S1.4A AppContainer launch-path tests.

These are **mocked** launch-path tests: they prove the wiring, creation
flags, refusal paths and cleanup sequence. They do not prove OS-enforced
isolation; that requires an activated AppContainer profile and is
host-blocked on this machine.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

import pytest

# ``ctypes.wintypes`` and the sandbox modules that use it raise on
# non-Windows. Decide the platform before importing them so collection
# succeeds everywhere and this module is reported as skipped.
if sys.platform != "win32":
    pytest.skip("the Windows isolation primitives are Windows-only", allow_module_level=True)

from ctypes import wintypes

from quantsmind_agent.sandbox import SandboxError, SandboxPolicy, windows
from quantsmind_agent.sandbox.appcontainer import (
    _STARTUPINFOEX,
    AppContainerCapability,
    AppContainerLauncher,
    detect_appcontainer_capability,
)
from quantsmind_agent.sandbox.backends import AppContainerBackend

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


class _FakeApi:
    def __init__(self, handler: Any) -> None:
        self.restype: Any = None
        self.argtypes: Any = None
        self._handler = handler

    def __call__(self, *args: Any) -> Any:
        return self._handler(*args)


class _FakeKernel:
    """A Kernel32 stub that records the launch sequence without running anything."""

    def __init__(
        self,
        *,
        created: bool = True,
        assigned: bool = True,
        job_create: bool = True,
        limits_ok: bool = True,
        resume_ok: bool = True,
        wait_raises: bool = False,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed: list[Any] = []
        self.created = created
        self.assigned = assigned
        self.job_create = job_create
        self.limits_ok = limits_ok
        self.resume_ok = resume_ok
        self.wait_raises = wait_raises
        self.creation_flags: int | None = None
        self.startup_info: Any = None
        self._pipe_count = 0
        self._apis: dict[str, _FakeApi] = {
            "ReadFile": _FakeApi(lambda *a: self._invoke("ReadFile", a))
        }

    def __getattr__(self, name: str) -> _FakeApi:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._apis:
            self._apis[name] = _FakeApi(lambda *args, _name=name: self._invoke(_name, args))
        return self._apis[name]

    @staticmethod
    def _set_handle(pointer: Any, value: int) -> None:
        pointer._obj.value = value

    def _invoke(self, name: str, args: tuple[Any, ...]) -> int:
        self.calls.append((name, args))
        if name == "CreatePipe":
            base = 100 + 2 * self._pipe_count
            self._pipe_count += 1
            self._set_handle(args[0], base)
            self._set_handle(args[1], base + 1)
            return 1
        if name == "SetHandleInformation":
            return 1
        if name == "CreateProcessW":
            self.creation_flags = int(args[5])
            self.startup_info = args[8]._obj
            if not self.created:
                return 0
            info = args[9]._obj
            info.hProcess = 1
            info.hThread = 2
            return 1
        if name == "CloseHandle":
            handle = args[0]
            self.closed.append(getattr(handle, "value", handle))
            return 1
        if name == "CreateJobObjectW":
            return 500 if self.job_create else 0
        if name == "SetInformationJobObject":
            return 1 if self.limits_ok else 0
        if name == "TerminateJobObject":
            return 1
        if name == "AssignProcessToJobObject":
            return 1 if self.assigned else 0
        if name == "GetExitCodeProcess":
            args[1]._obj.value = 0
            return 1
        if name == "ResumeThread":
            return 1 if self.resume_ok else 0xFFFFFFFF
        if name == "WaitForSingleObject":
            if self.wait_raises:
                raise RuntimeError("wait failed unexpectedly")
            return 0
        if name == "ReadFile":
            return 0
        return 1


class _FakeLauncher:
    def __init__(self, *, sid: Any = 1234, attributes: bool = True) -> None:
        self._sid = sid
        self._attributes = attributes
        self.deleted = 0
        self.freed = 0
        self.attribute_buffer: Any = None

    def app_container_sid(self) -> Any:
        return self._sid

    def attribute_list(self, sid: Any) -> tuple[Any, Any]:
        if not self._attributes:
            return None, None
        self.attribute_buffer = ctypes.create_string_buffer(64)
        return self.attribute_buffer, None

    def startup_info_ex(self, attribute_list: Any) -> _STARTUPINFOEX:
        info = _STARTUPINFOEX()
        info.StartupInfo.cb = ctypes.sizeof(info)
        info.lpAttributeList = ctypes.cast(attribute_list, ctypes.c_void_p)
        return info

    def delete_attribute_list(self, buffer: Any) -> None:
        self.deleted += 1

    def free_sid(self, sid: Any) -> None:
        self.freed += 1


class _FailingThread:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def start(self) -> None:
        raise RuntimeError("reader thread could not start")

    def join(self, timeout: float | None = None) -> None:
        return None


class _ThreadShim:
    Thread = _FailingThread


def _assert_handles_closed(kernel: _FakeKernel) -> None:
    for handle in (1, 2, 100, 101, 102, 103):
        assert handle in kernel.closed, f"handle {handle} was not closed"


def _well_known_appcontainer_sid() -> Any:
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi.ConvertStringSidToSidW.restype = ctypes.c_int
    advapi.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    sid = wintypes.LPVOID()
    assert advapi.ConvertStringSidToSidW("S-1-15-2-1", ctypes.byref(sid))
    return sid


class TestAppContainerLaunchConstruction:
    """S14A-G6, G7, G11: mocked launch construction and refusal tests."""

    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        kernel: _FakeKernel,
        launcher: _FakeLauncher,
        *,
        token: bool = True,
    ) -> None:
        monkeypatch.setattr(windows, "_kernel32", lambda: kernel)
        monkeypatch.setattr(windows, "AppContainerLauncher", lambda: launcher)
        monkeypatch.setattr(windows, "_is_appcontainer_process", lambda handle: token)

    def test_the_startup_info_accepts_the_attribute_buffer(self) -> None:
        buffer = ctypes.create_string_buffer(64)
        info = AppContainerLauncher().startup_info_ex(buffer)
        assert info.lpAttributeList is not None
        assert info.StartupInfo.cb == ctypes.sizeof(info)

    def test_the_attribute_list_accepts_a_valid_sid(self) -> None:
        launcher = AppContainerLauncher()
        sid = _well_known_appcontainer_sid()
        try:
            buffer, capabilities = launcher.attribute_list(sid)
            assert buffer is not None
            assert capabilities.CapabilityCount == 0
            launcher.delete_attribute_list(buffer)
        finally:
            kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
            kernel.LocalFree.argtypes = [wintypes.HLOCAL]
            kernel.LocalFree.restype = wintypes.HLOCAL
            kernel.LocalFree(sid)

    def test_a_rejected_attribute_update_returns_no_list(self) -> None:
        launcher = AppContainerLauncher()
        calls = {"init": 0}

        class _RejectingKernel:
            def InitializeProcThreadAttributeList(
                self, buffer: Any, count: int, flags: int, size: Any
            ) -> int:
                calls["init"] += 1
                if calls["init"] == 1:
                    size._obj.value = 48
                    return 0
                return 1

            def UpdateProcThreadAttribute(self, *args: Any) -> int:
                return 0

            def DeleteProcThreadAttributeList(self, buffer: Any) -> None:
                return None

        launcher._kernel = _RejectingKernel()  # type: ignore[assignment]
        buffer, _capabilities = launcher.attribute_list(ctypes.c_void_p(1234))
        assert buffer is None

    def test_the_creation_flags_carry_the_extended_startup_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel()
        launcher = _FakeLauncher()
        self._patch(monkeypatch, kernel, launcher)
        result = windows.launch(
            _policy(tmp_path, (sys.executable, "-c", "pass")), use_appcontainer=True
        )
        assert result.established_level == "appcontainer"
        assert result.isolation_verified is False
        assert kernel.creation_flags is not None
        assert kernel.creation_flags & windows.EXTENDED_STARTUPINFO_PRESENT
        assert kernel.creation_flags & windows.CREATE_SUSPENDED
        assert isinstance(kernel.startup_info, _STARTUPINFOEX)
        assert kernel.startup_info.lpAttributeList is not None
        assert launcher.deleted == 1
        assert launcher.freed == 1

    def test_a_missing_profile_is_refused_without_a_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel()
        self._patch(monkeypatch, kernel, _FakeLauncher(sid=None))
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(SandboxError) as caught:
            windows.launch(policy, use_appcontainer=True)
        assert caught.value.reason == "appcontainer_profile_missing"
        assert not any(name == "CreateProcessW" for name, _ in kernel.calls)

    def test_a_failed_attribute_list_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel()
        self._patch(monkeypatch, kernel, _FakeLauncher(attributes=False))
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(SandboxError) as caught:
            windows.launch(policy, use_appcontainer=True)
        assert caught.value.reason == "appcontainer_attributes_failed"
        assert not any(name == "CreateProcessW" for name, _ in kernel.calls)

    def test_a_failed_process_creation_is_reported_and_cleaned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel(created=False)
        launcher = _FakeLauncher()
        self._patch(monkeypatch, kernel, launcher)
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(SandboxError) as caught:
            windows.launch(policy, use_appcontainer=True)
        assert caught.value.reason == "process_creation_failed"
        assert launcher.deleted == 1
        assert launcher.freed == 1
        assert 101 in kernel.closed and 103 in kernel.closed
        assert 100 in kernel.closed and 102 in kernel.closed
        assert sum(1 for name, _ in kernel.calls if name == "CreateProcessW") == 1

    def test_an_unverified_token_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel()
        self._patch(monkeypatch, kernel, _FakeLauncher(), token=False)
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(SandboxError) as caught:
            windows.launch(policy, use_appcontainer=True)
        assert caught.value.reason == "appcontainer_token_unverified"
        assert any(name == "TerminateProcess" for name, _ in kernel.calls)
        assert not any(name == "ResumeThread" for name, _ in kernel.calls)
        _assert_handles_closed(kernel)

    def test_a_failed_job_assignment_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel(assigned=False)
        self._patch(monkeypatch, kernel, _FakeLauncher())
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(SandboxError) as caught:
            windows.launch(policy, use_appcontainer=True)
        assert caught.value.reason == "job_assignment_failed"
        assert any(name == "TerminateProcess" for name, _ in kernel.calls)
        assert not any(name == "ResumeThread" for name, _ in kernel.calls)
        assert 500 in kernel.closed
        _assert_handles_closed(kernel)

    def test_a_failed_job_object_creation_terminates_the_suspended_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel(job_create=False)
        self._patch(monkeypatch, kernel, _FakeLauncher())
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(SandboxError) as caught:
            windows.launch(policy, use_appcontainer=True)
        assert caught.value.reason == "job_object_unavailable"
        assert any(name == "TerminateProcess" for name, _ in kernel.calls)
        assert not any(name == "TerminateJobObject" for name, _ in kernel.calls)
        assert not any(name == "ResumeThread" for name, _ in kernel.calls)
        _assert_handles_closed(kernel)

    def test_failed_job_limits_terminate_the_suspended_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel(limits_ok=False)
        self._patch(monkeypatch, kernel, _FakeLauncher())
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(SandboxError) as caught:
            windows.launch(policy, use_appcontainer=True)
        assert caught.value.reason == "job_object_limits_failed"
        assert any(name == "TerminateProcess" for name, _ in kernel.calls)
        assert not any(name == "ResumeThread" for name, _ in kernel.calls)
        assert 500 in kernel.closed
        _assert_handles_closed(kernel)

    def test_a_failed_reader_start_terminates_the_job_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel()
        self._patch(monkeypatch, kernel, _FakeLauncher())
        monkeypatch.setattr(windows, "threading", _ThreadShim())
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(RuntimeError, match="reader"):
            windows.launch(policy, use_appcontainer=True)
        assert any(name == "TerminateJobObject" for name, _ in kernel.calls)
        assert not any(name == "ResumeThread" for name, _ in kernel.calls)
        assert 500 in kernel.closed
        _assert_handles_closed(kernel)

    def test_a_failed_resume_terminates_the_job_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel(resume_ok=False)
        self._patch(monkeypatch, kernel, _FakeLauncher())
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(SandboxError) as caught:
            windows.launch(policy, use_appcontainer=True)
        assert caught.value.reason == "thread_resume_failed"
        assert any(name == "TerminateJobObject" for name, _ in kernel.calls)
        assert 500 in kernel.closed
        _assert_handles_closed(kernel)

    def test_an_unexpected_wait_failure_cleans_up_the_job_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel(wait_raises=True)
        self._patch(monkeypatch, kernel, _FakeLauncher())
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(RuntimeError, match="wait failed"):
            windows.launch(policy, use_appcontainer=True)
        assert any(name == "TerminateJobObject" for name, _ in kernel.calls)
        assert 500 in kernel.closed
        _assert_handles_closed(kernel)

    def test_the_backend_dispatches_to_the_appcontainer_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeKernel()
        self._patch(monkeypatch, kernel, _FakeLauncher())
        backend = AppContainerBackend(
            AppContainerCapability(available=True, reason="test", token_privileges={})
        )
        result = backend.launch(_policy(tmp_path, (sys.executable, "-c", "pass")))
        assert result.established_level == "appcontainer"
        assert result.isolation_verified is False


class TestHostBlocked:
    """Negative safety evidence on a host without an activated profile."""

    def test_the_host_refuses_an_appcontainer_launch_without_a_profile(
        self, tmp_path: Path
    ) -> None:
        if detect_appcontainer_capability().available:
            pytest.skip("this host has an activated AppContainer profile")
        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        with pytest.raises(SandboxError) as caught:
            windows.launch(policy, use_appcontainer=True)
        assert caught.value.reason == "appcontainer_profile_missing"
