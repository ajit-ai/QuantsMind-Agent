"""Sign and verify Windows release artifacts with an explicit identity.

This module never selects a certificate from a personal store by subject. A
release signer supplies the certificate thumbprint and RFC 3161 timestamp URL
through the environment, so an accidental local certificate cannot be used.

Environment::

    QM_SIGNING_CERT_SHA1       40-hex SHA-1 thumbprint of the signing certificate
    QM_SIGNING_TIMESTAMP_URL   HTTPS RFC 3161 timestamp service
    QM_SIGNTOOL                optional explicit path to signtool.exe

Commands::

    python scripts/signing.py check
    python scripts/signing.py sign path\\to\\artifact.exe
    python scripts/signing.py verify path\\to\\artifact.exe
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
CERTIFICATE_ENV = "QM_SIGNING_CERT_SHA1"
TIMESTAMP_ENV = "QM_SIGNING_TIMESTAMP_URL"
SIGNTOOL_ENV = "QM_SIGNTOOL"
_THUMBPRINT_RE = re.compile(r"^[0-9A-F]{40}$")


class SigningError(RuntimeError):
    """Signing or verification cannot proceed safely."""


@dataclass(frozen=True)
class SigningConfig:
    certificate_thumbprint: str
    timestamp_url: str
    signtool: str


def normalize_thumbprint(value: str) -> str:
    thumbprint = value.replace(" ", "").replace(":", "").upper()
    if not _THUMBPRINT_RE.fullmatch(thumbprint):
        raise SigningError(f"{CERTIFICATE_ENV} must be a 40-character SHA-1 certificate thumbprint")
    return thumbprint


def validate_timestamp_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SigningError(f"{TIMESTAMP_ENV} must be an absolute HTTPS URL")
    return value


def find_signtool(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    explicit = env.get(SIGNTOOL_ENV, "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SigningError(f"{SIGNTOOL_ENV} points to a missing file: {explicit}")
        return str(path)
    found = shutil.which("signtool")
    if found:
        return found

    roots = [
        env.get("ProgramFiles(x86)", ""),
        env.get("ProgramFiles", ""),
    ]
    versions: list[Path] = []
    for root in filter(None, roots):
        bin_root = Path(root) / "Windows Kits" / "10" / "bin"
        if bin_root.is_dir():
            versions.extend(
                child
                for child in bin_root.iterdir()
                if child.is_dir() and child.name.startswith("10.")
            )
    for version in sorted(versions, key=lambda item: item.name, reverse=True):
        for architecture in ("x64", "x86", "arm64"):
            candidate = version / architecture / "signtool.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def load_signing_config(
    environ: Mapping[str, str] | None = None,
    *,
    require_tool: bool = True,
) -> SigningConfig:
    env = os.environ if environ is None else environ
    raw_thumbprint = env.get(CERTIFICATE_ENV, "").strip()
    if not raw_thumbprint:
        raise SigningError(
            f"{CERTIFICATE_ENV} is required; no certificate is selected automatically"
        )
    raw_timestamp = env.get(TIMESTAMP_ENV, "").strip()
    if not raw_timestamp:
        raise SigningError(f"{TIMESTAMP_ENV} is required")

    signtool = find_signtool(env)
    if signtool is None and require_tool:
        raise SigningError(
            "signtool.exe was not found; install the Windows SDK or set " + SIGNTOOL_ENV
        )
    return SigningConfig(
        certificate_thumbprint=normalize_thumbprint(raw_thumbprint),
        timestamp_url=validate_timestamp_url(raw_timestamp),
        signtool=signtool or "signtool.exe",
    )


def sign_command(path: Path | str, config: SigningConfig) -> list[str]:
    path = Path(path)
    if not path.is_file():
        raise SigningError(f"cannot sign a missing file: {path}")
    return [
        config.signtool,
        "sign",
        "/sha1",
        config.certificate_thumbprint,
        "/fd",
        "sha256",
        "/tr",
        config.timestamp_url,
        "/td",
        "sha256",
        "/v",
        str(path),
    ]


def verify_command(path: Path | str, signtool: str) -> list[str]:
    path = Path(path)
    if not path.is_file():
        raise SigningError(f"cannot verify a missing file: {path}")
    return [signtool, "verify", "/pa", "/all", "/v", str(path)]


def _run(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SigningError(f"could not run {command[0]}: {exc}") from exc
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode != 0:
        raise SigningError(f"{command[0]} exited {completed.returncode}: {output[-2000:]}")
    return output


def authenticode_status(path: Path | str) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        raise SigningError(f"cannot inspect a missing file: {path}")
    quoted = "'" + str(path).replace("'", "''") + "'"
    script = (
        f"$s = Get-AuthenticodeSignature -LiteralPath {quoted}; "
        "$signer = $null; if ($s.SignerCertificate) { "
        "$signer = $s.SignerCertificate.Thumbprint }; "
        "$subject = $null; if ($s.SignerCertificate) { "
        "$subject = $s.SignerCertificate.Subject }; "
        "$stamper = $null; if ($s.TimeStamperCertificate) { "
        "$stamper = $s.TimeStamperCertificate.Subject }; "
        "[pscustomobject]@{ Status = $s.Status.ToString(); "
        "SignerThumbprint = $signer; SignerSubject = $subject; "
        "TimeStamperSubject = $stamper; StatusMessage = $s.StatusMessage } "
        "| ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SigningError(f"could not inspect Authenticode signature: {exc}") from exc
    if completed.returncode != 0:
        raise SigningError(completed.stderr.strip() or "Authenticode inspection failed")
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SigningError(f"Authenticode inspection returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SigningError("Authenticode inspection returned no object")
    return {key: "" if value is None else str(value) for key, value in parsed.items()}


def verify_file(
    path: Path | str,
    *,
    expected_thumbprint: str | None = None,
    require_timestamp: bool = True,
    signtool: str | None = None,
) -> dict[str, str]:
    path = Path(path)
    tool = signtool or find_signtool()
    if tool is None:
        raise SigningError(
            "signtool.exe was not found; install the Windows SDK or set " + SIGNTOOL_ENV
        )
    details = authenticode_status(path)
    errors: list[str] = []
    if details.get("Status") != "Valid":
        errors.append(
            f"Authenticode status is {details.get('Status') or 'unknown'}"
            + (f": {details['StatusMessage']}" if details.get("StatusMessage") else "")
        )
    if expected_thumbprint is not None:
        expected = normalize_thumbprint(expected_thumbprint)
        if details.get("SignerThumbprint") != expected:
            errors.append("signer thumbprint does not match the authorized certificate")
    if require_timestamp and not details.get("TimeStamperSubject"):
        errors.append("signature has no trusted timestamp")
    if errors:
        raise SigningError("; ".join(errors))

    output = _run(verify_command(path, tool))
    if "Successfully verified" not in output:
        raise SigningError("signtool did not report a successful verification")
    if require_timestamp and "timestamp" not in output.lower():
        raise SigningError("signtool did not report a timestamped signature")
    return details


def sign_file(path: Path | str, config: SigningConfig, *, dry_run: bool = False) -> list[str]:
    path = Path(path)
    command = sign_command(path, config)
    if dry_run:
        return command
    _run(command)
    verify_file(
        path,
        expected_thumbprint=config.certificate_thumbprint,
        require_timestamp=True,
        signtool=config.signtool,
    )
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="validate the signing environment")

    sign_parser = subparsers.add_parser("sign", help="sign and verify one artifact")
    sign_parser.add_argument("path", type=Path)
    sign_parser.add_argument("--dry-run", action="store_true")

    verify_parser = subparsers.add_parser("verify", help="verify one signed artifact")
    verify_parser.add_argument("path", type=Path)
    verify_parser.add_argument("--expected-thumbprint", default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            config = load_signing_config()
            print(f"OK: certificate {config.certificate_thumbprint}")
            print(f"OK: timestamp {config.timestamp_url}")
            print(f"OK: signtool {config.signtool}")
            return 0
        if args.command == "sign":
            config = load_signing_config()
            command = sign_file(args.path, config, dry_run=args.dry_run)
            print(" ".join(command))
            return 0
        expected = args.expected_thumbprint or os.environ.get(CERTIFICATE_ENV)
        details = verify_file(args.path, expected_thumbprint=expected)
        print(f"OK: {args.path} signer={details.get('SignerSubject', '')}")
        return 0
    except SigningError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
