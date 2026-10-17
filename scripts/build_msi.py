"""Build a Windows Installer (.msi) for QuantsMind-Agent from an onedir build.

PyInstaller's onedir output is a *folder*: awkward to distribute, and it gives the
user no Start Menu entry, no Add/Remove Programs entry and no uninstaller. This
turns that folder into a real Windows Installer package.

Why the ``.wxs`` is generated instead of hand-written
-----------------------------------------------------
The bundle is around 900 files in roughly 100 directories, and Windows Installer
wants one component per directory, each with an identity that stays the same
across product versions so an upgrade replaces files instead of duplicating them.
Hand-maintaining that is not realistic, so this script generates the authoring
from the build output and derives every component GUID from its path. The same
bundle therefore always produces the same identities.

Install scope
-------------
``--scope perUser`` (the default) installs to the per-user Programs folder with
**no elevation**, which is what lets a package be built, installed, launched and
uninstalled on a machine with no administrator rights. ``--scope perMachine``
targets ``Program Files`` and does require elevation; only the resolved directory
and the package ``Scope`` change.

Neither scope touches user data. Configuration, credentials, logs and projects
live outside the installation directory, and this package installs nothing there,
so upgrades and uninstalls leave them alone. That boundary is asserted rather
than assumed: the build fails if anything user-specific is about to be packaged.

Prerequisites
-------------
WiX Toolset 4 or newer, as a .NET tool::

    dotnet tool install --global wix

Usage::

    python scripts/build_msi.py --onedir artifacts/v0.2.0/windows/onedir
    python scripts/build_msi.py --onedir dist --scope perMachine

Writes ``BUILD-METADATA.json`` beside the MSI, recording the tool, the command,
the package size and its SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parents[1]

#: Root the generated identities are derived from. A constant, not a random UUID,
#: so rebuilding the same bundle produces the same component GUIDs -- which is
#: what makes installing a later version an upgrade rather than a second install.
IDENTITY_NAMESPACE = uuid.UUID("6f6a1f2e-6c1a-5f2b-9d3e-7c4a2b8d0f11")

#: Product identity, stable across every version. Windows Installer uses it to
#: recognise a newer build of the same product during an upgrade.
UPGRADE_CODE = "{7C4A2B8D-0F11-4E2A-9B3C-5D6E7F8A9B0C}"

MANUFACTURER = "QuantsMind"

#: Never packaged: these are per-user configuration or credential stores, and
#: shipping one inside an installer hands every recipient the build machine's
#: settings.
FORBIDDEN_NAMES = {"config.toml", "secrets.json", "credentials.json", ".env"}

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
checks: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def environment(name: str, ok: bool, detail: str = "") -> None:
    """Record an environment fact.

    Whether a signing certificate exists is a property of the build machine, not
    a property of the package. Reporting it as a failed check would mean a
    perfectly good unsigned package reports a failed build, and reporting it as a
    pass would imply the package is signed. It is neither: it is reported, and it
    does not decide the exit code.
    """

    status = PASS if ok else INCONCLUSIVE
    checks.append((name, status, detail))
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def read_version() -> str:
    """Read the product version without importing the package."""

    text = (REPO / "src" / "quantsmind_agent" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__: Final\[str\] = "([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def git_commit() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            or None
        )
    except OSError:  # pragma: no cover - git absent
        return None


def find_wix() -> str | None:
    """Locate the WiX CLI, preferring a .NET global tool installation."""

    found = shutil.which("wix")
    if found:
        return found
    candidate = Path.home() / ".dotnet" / "tools" / "wix.exe"
    return str(candidate) if candidate.exists() else None


def tool_version(executable: str) -> str:
    try:
        out = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - tool absent
        return "unknown"
    first = out.stdout.strip().splitlines()
    return first[0] if first else "unknown"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_user_data(onedir_root: Path) -> list[str]:
    """Return every packaged path that looks like per-user data or a secret."""

    return [
        str(path.relative_to(onedir_root))
        for path in onedir_root.rglob("*")
        if path.is_file() and path.name.lower() in FORBIDDEN_NAMES
    ]


def identity(relative: str) -> str:
    """A stable GUID for a bundle path."""

    return "{" + str(uuid.uuid5(IDENTITY_NAMESPACE, relative)).upper() + "}"


def identifier(prefix: str, relative: str) -> str:
    """A WiX identifier derived from a path, so it is stable and legal."""

    slug = re.sub(r"[^A-Za-z0-9_]", "_", relative)[:40]
    return f"{prefix}_{slug}_{uuid.uuid5(IDENTITY_NAMESPACE, relative).hex[:8]}"


def bundle_tree(onedir_root: Path) -> tuple[list[str], dict[str, list[Path]]]:
    """Return (directories, files-per-directory) for the bundle.

    ``os.walk`` is top-down, so a parent directory always precedes its children,
    which is the order WiX requires.
    """

    dirs: list[str] = []
    files: dict[str, list[Path]] = {}
    for current, _subdirs, filenames in os.walk(onedir_root):
        relative = os.path.relpath(current, onedir_root).replace("\\", "/")
        dirs.append("" if relative == "." else relative)
        files["" if relative == "." else relative] = [
            Path(current) / name for name in sorted(filenames)
        ]
    return dirs, files


def generate_wxs(
    onedir_root: Path,
    exe_name: str,
    version: str,
    scope: str,
    wxs_path: Path,
) -> tuple[int, int]:
    """Write the WiX authoring for a bundle. Returns (components, files)."""

    per_machine = scope == "perMachine"
    standard_dir = "ProgramFiles6432Folder" if per_machine else "LocalAppDataFolder"
    dirs, files = bundle_tree(onedir_root)

    out: list[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!-- Generated by scripts/build_msi.py. Do not edit by hand. -->",
        '<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">',
        "  <Package",
        '    Name="QuantsMindAgent"',
        f'    Manufacturer="{escape(MANUFACTURER)}"',
        f'    Version="{version}"',
        f'    UpgradeCode="{UPGRADE_CODE}"',
        f'    Scope="{scope}"',
        '    Compressed="yes"',
        '    InstallerVersion="500">',
        "    <SummaryInformation",
        f'      Description="QuantsMind-Agent {escape(version)} - a local coding agent"',
        f'      Manufacturer="{escape(MANUFACTURER)}" />',
        "    <MajorUpgrade",
        '      AllowSameVersionUpgrades="yes"',
        '      DowngradeErrorMessage="A newer version of QuantsMind-Agent is already'
        ' installed." />',
        '    <MediaTemplate EmbedCab="yes" />',
        "",
        "    <!-- Application files only. Configuration, credentials, logs and",
        "         projects live outside this directory and are never touched by",
        "         install, upgrade or uninstall. -->",
        f'    <StandardDirectory Id="{standard_dir}">',
    ]
    pad = "      "
    if not per_machine:
        out.append(f'{pad}<Directory Id="INSTALLPARENT" Name="Programs">')
        pad += "  "
    out.append(f'{pad}<Directory Id="INSTALLDIR" Name="QuantsMindAgent">')

    # Directory rows must be *nested*, not merely ordered. WiX reads each
    # <Directory> as a child of the element it sits inside, so a flattened list of
    # correctly indented rows installs _internal/PySide6/plugins as a top-level
    # "plugins" folder, and the application then fails to start with a missing
    # native module instead of an obviously broken layout.
    children: dict[str, list[str]] = {}
    for relative in dirs:
        if not relative:
            continue
        parent = str(Path(relative).parent).replace("\\", "/")
        children.setdefault("" if parent == "." else parent, []).append(relative)

    def emit_dirs(parent: str, depth: int) -> None:
        for relative in sorted(children.get(parent, [])):
            row = " " * (len(pad) + 2 + depth * 2)
            out.append(
                f'{row}<Directory Id="{identifier("dir", relative)}"'
                f' Name="{escape(Path(relative).name)}">'
            )
            emit_dirs(relative, depth + 1)
            out.append(f"{row}</Directory>")

    emit_dirs("", 0)
    out.append(f"{pad}</Directory>")
    if not per_machine:
        out.append("      </Directory>")
    out.append("    </StandardDirectory>")
    out += [
        "",
        "    <!-- Shortcut folders. For a per-user package Windows Installer",
        "         resolves these inside the installing user's profile. -->",
        '    <StandardDirectory Id="ProgramMenuFolder" />',
        '    <StandardDirectory Id="DesktopFolder" />',
        "",
        '    <ComponentGroup Id="AppFiles" Directory="INSTALLDIR">',
    ]

    components = 0
    file_count = 0
    for relative in dirs:
        if not files.get(relative):
            continue
        components += 1
        directory_id = "INSTALLDIR" if not relative else identifier("dir", relative)
        key = relative or "install"
        holds_exe = any(source.name == exe_name for source in files[relative])
        keypath = "" if holds_exe else ' KeyPath="yes"'
        out += [
            f'      <Component Id="{identifier("cmp", key)}" Directory="{directory_id}"'
            f' Guid="{identity(key)}">',
            '        <RegistryValue Root="HKCU"'
            f' Key="Software\\{MANUFACTURER}\\QuantsMindAgent\\files"'
            f' Name="{escape(key)}" Type="integer" Value="1"{keypath} />',
        ]
        for source in files[relative]:
            file_count += 1
            is_exe = source.name == exe_name
            out.append(
                f'        <File Source="{escape(str(source))}" Name="{escape(source.name)}"'
                + (' KeyPath="yes" />' if is_exe else " />")
            )
            if is_exe:
                # The executable owns the shortcuts, so they are created and
                # removed with the file the user actually launches.
                out += [
                    '        <Shortcut Id="StartMenuShortcut" Name="QuantsMind-Agent"',
                    '          Directory="ProgramMenuFolder" WorkingDirectory="INSTALLDIR"',
                    '          Advertise="no" />',
                    '        <Shortcut Id="DesktopShortcut" Name="QuantsMind-Agent"',
                    '          Directory="DesktopFolder" WorkingDirectory="INSTALLDIR"',
                    '          Advertise="no" />',
                    '        <RemoveFolder Id="RemoveStartMenuFolder"'
                    ' Directory="ProgramMenuFolder" On="uninstall" />',
                ]
        out.append("      </Component>")

    out += [
        '      <Component Id="RemoveInstallDir" Directory="INSTALLDIR"'
        f' Guid="{identity("remove-install-dir")}">',
        '        <RegistryValue Root="HKCU"'
        f' Key="Software\\{MANUFACTURER}\\QuantsMindAgent" Name="installed"'
        ' Type="integer" Value="1" KeyPath="yes" />',
        '        <RemoveFolder Id="RemoveInstallDir" On="uninstall" />',
        "      </Component>",
        # Windows Installer does not create the Add/Remove Programs entry for a
        # per-user package by itself, and an installer the user cannot find in
        # installed apps is an installer they cannot uninstall. This is the
        # documented key, written under the installing user's own hive.
        '      <Component Id="UninstallRegistry" Directory="INSTALLDIR"'
        f' Guid="{identity("uninstall-registry")}">',
        '        <RegistryValue Root="HKCU"'
        ' Key="Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\[ProductCode]"'
        ' Name="DisplayName" Type="string" Value="QuantsMind-Agent" KeyPath="yes" />',
        '        <RegistryValue Root="HKCU"'
        ' Key="Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\[ProductCode]"'
        f' Name="DisplayVersion" Type="string" Value="{escape(version)}" />',
        '        <RegistryValue Root="HKCU"'
        ' Key="Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\[ProductCode]"'
        f' Name="Publisher" Type="string" Value="{escape(MANUFACTURER)}" />',
        '        <RegistryValue Root="HKCU"'
        ' Key="Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\[ProductCode]"'
        ' Name="InstallLocation" Type="string" Value="[INSTALLDIR]" />',
        '        <RegistryValue Root="HKCU"'
        ' Key="Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\[ProductCode]"'
        ' Name="UninstallString" Type="string"'
        ' Value="&quot;MsiExec.exe&quot; /X [ProductCode]" />',
        '        <RegistryValue Root="HKCU"'
        ' Key="Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\[ProductCode]"'
        ' Name="NoModify" Type="integer" Value="1" />',
        '        <RegistryValue Root="HKCU"'
        ' Key="Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\[ProductCode]"'
        ' Name="NoRepair" Type="integer" Value="1" />',
        "      </Component>",
        "    </ComponentGroup>",
        "",
        '    <Feature Id="Complete" Title="QuantsMind-Agent" Level="1">',
        '      <ComponentGroupRef Id="AppFiles" />',
        "    </Feature>",
        "  </Package>",
        "</Wix>",
        "",
    ]
    wxs_path.write_text("\n".join(out), encoding="utf-8")
    return components, file_count


def locate_bundle(path: Path) -> tuple[Path | None, str]:
    """Find the bundle root and the executable inside it.

    PyInstaller writes ``dist/<AppName>/<AppName>.exe``, so the directory handed
    to this script is usually the *parent* of the bundle. Both that and the
    bundle folder itself are accepted, so the caller does not have to know which.
    """

    if not path.is_dir():
        return None, ""
    exes = sorted(path.glob("*.exe"))
    if exes:
        return path, exes[0].name
    nested = [child for child in sorted(path.iterdir()) if child.is_dir() and child.glob("*.exe")]
    if len(nested) == 1:
        bundle = nested[0]
        return bundle, sorted(bundle.glob("*.exe"))[0].name
    return None, ""


def find_signing_certificate() -> str | None:
    """Return the subject of a usable code-signing certificate, if any.

    Signing a release package needs a real certificate issued for it. This looks
    for one in the current user's personal store and reports honestly when there
    is none, because an unsigned package that says it is signed is worse than an
    unsigned package that admits it.
    """

    if platform.system() != "Windows":
        return None
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-ChildItem Cert:\\CurrentUser\\My | Where-Object "
                "{$_.HasPrivateKey -and $_.EnhancedKeyUsageList.ObjectId.Value "
                "-contains '1.3.6.1.5.5.7.3.3'} | Select-Object -First 1 "
                "-ExpandProperty Subject",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - powershell absent
        return None
    subject = out.stdout.strip()
    return subject or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--onedir", required=True, help="PyInstaller onedir output (the folder holding the .exe)"
    )
    parser.add_argument("--output", default=None, help="MSI path (default <onedir>/../msi)")
    parser.add_argument("--scope", choices=["perUser", "perMachine"], default="perUser")
    parser.add_argument("--keep-wxs", action="store_true", help="keep the generated authoring")
    args = parser.parse_args(argv)

    onedir = Path(args.onedir)
    if not onedir.is_absolute():
        onedir = REPO / onedir
    bundle, exe_name = locate_bundle(onedir)
    if bundle is None:
        print(f"no packaged executable found in {onedir}", file=sys.stderr)
        return 2

    version = read_version()
    output = (
        Path(args.output)
        if args.output
        else onedir.parent / "msi" / f"QuantsMindAgent-{version}-x64.msi"
    )
    if not output.is_absolute():
        output = REPO / output
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"packaging {bundle} -> {output}\n")
    check("source bundle exists", bundle.is_dir(), str(bundle))

    # A signature must come from a real certificate. Report what is available and
    # sign only when a certificate genuinely exists; an invented or self-signed
    # "signature" would be worse than an honest unsigned package.
    signer = find_signing_certificate()
    environment(
        "code-signing certificate available",
        signer is not None,
        signer or "none in CurrentUser\\My; package is UNSIGNED",
    )

    leaked = find_user_data(bundle)
    check(
        "bundle carries no configuration or credential file",
        not leaked,
        ", ".join(leaked[:5]) if leaked else "checked every packaged file name",
    )

    wix = find_wix()
    if wix is None:
        print(
            "WiX Toolset 4+ is required and was not found.\n    dotnet tool install --global wix\n",
            file=sys.stderr,
        )
        return 2
    version_text = tool_version(wix)
    print(f"using WiX {version_text} at {wix}\n")

    wxs_path = output.parent / f"{output.stem}.wxs"
    components, files = generate_wxs(bundle, exe_name, version, args.scope, wxs_path)
    check(
        "authoring generated",
        wxs_path.is_file() and files > 0,
        f"{wxs_path.name}: {components} components, {files} files",
    )

    started = time.time()
    completed = subprocess.run(
        [wix, "build", str(wxs_path), "-o", str(output)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stdout[-4000:], file=sys.stderr)
        print(completed.stderr[-2000:], file=sys.stderr)
        check("wix build", False, f"exit={completed.returncode}")
        if not args.keep_wxs:
            wxs_path.unlink(missing_ok=True)
        return completed.returncode
    check("wix build", True, f"{time.time() - started:.1f}s")

    if not args.keep_wxs:
        wxs_path.unlink(missing_ok=True)

    check("MSI produced", output.is_file(), f"{output.name} {output.stat().st_size} bytes")

    # An MSI is an OLE compound file; anything user-specific inside it would
    # have been caught above by name, so this only confirms the package is
    # readable as a Windows Installer database at all.
    with output.open("rb") as handle:
        magic = handle.read(8)
    check(
        "MSI is a Windows Installer database",
        magic == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        magic.hex(),
    )

    metadata = {
        "artifact": "msi",
        "version": version,
        "commit": git_commit(),
        "scope": args.scope,
        "install_directory": (
            r"%ProgramFiles%\QuantsMindAgent"
            if args.scope == "perMachine"
            else r"%LOCALAPPDATA%\Programs\QuantsMindAgent"
        ),
        "elevation_required": args.scope == "perMachine",
        "user_data_location": r"%LOCALAPPDATA%\QuantsMind-Agent (never packaged)",
        "packaging_tool": f"WiX Toolset {version_text}",
        "packaging_command": f"python scripts/build_msi.py --onedir {args.onedir}",
        "built_on": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "build_seconds": round(time.time() - started, 1),
        "components": components,
        "files": files,
        "filename": output.name,
        "bytes": output.stat().st_size,
        "sha256": sha256_of(output),
        "code_signed": False,
        "returncode": completed.returncode,
    }
    (output.parent / "BUILD-METADATA.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    failures = [name for name, status, _d in checks if status == FAIL]
    unverified = [name for name, status, _d in checks if status == INCONCLUSIVE]
    print()
    print(f"{len(checks) - len(failures)}/{len(checks)} MSI build checks passed")
    if unverified:
        print("environment, not gating: " + ", ".join(unverified))
    if failures:
        print("failing: " + ", ".join(failures))
        return 1
    print(f"built: {output}")
    print("note: this builds the package. Installing, launching and uninstalling it")
    print("      are separate steps -- see scripts/verify_msi_install.ps1")
    print("note: the package is UNSIGNED; Windows SmartScreen will warn on first run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
