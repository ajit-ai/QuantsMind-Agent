"""Collect every packaging artifact into one manifest with SHA-256 checksums.

A release is a set of files on several machines, and "which artifact was that?"
is answered badly from memory and from file names. This walks an artifacts
directory, reads the ``BUILD-METADATA.json`` each build wrote, and produces two
files anyone can check:

* ``MANIFEST.json`` -- the whole matrix: version, commit, OS, architecture,
  Python, packaging tool, build command, size, checksum, and the verification
  status that was actually recorded
* ``SHA256SUMS`` -- one ``<checksum>  <path>`` line per artifact, in the format
  ``sha256sum -c`` expects

Artifacts that were *not* built are recorded too, with their reason. A manifest
that only lists what succeeded invites the reader to assume the rest never
existed, which is the specific mistake this milestone has to avoid.

Usage::

    python scripts/record_artifacts.py --artifacts artifacts/v0.2.0 \\
        --commit 3e4e548 --version 0.2.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The eight formats this milestone asks for, and where each one is expected.
#: Listed up front so a missing platform shows up as a missing platform rather
#: than as silence.
EXPECTED: dict[str, str] = {
    "windows/onedir": "Windows executable-folder distribution",
    "windows/msi": "Windows Installer package",
    "ubuntu/onedir": "Ubuntu executable-folder distribution",
    "ubuntu/deb": "Debian package",
    "macos/app": "macOS application bundle",
    "macos/dmg": "macOS disk image",
    "freebsd/onedir": "FreeBSD executable-folder distribution",
    "freebsd/pkg": "FreeBSD native package",
}


@dataclass
class Entry:
    """One row of the packaging matrix."""

    key: str
    description: str
    status: str
    path: str = ""
    checksum_scope: str = "file"
    key_file: str = ""
    target: str = ""
    build_command: str = ""
    build_tool: str = ""
    built_on: dict[str, str] = field(default_factory=dict)
    bytes: int = 0
    sha256: str = ""
    installed: str = "not attempted"
    launched: str = "not attempted"
    native_verified: bool = False
    limitations: list[str] = field(default_factory=list)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=False
            ).stdout.strip()
            or None
        )
    except OSError:  # pragma: no cover - git absent
        return None


def _payload(path: Path) -> Path | None:
    """The file an artifact is judged by: itself, or a folder's executable.

    PyInstaller's onedir output nests the executable one level down, so the
    search has to look inside the application folder rather than only beside it.
    """

    if path.is_file():
        return path
    candidates = [
        candidate
        for candidate in path.rglob("QuantsMindAgent*")
        if candidate.is_file() and candidate.suffix in (".exe", "")
    ]
    if candidates:
        return min(candidates, key=lambda p: len(p.parts))
    files = [p for p in path.rglob("*") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def _directory_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _tree_checksum(root: Path) -> str:
    """A deterministic digest over a whole directory tree.

    A single file's checksum cannot describe a 160 MB folder, and hashing
    "the biggest file in it" describes nothing useful. This walks every file in
    sorted relative-path order and digests the path and content together, so any
    added, removed, renamed or modified file changes the result. Documented in
    MANIFEST.json so a reader can reproduce it.
    """

    digest = hashlib.sha256()
    for file in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: str(p)):
        digest.update(str(file.relative_to(root)).encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
        digest.update(sha256_of(file).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    """Repository-relative where possible, absolute where not.

    An artifacts directory outside the checkout is a legitimate thing to record,
    and raising ``ValueError`` from ``relative_to`` there would be a confusing
    way to say so.
    """

    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def _as_dict(value: object) -> dict[str, str]:
    """Coerce loaded JSON to a string mapping without pretending to know more."""

    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def collect(artifacts: Path) -> list[Entry]:
    entries: list[Entry] = []
    for key, description in EXPECTED.items():
        target = artifacts / key
        metadata_path = target / "BUILD-METADATA.json"
        if not target.exists():
            entries.append(
                Entry(
                    key=key,
                    description=description,
                    status="BLOCKED",
                    limitations=["not built on this machine; see the packaging documentation"],
                )
            )
            continue

        metadata: dict[str, object] = {}
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        payload = _payload(target)
        built_on = _as_dict(metadata.get("built_on"))
        tool = str(
            metadata.get("packaging_tool") or "PyInstaller (packaging/quantsmind-agent.spec)"
        )
        raw_bytes = metadata.get("bytes")
        # A build that declares a single filename is a package (an .msi, a .deb,
        # a .dmg, a .pkg); anything else is a folder of files. Guessing from the
        # directory alone would describe the MSI as a folder containing one file.
        declared = metadata.get("filename")
        declared_path = target / str(declared) if declared else None
        if declared_path is not None and declared_path.is_file():
            scope, checksum = "file", sha256_of(declared_path)
            artifact_path = _display_path(declared_path)
            key_file = ""
        else:
            scope, checksum = "directory-tree", _tree_checksum(target)
            artifact_path = _display_path(target)
            key_file = _display_path(payload) if payload is not None and payload.is_file() else ""
        entries.append(
            Entry(
                key=key,
                description=description,
                status="BUILT",
                path=artifact_path,
                checksum_scope=scope,
                key_file=key_file,
                target=" ".join(
                    value
                    for value in (
                        built_on.get("system"),
                        built_on.get("release"),
                        built_on.get("machine"),
                    )
                    if value
                ),
                build_command=str(metadata.get("packaging_command") or ""),
                build_tool=tool,
                built_on=built_on,
                bytes=int(raw_bytes) if isinstance(raw_bytes, int) else _directory_bytes(target),
                sha256=checksum,
            )
        )
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, help="the artifacts directory to record")
    parser.add_argument("--commit", default=None, help="source commit for the whole set")
    parser.add_argument("--version", default=None, help="product version for the whole set")
    args = parser.parse_args(argv)

    artifacts = Path(args.artifacts)
    if not artifacts.is_absolute():
        artifacts = REPO / artifacts
    if not artifacts.is_dir():
        print(f"no artifacts directory at {artifacts}", file=sys.stderr)
        return 2

    entries = collect(artifacts)
    version = args.version or "unknown"
    if version == "unknown":
        text = (REPO / "src" / "quantsmind_agent" / "version.py").read_text(encoding="utf-8")
        import re

        match = re.search(r'__version__: Final\[str\] = "([^"]+)"', text)
        version = match.group(1) if match else "unknown"

    manifest = {
        "product": "QuantsMind-Agent",
        "version": version,
        "commit": args.commit or git_commit(),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "recorded_on": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "checksum_algorithm": "sha256",
        "checksum_scopes": {
            "file": "sha256 of the file itself",
            "directory-tree": (
                "sha256 over every file in the directory, in sorted relative-path order, "
                "digesting each path and the sha256 of its contents"
            ),
        },
        "artifacts": [asdict(entry) for entry in entries],
        "notes": [
            "status BUILT means the artifact exists on this machine; native_verified and the "
            "install/launch fields are set only where that was actually done.",
            "BLOCKED entries were not built. They are listed so a reader cannot mistake an "
            "absent platform for a forgotten one.",
        ],
    }

    (artifacts / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    # ``sha256sum -c`` verifies files. Directory artifacts contribute the file a
    # user actually launches, so the set is checkable with one command; the whole
    # folder is described by the tree digest in MANIFEST.json.
    sums = []
    for entry in entries:
        if not entry.sha256 or not entry.path:
            continue
        if entry.checksum_scope == "file":
            sums.append(f"{entry.sha256}  {entry.path}")
        elif entry.key_file:
            sums.append(f"{sha256_of(Path(entry.key_file))}  {entry.key_file}")
    (artifacts / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")

    built = [e for e in entries if e.status == "BUILT"]
    blocked = [e for e in entries if e.status != "BUILT"]
    print(f"recorded {len(entries)} artifacts in {artifacts}")
    for entry in entries:
        mark = "OK  " if entry.status == "BUILT" else "MISS"
        print(f"  [{mark}] {entry.key:16} {entry.status:8} {entry.bytes:>12,} bytes  {entry.path}")
    print()
    print(f"{len(built)} built, {len(blocked)} blocked")
    print("checksums: " + str(artifacts / "SHA256SUMS"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
