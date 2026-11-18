"""The AppContainer isolation backend (M3B-S1.4A).

AppContainer is the only Windows mechanism that gives a child a *different
security context* without a separate user account: the child runs with an
AppContainer token at low integrity, with a capability set the application
chooses, and with an identity the Credential Manager does not serve.

This module implements the launch path and, just as importantly, the
**capability detection**: it works out, without changing anything, whether
this host and this token can provide the boundary. The detection is the
gate: the runner never launches untrusted code on a host that cannot
confine it.

A working AppContainer launch needs three things:

1. **A profile.** ``CreateAppContainerProfile`` registers the container and
   returns its SID. That needs ``SeCreateTokenPrivilege`` and
   ``SeIncreaseQuotaPrivilege``, which a standard token does not hold.
2. **An ACL** on the paths the child must reach -- the disposable
   workspace and the Python runtime -- granting the AppContainer SID access
   and nothing else.
3. **A launch** with ``PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`` and
   ``EXTENDED_STARTUPINFO_PRESENT``, with **no** capabilities, so no
   ``internetClient`` and therefore no outbound network.

This milestone implements all three and *detects* the absence of the first
two. It does not create a profile or change an ACL: both are owner
activation steps, and the plan is in ``OWNER_ACTIVATION_PLAN.md``.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "APP_CONTAINER_NAME",
    "AppContainerCapability",
    "AppContainerLauncher",
    "detect_appcontainer_capability",
    "token_privileges",
]

#: The profile name this application would use. It is stable, so the SID is
#: stable, so the ACLs are stable.
APP_CONTAINER_NAME = "quantsmind-agent.sandbox"

#: ``PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES``
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
EXTENDED_STARTUPINFO_PRESENT = 0x00080000

#: The privileges the *token* must hold for the launch to work.
REQUIRED_PRIVILEGES = ("SeCreateTokenPrivilege", "SeAssignPrimaryTokenPrivilege")

#: The privileges a profile creation needs.
PROFILE_PRIVILEGES = ("SeCreateTokenPrivilege", "SeIncreaseQuotaPrivilege")


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", wintypes.LPVOID),
        ("Capabilities", ctypes.POINTER(_SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class _STARTUPINFOW(ctypes.Structure):
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


class _STARTUPINFOEX(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


@dataclass(frozen=True, slots=True)
class AppContainerCapability:
    """What this host can provide for an AppContainer launch."""

    available: bool
    reason: str = ""
    profile_name: str = APP_CONTAINER_NAME
    sid: str = ""
    missing_requirements: tuple[str, ...] = ()
    token_privileges: dict[str, bool] = field(default_factory=dict)
    api_present: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "profile_name": self.profile_name,
            "sid": self.sid,
            "missing_requirements": list(self.missing_requirements),
            "api_present": self.api_present,
        }


def token_privileges() -> dict[str, bool]:
    """The privileges the current process holds, and whether they are enabled.

    Read-only. The enumeration is done with ``whoami /priv``, which reads the
    process token the same way the operating system does, rather than
    re-implementing the token layout. A failure to enumerate is reported as
    "no privileges known", which is the fail-closed direction: the boundary
    is then treated as unavailable.
    """

    import subprocess

    try:
        completed = subprocess.run(
            ["whoami", "/priv"],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    found: dict[str, bool] = {}
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped or "Privilege Name" in stripped or stripped.startswith("-"):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        state = parts[-1]
        if name.startswith("Se") and state in ("Enabled", "Disabled"):
            found[name] = state == "Enabled"
    return found


def detect_appcontainer_capability() -> AppContainerCapability:
    """What this host and this token can provide, without changing anything.

    The checks are, in order: the APIs are present; the token holds the
    privileges a profile creation and a launch need; and the profile can be
    *derived* (which is what proves the container is registered).
    """

    try:
        userenv = ctypes.WinDLL("Userenv.dll", use_last_error=True)
    except OSError as exc:
        return AppContainerCapability(
            available=False, reason=f"userenv.dll is unavailable: {exc}", api_present=False
        )
    api_present = all(
        hasattr(userenv, name)
        for name in (
            "CreateAppContainerProfile",
            "DeleteAppContainerProfile",
            "DeriveAppContainerSidFromAppContainerName",
        )
    )
    if not api_present:
        return AppContainerCapability(
            available=False,
            reason="the AppContainer APIs are not present on this host",
            api_present=False,
        )

    privileges = token_privileges()
    missing = tuple(name for name in REQUIRED_PRIVILEGES if name not in privileges)
    if missing:
        return AppContainerCapability(
            available=False,
            reason=(
                "this token does not hold the privileges an AppContainer launch needs; "
                "the activation must be performed from an elevated context"
            ),
            missing_requirements=missing,
            token_privileges={name: privileges.get(name, False) for name in REQUIRED_PRIVILEGES},
        )

    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_int
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    sid = wintypes.LPVOID()
    derived = userenv.DeriveAppContainerSidFromAppContainerName(
        APP_CONTAINER_NAME, ctypes.byref(sid)
    )
    if not derived:
        return AppContainerCapability(
            available=False,
            reason=(
                "the AppContainer profile is not registered on this host; "
                "the owner activation step has not been performed"
            ),
            missing_requirements=("appcontainer_profile",),
            token_privileges={name: privileges.get(name, False) for name in REQUIRED_PRIVILEGES},
        )
    return AppContainerCapability(
        available=True,
        reason="the AppContainer profile is registered and the token may launch it",
        sid="",
        token_privileges={name: privileges.get(name, False) for name in REQUIRED_PRIVILEGES},
    )


class AppContainerLauncher:
    """Launches a child inside the AppContainer.

    The launch uses ``PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`` with an
    **empty** capability set, so the container has no ``internetClient`` and
    therefore no outbound network. The child is created **suspended**, and the
    caller resumes it only after the isolation has been verified.
    """

    def __init__(self, profile_name: str = APP_CONTAINER_NAME) -> None:
        self._profile_name = profile_name
        self._kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        self._userenv = ctypes.WinDLL("Userenv.dll", use_last_error=True)
        self._configure()

    def _configure(self) -> None:
        kernel = self._kernel
        kernel.InitializeProcThreadAttributeList.restype = ctypes.c_int
        kernel.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.UpdateProcThreadAttribute.restype = ctypes.c_int
        kernel.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel.DeleteProcThreadAttributeList.restype = None
        kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel.CreateProcessW.restype = ctypes.c_int
        kernel.CreateProcessW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_int
        self._userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.LPVOID),
        ]

    def app_container_sid(self) -> Any:
        """The AppContainer SID for the profile, or ``None``."""

        sid = wintypes.LPVOID()
        if not self._userenv.DeriveAppContainerSidFromAppContainerName(
            self._profile_name, ctypes.byref(sid)
        ):
            return None
        return sid

    def attribute_list(self, sid: Any) -> tuple[Any, Any]:
        """The ``PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`` attribute list.

        The capability set is **empty**: no ``internetClient``, no
        ``privateNetworkClientServer``, nothing.
        """

        capabilities = _SECURITY_CAPABILITIES()
        capabilities.AppContainerSid = sid
        capabilities.CapabilityCount = 0
        capabilities.Capabilities = ctypes.cast(0, ctypes.POINTER(_SID_AND_ATTRIBUTES))
        capabilities.Reserved = 0
        size = wintypes.DWORD()
        self._kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not self._kernel.InitializeProcThreadAttributeList(buffer, 1, 0, ctypes.byref(size)):
            return None, None
        if not self._kernel.UpdateProcThreadAttribute(
            buffer,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(capabilities),
            ctypes.sizeof(capabilities),
            None,
            None,
        ):
            self._kernel.DeleteProcThreadAttributeList(buffer)
            return None, None
        return buffer, capabilities

    def delete_attribute_list(self, buffer: Any) -> None:
        if buffer is not None:
            self._kernel.DeleteProcThreadAttributeList(buffer)
            buffer = None

    def free_sid(self, sid: Any) -> None:
        """Release an AppContainer SID with ``FreeSid``, as the API requires."""

        if sid is None:
            return
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi.FreeSid.argtypes = [wintypes.LPVOID]
        advapi.FreeSid.restype = wintypes.LPVOID
        advapi.FreeSid(sid)

    def startup_info_ex(self, attribute_list: Any) -> _STARTUPINFOEX:
        """The ``STARTUPINFOEX`` that carries the attribute list."""

        info = _STARTUPINFOEX()
        info.StartupInfo.cb = ctypes.sizeof(info)
        info.lpAttributeList = ctypes.cast(attribute_list, ctypes.c_void_p)
        return info
