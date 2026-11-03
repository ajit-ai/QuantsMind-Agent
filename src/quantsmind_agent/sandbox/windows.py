"""The Windows execution boundary (M3B-S1.4).

This module implements what Windows enforces **without administrator
rights**:

* a **Job Object** with an active-process limit, per-process and per-job
  memory limits, a per-process time limit and ``KILL_ON_JOB_CLOSE``, so the
  whole process tree is contained, capped and torn down with the sandbox;
* a **controlled environment** built from an allowlist, with the credential
  variables removed and the user-profile variables pointed at a
  sandbox-local directory;
* a **controlled working directory** inside the disposable workspace;
* **capped output**, a **timeout**, and **cancellation** that terminates the
  process tree rather than a single process.

What Windows does **not** enforce here, and what this module therefore does
not claim, is confidentiality: a child running under the desktop user's
token can still read that user's files and open sockets. The
:mod:`quantsmind_agent.sandbox.probe` module *measures* that, and the
runner refuses to execute code the caller declared untrusted unless the
measurement says the boundary is real.

`pywin32` is not required: the Win32 calls are made through `ctypes`, so
the packaged application carries no new runtime dependency.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from quantsmind_agent.sandbox.appcontainer import (
    EXTENDED_STARTUPINFO_PRESENT,
    AppContainerLauncher,
)
from quantsmind_agent.sandbox.policy import SandboxError, SandboxPolicy, SandboxResult

__all__ = [
    "CREDENTIAL_ENV_NAMES",
    "EXTENDED_STARTUPINFO_PRESENT",
    "JobObject",
    "build_child_environment",
    "launch",
    "sandbox_supported",
]

IS_WINDOWS = os.name == "nt"

# -- Win32 constants ------------------------------------------------------
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100

#: ``TokenIsAppContainer`` token-information class.
TOKEN_IS_APPCONTAINER = 29
TOKEN_QUERY = 0x0008

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000020
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
STILL_ACTIVE = 259

#: The environment the child keeps. Everything else is dropped, so the
#: child cannot read a secret that happens to live in the parent's
#: environment.
ENV_ALLOWLIST = (
    "SystemRoot",
    "windir",
    "SystemDrive",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "OS",
    "TEMP",
    "TMP",
    "HOMEDRIVE",
    "HOMEPATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)

#: The environment variables that must never reach a child.
CREDENTIAL_ENV_NAMES = (
    "QM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_CLIENT_SECRET",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "NPM_TOKEN",
)


def sandbox_supported() -> bool:
    """Whether this platform has the primitives the sandbox needs."""

    return IS_WINDOWS


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounter(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounter),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _kernel32() -> Any:
    return ctypes.WinDLL("Kernel32.dll", use_last_error=True)


def _configure(kernel: Any) -> Any:
    """Give every Win32 call the right prototype.

    Without this, a handle is truncated to 32 bits by the default ``c_int``
    return type, and the call writes through a null pointer.
    """

    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.restype = ctypes.c_int
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.restype = ctypes.c_int
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.restype = ctypes.c_int
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CreatePipe.restype = ctypes.c_int
    kernel.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetHandleInformation.restype = ctypes.c_int
    kernel.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel.ReadFile.restype = ctypes.c_int
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel.CreateProcessW.restype = ctypes.c_int
    kernel.CreateProcessW.argtypes = [
        ctypes.c_void_p,  # lpApplicationName
        wintypes.LPWSTR,  # lpCommandLine
        ctypes.c_void_p,  # lpProcessAttributes
        ctypes.c_void_p,  # lpThreadAttributes
        wintypes.BOOL,  # bInheritHandles
        wintypes.DWORD,  # dwCreationFlags
        ctypes.c_void_p,  # lpEnvironment
        wintypes.LPCWSTR,  # lpCurrentDirectory
        ctypes.c_void_p,  # lpStartupInfo
        ctypes.c_void_p,  # lpProcessInformation
    ]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.TerminateProcess.restype = ctypes.c_int
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    return kernel


class JobObject:
    """A Job Object that contains a process tree.

    The job is the *lifecycle* boundary: it caps the number of processes,
    the memory and the CPU time, and closing it kills everything inside.
    It is **not** a confidentiality boundary, and the code does not pretend
    it is.
    """

    def __init__(
        self,
        *,
        active_process_limit: int = 8,
        memory_limit: int = 1_073_741_824,
        cpu_seconds: int = 120,
    ) -> None:
        if not IS_WINDOWS:
            raise SandboxError(
                "the job object boundary is only available on Windows",
                reason="unsupported_platform",
            )
        self._kernel = _configure(_kernel32())
        handle = self._kernel.CreateJobObjectW(None, None)
        if not handle:
            raise SandboxError(
                "the job object could not be created",
                reason="job_object_unavailable",
                detail=f"winerror {ctypes.get_last_error()}",
            )
        self._handle = handle
        limits = _JobExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_JOB_MEMORY
            | JOB_OBJECT_LIMIT_PROCESS_TIME
        )
        limits.BasicLimitInformation.ActiveProcessLimit = int(active_process_limit)
        limits.BasicLimitInformation.PerProcessUserTimeLimit = int(cpu_seconds * 10_000_000)
        limits.ProcessMemoryLimit = int(memory_limit)
        limits.JobMemoryLimit = int(memory_limit)
        ok = self._kernel.SetInformationJobObject(
            self._handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not ok:
            error = ctypes.get_last_error()
            self.close()
            raise SandboxError(
                "the job object limits could not be applied",
                reason="job_object_limits_failed",
                detail=f"winerror {error}",
            )

    @property
    def handle(self) -> Any:
        return self._handle

    def assign(self, process_handle: Any) -> None:
        """Put a process into the job, before it is resumed."""

        if not self._kernel.AssignProcessToJobObject(self._handle, process_handle):
            raise SandboxError(
                "the process could not be assigned to the job",
                reason="job_assignment_failed",
                detail=f"winerror {ctypes.get_last_error()}",
            )

    def terminate(self, exit_code: int = 1) -> None:
        """Kill every process in the job."""

        if self._handle:
            self._kernel.TerminateJobObject(self._handle, exit_code)

    def close(self) -> None:
        """Close the job, which kills anything still inside it."""

        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> JobObject:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def build_child_environment(
    policy: SandboxPolicy,
    sandbox_home: Path,
) -> dict[str, str]:
    """The environment the child receives.

    The allowlist is the base: a variable the parent happens to have is not
    inherited unless it is on the list. The credential variables are removed
    even if they were on the list, the user-profile variables are pointed at
    the sandbox's own directory, and the policy's injections are applied
    last.
    """

    stripped = set(CREDENTIAL_ENV_NAMES) | set(policy.stripped_environment)
    env: dict[str, str] = {}
    for name in ENV_ALLOWLIST:
        if name in stripped:
            continue
        value = os.environ.get(name)
        if value:
            env[name] = value
    home = str(sandbox_home)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["APPDATA"] = str(Path(home) / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(Path(home) / "AppData" / "Local")
    env["TEMP"] = str(Path(home) / "tmp")
    env["TMP"] = str(Path(home) / "tmp")
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in stripped:
        env.pop(name, None)
    env.update(policy.injected_environment)
    return env


def _environment_block(env: dict[str, str]) -> str:
    """A UTF-16 environment block, as ``CreateProcessW`` expects."""

    return "".join(f"{name}={value}\0" for name, value in env.items()) + "\0"


def _read_capped(
    handle: Any,
    limit: int,
    sink: list[bytes],
    truncated: list[bool],
) -> None:
    """Drain a pipe, stopping at the cap so a child cannot flood memory."""

    kernel = _configure(_kernel32())
    buffer = 4096
    read = wintypes.DWORD()
    while True:
        chunk = ctypes.create_string_buffer(buffer)
        ok = kernel.ReadFile(handle, chunk, buffer, ctypes.byref(read), None)
        if not ok or read.value == 0:
            break
        data = chunk.raw[: read.value]
        if sum(len(item) for item in sink) + len(data) > limit:
            sink.append(data[: max(0, limit - sum(len(item) for item in sink))])
            truncated[0] = True
            break
        sink.append(data)


def _is_appcontainer_process(process_handle: Any) -> bool:
    """Whether the child's token carries the AppContainer flag.

    Checked while the process is still suspended, so an unverified child
    can be terminated before it ever runs.
    """

    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi.OpenProcessToken.restype = ctypes.c_int
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.restype = ctypes.c_int
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
        return False
    try:
        value = wintypes.DWORD()
        size = wintypes.DWORD()
        if not advapi.GetTokenInformation(
            token,
            TOKEN_IS_APPCONTAINER,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(size),
        ):
            return False
        return bool(value.value)
    finally:
        kernel = _configure(_kernel32())
        kernel.CloseHandle(token)


def launch(
    policy: SandboxPolicy,
    *,
    cancel_event: Any = None,
    sandbox_home: Path | None = None,
    use_appcontainer: bool = False,
) -> SandboxResult:
    """Run the policy's command inside the Windows boundary.

    The command is fixed by the policy, the environment is controlled, the
    working directory is the workspace, the output is capped, and the whole
    process tree is inside a job object that is torn down when the sandbox
    closes. When ``use_appcontainer`` is set, the child is launched with an
    empty AppContainer security-capabilities attribute and its effective
    AppContainer identity is verified while suspended before any job object
    assignment or thread resume.
    """

    if not IS_WINDOWS:
        raise SandboxError(
            "the sandbox is only available on Windows",
            reason="unsupported_platform",
        )

    workspace = Path(policy.workspace_root).resolve()
    if not workspace.is_dir():
        raise SandboxError(
            "the sandbox workspace does not exist",
            reason="workspace_missing",
        )

    # -- AppContainer setup -------------------------------------------------
    launcher: Any = None
    attribute_list: Any = None
    sid: Any = None
    extra_flags = 0
    if use_appcontainer:
        launcher = AppContainerLauncher()
        sid = launcher.app_container_sid()
        if sid is None:
            raise SandboxError(
                "the AppContainer profile is not registered on this host",
                reason="appcontainer_profile_missing",
                detail=(
                    "the owner activation step has not been performed; the launch "
                    "is refused rather than run outside the boundary"
                ),
            )
        try:
            attribute_list, _capabilities = launcher.attribute_list(sid)
        except Exception as exc:
            raise SandboxError(
                "the AppContainer launch attributes could not be prepared",
                reason="appcontainer_attributes_failed",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        if attribute_list is None:
            raise SandboxError(
                "the AppContainer launch attributes could not be prepared",
                reason="appcontainer_attributes_failed",
                detail=f"winerror {ctypes.get_last_error()}",
            )
        try:
            startup = launcher.startup_info_ex(attribute_list)
        except Exception as exc:
            launcher.delete_attribute_list(attribute_list)
            launcher.free_sid(sid)
            raise SandboxError(
                "the AppContainer startup info could not be prepared",
                reason="appcontainer_attributes_failed",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        extra_flags = EXTENDED_STARTUPINFO_PRESENT

    home = Path(sandbox_home) if sandbox_home else workspace / ".sandbox"
    home.mkdir(parents=True, exist_ok=True)
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    (home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
    (home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)

    env = build_child_environment(policy, home)
    kernel = _configure(_kernel32())

    stdout_read = wintypes.HANDLE()
    stdout_write = wintypes.HANDLE()
    stderr_read = wintypes.HANDLE()
    stderr_write = wintypes.HANDLE()
    if not kernel.CreatePipe(
        ctypes.byref(stdout_read), ctypes.byref(stdout_write), None, 0
    ) or not kernel.CreatePipe(ctypes.byref(stderr_read), ctypes.byref(stderr_write), None, 0):
        error = ctypes.get_last_error()
        raise SandboxError(
            "the sandbox output pipes could not be created",
            reason="pipe_creation_failed",
            detail=f"winerror {error}",
        )
    for handle in (stdout_read, stderr_read):
        kernel.SetHandleInformation(handle, 0x00000001, 0x00000000)  # not inheritable
    for handle in (stdout_write, stderr_write):
        kernel.SetHandleInformation(handle, 0x00000001, 0x00000001)  # inheritable

    security = _SecurityAttributes()
    security.nLength = ctypes.sizeof(security)
    security.bInheritHandle = True

    if use_appcontainer:
        startup.StartupInfo.hStdInput = None
        startup.StartupInfo.hStdOutput = stdout_write
        startup.StartupInfo.hStdError = stderr_write
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    else:
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.hStdInput = None
        startup.hStdOutput = stdout_write
        startup.hStdError = stderr_write
        startup.dwFlags = STARTF_USESTDHANDLES

    process_info = _ProcessInformation()
    command_line = " ".join(f'"{part}"' if " " in part else part for part in policy.command)
    command_buffer = ctypes.create_unicode_buffer(command_line)
    environment_buffer = ctypes.create_unicode_buffer(_environment_block(env))
    try:
        created = kernel.CreateProcessW(
            None,
            ctypes.cast(command_buffer, wintypes.LPWSTR),
            None,
            None,
            True,
            CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | extra_flags,
            ctypes.cast(environment_buffer, ctypes.c_void_p),
            str(workspace),
            ctypes.byref(startup),
            ctypes.byref(process_info),
        )
    finally:
        kernel.CloseHandle(stdout_write)
        kernel.CloseHandle(stderr_write)
        if use_appcontainer and launcher is not None:
            if attribute_list is not None:
                launcher.delete_attribute_list(attribute_list)
                attribute_list = None
            if sid is not None:
                launcher.free_sid(sid)
                sid = None
    if not created:
        error = ctypes.get_last_error()
        kernel.CloseHandle(stdout_read)
        kernel.CloseHandle(stderr_read)
        raise SandboxError(
            "the sandboxed process could not be created",
            reason="process_creation_failed",
            detail=f"winerror {error}",
        )

    # Verify the child's effective AppContainer identity while suspended, and
    # keep every resource owned here until it is either transferred to the job
    # object or deterministically torn down below.
    job: JobObject | None = None
    assigned = False
    readers: list[threading.Thread] = []
    stdout_sink: list[bytes] = []
    stderr_sink: list[bytes] = []
    truncated = [False]
    started = time.perf_counter()
    try:
        if use_appcontainer and not _is_appcontainer_process(process_info.hProcess):
            raise SandboxError(
                "the created process is not running in the AppContainer",
                reason="appcontainer_token_unverified",
                detail="the launch is refused rather than run outside the boundary",
            )

        job = JobObject(
            active_process_limit=policy.max_child_processes,
            memory_limit=policy.max_memory_bytes,
            cpu_seconds=int(policy.timeout_seconds) + 5,
        )
        job.assign(process_info.hProcess)
        assigned = True
        readers = [
            threading.Thread(
                target=_read_capped,
                args=(stdout_read, policy.max_output_bytes, stdout_sink, truncated),
                daemon=True,
            ),
            threading.Thread(
                target=_read_capped,
                args=(stderr_read, policy.max_output_bytes, stderr_sink, truncated),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        if kernel.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
            raise SandboxError(
                "the suspended sandboxed process could not be resumed",
                reason="thread_resume_failed",
                detail=f"winerror {ctypes.get_last_error()}",
            )

        deadline = started + policy.timeout_seconds
        cancelled = False
        timed_out = False
        while True:
            waited = kernel.WaitForSingleObject(process_info.hProcess, 100)
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                cancelled = True
                job.terminate()
                break
            if waited == WAIT_TIMEOUT:
                if time.perf_counter() >= deadline:
                    timed_out = True
                    job.terminate()
                    break
                continue
            break

        kernel.WaitForSingleObject(process_info.hProcess, 2000)
        exit_code: int | None = None
        code = wintypes.DWORD()
        if kernel.GetExitCodeProcess(process_info.hProcess, ctypes.byref(code)):
            exit_code = int(code.value)
        for reader in readers:
            reader.join(timeout=1.0)
        return SandboxResult(
            exit_code=exit_code,
            stdout=b"".join(stdout_sink).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_sink).decode("utf-8", errors="replace"),
            timed_out=timed_out,
            cancelled=cancelled,
            isolation_verified=not use_appcontainer,
            established_level="appcontainer" if use_appcontainer else "lifecycle",
            duration_seconds=time.perf_counter() - started,
            truncated=truncated[0],
        )
    finally:
        # The child is still suspended until ResumeThread runs. If it never
        # entered the job object, this frame is its sole owner: terminate that
        # exact process before its handles are closed. Once assigned, the job
        # object owns the process tree and tear-down goes through the job.
        if not assigned:
            kernel.TerminateProcess(process_info.hProcess, 1)
        if job is not None:
            job.terminate()
            job.close()
        for reader in readers:
            reader.join(timeout=1.0)
        kernel.CloseHandle(process_info.hThread)
        kernel.CloseHandle(process_info.hProcess)
        kernel.CloseHandle(stdout_read)
        kernel.CloseHandle(stderr_read)
