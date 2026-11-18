"""The owner activation for the AppContainer sandbox (M3B-S1.4A).

This module is the *only* place that changes Windows security
configuration, and it is **non-destructive by default**: with no arguments it
prints what would change and exits. Nothing is provisioned unless the
operator both asks for it and types the confirmation phrase.

What ``apply`` does, in order:

1. checks that the process is elevated, and refuses otherwise;
2. creates the AppContainer profile (once) and records its SID;
3. grants that SID **read and execute** on the Python runtime, so the child
   can start, and **modify** on the disposable workspace, and nothing else;
4. prints the exact commands it ran, so the change is auditable.

What it deliberately does not do: it never grants the container access to the
user's profile, to the credential store, to another repository, or to the
network; it never creates a Windows account; it never changes a firewall
rule; and it never touches the owner's credentials.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quantsmind_agent.sandbox.appcontainer import APP_CONTAINER_NAME

__all__ = [
    "ActivationCheck",
    "ActivationPreflight",
    "activation_plan",
    "create_profile",
    "derived_sid",
    "grant_acl",
    "is_elevated",
    "main",
    "preflight",
    "verify_activation",
]

#: The phrase the operator must type to apply the activation.
CONFIRMATION_PHRASE = "activate the quantsmind-agent appcontainer sandbox"

#: The access the container needs on the Python runtime: read and execute.
RUNTIME_ACCESS = "(OI)(CI)RX"

#: The access the container needs on the disposable workspace: modify.
WORKSPACE_ACCESS = "(OI)(CI)M"


def is_elevated() -> bool:
    """Whether this process holds an elevated token."""

    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
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

    class _Elevation(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return False
    try:
        elevation = _Elevation()
        size = wintypes.DWORD()
        ok = advapi.GetTokenInformation(
            token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(size)
        )
        return bool(ok and elevation.TokenIsElevated)
    finally:
        kernel.CloseHandle(token)


def create_profile(name: str = APP_CONTAINER_NAME) -> str:
    """Create the per-user AppContainer profile and return its SID.

    Windows documents ``E_ACCESSDENIED`` for callers without permission, but
    does not document an administrator requirement; access depends on the
    caller's profile and policy. If the profile already exists, the existing
    SID is returned and nothing is changed.
    """

    userenv = ctypes.WinDLL("Userenv.dll", use_last_error=True)
    userenv.CreateAppContainerProfile.restype = ctypes.c_int
    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_int
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    sid = wintypes.LPVOID()
    result = userenv.CreateAppContainerProfile(
        name,
        "QuantsMind-Agent Sandbox",
        "QuantsMind-Agent execution sandbox",
        None,
        0,
        ctypes.byref(sid),
    )
    if result == 0:
        return _sid_text(sid)
    # Only "the application data store already exists" is reusable; every
    # other failure (for example E_ACCESSDENIED) must be surfaced rather than
    # masked by a derived SID that has no registered profile behind it.
    if (result & 0xFFFFFFFF) != 0x800700B7:
        raise OSError(
            result & 0xFFFFFFFF,
            "the AppContainer profile could not be created",
        )
    if userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid)):
        return _sid_text(sid)
    raise OSError(
        ctypes.get_last_error(),
        "the existing AppContainer profile's SID could not be derived",
    )


def _sid_text(sid: Any) -> str:
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    text = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise OSError(ctypes.get_last_error(), "the SID could not be rendered")
    try:
        return ctypes.wstring_at(text)
    finally:
        kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        kernel.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel.LocalFree.restype = wintypes.HLOCAL
        kernel.LocalFree(text)


def derived_sid(name: str = APP_CONTAINER_NAME) -> str | None:
    """The registered profile's SID text, or ``None`` when it is absent.

    Read-only. Chain-of-trust note: Microsoft documents only ``S_OK`` and
    ``E_INVALIDARG`` for the derivation call and exposes no separate
    "profile exists" query, so this is the best available read-only check.
    On this host the call returns failure while the profile is absent.
    """

    userenv = ctypes.WinDLL("Userenv.dll", use_last_error=True)
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_int
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    sid = wintypes.LPVOID()
    if not userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid)):
        return None
    try:
        return _sid_text(sid)
    finally:
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi.FreeSid.argtypes = [wintypes.LPVOID]
        advapi.FreeSid.restype = wintypes.LPVOID
        advapi.FreeSid(sid)


@dataclass(frozen=True, slots=True)
class ActivationCheck:
    """One read-only activation precondition."""

    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ActivationPreflight:
    """The read-only activation preflight result."""

    checks: tuple[ActivationCheck, ...]
    profile_name: str = APP_CONTAINER_NAME
    sid: str = ""
    workspace: str = ""
    elevated: bool = False

    @property
    def ready(self) -> bool:
        return all(check.ok for check in self.checks)

    def missing(self) -> list[str]:
        return [check.name for check in self.checks if not check.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "quantsmind.sandbox_activation_preflight/1",
            "profile_name": self.profile_name,
            "sid": self.sid,
            "workspace": self.workspace,
            "elevated": self.elevated,
            "ready": self.ready,
            "missing": self.missing(),
            "checks": [check.to_dict() for check in self.checks],
        }


def preflight(workspace: Path | None = None) -> ActivationPreflight:
    """Check every activation precondition without changing anything."""

    supported = False
    detail = "unknown platform"
    try:
        version = sys.getwindowsversion()
        supported = os.name == "nt" and version.major >= 10
        detail = f"{os.name} {version.major}.{version.minor} build {version.build}"
    except (AttributeError, OSError) as exc:
        detail = f"{type(exc).__name__}"
    checks = [ActivationCheck("windows_supported", supported, detail)]

    apis = False
    try:
        userenv = ctypes.WinDLL("Userenv.dll", use_last_error=True)
        apis = all(
            hasattr(userenv, name)
            for name in (
                "CreateAppContainerProfile",
                "DeleteAppContainerProfile",
                "DeriveAppContainerSidFromAppContainerName",
            )
        )
    except OSError as exc:
        detail = str(exc)
    else:
        detail = "present" if apis else "missing"
    checks.append(ActivationCheck("appcontainer_apis", apis, detail))

    try:
        sid = derived_sid()
    except OSError as exc:
        sid = None
        detail = f"derivation failed: {exc}"
    else:
        detail = sid or "not registered"
    checks.append(ActivationCheck("profile_registered", sid is not None, detail))

    elevated = is_elevated()
    checks.append(
        ActivationCheck(
            "elevated_context",
            True,
            "elevated"
            if elevated
            else "standard user (profile creation is per-user; ACLs may require ownership)",
        )
    )

    if workspace is None:
        checks.append(ActivationCheck("workspace_supplied", False, "--workspace is required"))
    else:
        resolved = Path(workspace).resolve()
        checks.append(ActivationCheck("workspace_exists", resolved.is_dir(), str(resolved)))
        checks.append(
            ActivationCheck(
                "workspace_writable",
                resolved.is_dir() and os.access(resolved, os.W_OK),
                str(resolved),
            )
        )
    runtime = Path(sys.executable).resolve().parent
    checks.append(ActivationCheck("runtime_readable", runtime.is_dir(), str(runtime)))
    return ActivationPreflight(
        checks=tuple(checks),
        sid=sid or "",
        workspace=str(workspace) if workspace is not None else "",
        elevated=elevated,
    )


def verify_activation(workspace: Path | None = None) -> ActivationPreflight:
    """Alias of :func:`preflight` used by the post-activation verification."""

    return preflight(workspace)


def grant_acl(path: Path, sid: str, access: str) -> list[str]:
    """Grant the AppContainer SID access to one path, and nothing else.

    The command is returned so the caller can show it; it is executed with
    ``icacls``, which is the standard Windows ACL editor.
    """

    command = ["icacls", str(path), "/grant", f"*{sid}:{access}"]
    import subprocess

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise OSError(
            completed.returncode,
            f"the ACL for {path} could not be granted: {completed.stderr or completed.stdout}",
        )
    return [" ".join(f'"{part}"' if " " in part else part for part in command)]


def activation_plan(workspace: Path | None = None) -> dict[str, Any]:
    """What the activation would change. Nothing is changed to produce it."""

    import sys as _sys

    runtime = Path(_sys.executable).resolve().parent
    base = Path(getattr(_sys, "base_prefix", _sys.prefix)).resolve()
    plan = {
        "schema": "quantsmind.sandbox_activation_plan/1",
        "profile_name": APP_CONTAINER_NAME,
        "elevated": is_elevated(),
        "changes": [
            {
                "what": "an AppContainer profile",
                "name": APP_CONTAINER_NAME,
                "scope": "per-user (created for the current account, owned by it)",
                "why": (
                    "the child needs a security context that the Credential Manager "
                    "does not serve and that has no network capability"
                ),
                "reversible": "DeleteAppContainerProfile removes it",
            },
            {
                "what": "a read-and-execute ACL for the AppContainer SID",
                "path": str(runtime),
                "scope": "per-application (the interpreter's directory)",
                "why": "the child must be able to load the Python runtime",
                "reversible": "icacls <path> /remove *<sid>",
            },
            {
                "what": "a read-and-execute ACL for the AppContainer SID",
                "path": str(base),
                "scope": "per-application (the base interpreter's directory)",
                "why": (
                    "a virtual environment's interpreter loads the base installation's "
                    "standard library and DLLs"
                ),
                "reversible": "icacls <path> /remove *<sid>",
            },
            {
                "what": "a modify ACL for the AppContainer SID",
                "path": str(workspace) if workspace else "(the disposable workspace)",
                "scope": "per-application (the disposable workspace)",
                "why": "the child must be able to write the benchmark's own files",
                "reversible": "icacls <path> /remove *<sid>",
            },
        ],
        "not_changed": [
            "the owner's credentials and the credential store",
            "the user's profile beyond the interpreter's directory",
            "any other repository",
            "firewall rules or network settings",
            "Windows accounts",
            "virtualisation features",
        ],
    }
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare or apply the AppContainer sandbox activation"
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="check",
        choices=["check", "preflight", "verify", "apply"],
        help=(
            "check (default) changes nothing; preflight reports every "
            "precondition; verify returns nonzero unless activation is complete; "
            "apply provisions the boundary"
        ),
    )
    parser.add_argument(
        "--workspace",
        default="",
        help="the disposable workspace the container must be able to write",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f'the operator must type: "{CONFIRMATION_PHRASE}"',
    )
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).resolve() if args.workspace else None
    plan = activation_plan(workspace)

    if args.action == "check":
        print(json.dumps(plan, indent=2, sort_keys=True))
        print(
            "\nThis is a dry run: nothing was created, granted or changed.\n"
            f'Run "apply" with --confirm "{CONFIRMATION_PHRASE}" from an elevated shell.'
        )
        return 0

    if args.action == "preflight":
        result = preflight(workspace)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.action == "verify":
        result = verify_activation(workspace)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0 if result.ready else 1

    if args.confirm != CONFIRMATION_PHRASE:
        print(
            "REFUSED: the activation needs the explicit confirmation phrase. Nothing was changed.",
            file=sys.stderr,
        )
        return 2
    if workspace is None:
        print(
            "REFUSED: --workspace is required, so the container is never given access "
            "to an unspecified path. Nothing was changed.",
            file=sys.stderr,
        )
        return 2
    if not plan["elevated"]:
        print(
            "REFUSED: the activation must be run from an elevated shell. Nothing was changed.",
            file=sys.stderr,
        )
        return 2

    sid = create_profile()
    print(f"AppContainer profile: {APP_CONTAINER_NAME} ({sid})")
    import sys as _sys

    commands: list[str] = []
    for path in dict.fromkeys(
        [Path(_sys.executable).resolve().parent, Path(_sys.base_prefix).resolve()]
    ):
        commands += grant_acl(path, sid, RUNTIME_ACCESS)
    commands += grant_acl(workspace, sid, WORKSPACE_ACCESS)
    print("The following commands were run:")
    for command in commands:
        print(f"  {command}")
    print(
        "\nTo reverse this:\n"
        f'  icacls "<path>" /remove *{sid}\n'
        f"  and delete the AppContainer profile {APP_CONTAINER_NAME}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
