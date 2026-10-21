"""Read and enforce the release version and Windows Installer identity policy.

The source version is authoritative for the package. This module validates that
the other version-bearing files agree with it before a build or release starts,
and derives the MSI ProductCode from the version so one release cannot silently
reuse another release's product identity.

Run the local check with::

    python scripts/release_policy.py
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO / "packaging" / "release-policy.toml"
VERSION_PATH = REPO / "src" / "quantsmind_agent" / "version.py"
PYPROJECT_PATH = REPO / "pyproject.toml"

_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_UPGRADE_CODE_RE = re.compile(r"^\{[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}\}$")
_VERSION_SOURCE_RE = re.compile(r'__version__: Final\[str\] = "([^"]+)"')
_VERSION_INFO_RE = re.compile(
    r"version_info: Final\[tuple\[int, int, int\]\] = "
    r"\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)"
)

_ALLOWED_UPGRADE_SCHEDULES = {"afterInstallInitialize", "afterInstallValidate"}


class ReleasePolicyError(ValueError):
    """A release input violates the local policy."""


@dataclass(frozen=True)
class ReleasePolicy:
    product_name: str
    upgrade_code: str
    target_version: str
    qualified_source_version: str
    source_is_target: bool
    distribution_authorized: bool
    minimum_upgrade_from: str
    product_code_namespace: uuid.UUID
    upgrade_schedule: str
    allow_same_version_upgrades: bool
    signing_algorithm: str
    timestamp_required: bool
    certificate_thumbprint_env: str
    timestamp_url_env: str


def _table(data: dict[str, object], name: str) -> dict[str, object]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ReleasePolicyError(f"release policy is missing [{name}]")
    return value


def _string(data: dict[str, object], name: str, field: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ReleasePolicyError(f"release policy {field} must be a non-empty string")
    return value.strip()


def _boolean(data: dict[str, object], name: str, field: str) -> bool:
    value = data.get(name)
    if not isinstance(value, bool):
        raise ReleasePolicyError(f"release policy {field} must be true or false")
    return value


def load_policy(path: Path = POLICY_PATH) -> ReleasePolicy:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleasePolicyError(f"cannot read release policy {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ReleasePolicyError(f"invalid release policy {path}: {exc}") from exc

    if data.get("schema_version") != 1:
        raise ReleasePolicyError("release policy schema_version must be 1")

    product = _table(data, "product")
    release = _table(data, "release")
    installer = _table(data, "windows_installer")
    signing = _table(data, "signing")

    namespace_text = _string(
        installer, "product_code_namespace", "windows_installer.product_code_namespace"
    )
    try:
        namespace = uuid.UUID(namespace_text)
    except ValueError as exc:
        raise ReleasePolicyError("windows_installer.product_code_namespace is not a UUID") from exc

    policy = ReleasePolicy(
        product_name=_string(product, "name", "product.name"),
        upgrade_code=_string(product, "upgrade_code", "product.upgrade_code"),
        target_version=_string(release, "target_version", "release.target_version"),
        qualified_source_version=_string(
            release, "qualified_source_version", "release.qualified_source_version"
        ),
        source_is_target=_boolean(release, "source_is_target", "release.source_is_target"),
        distribution_authorized=_boolean(
            release, "distribution_authorized", "release.distribution_authorized"
        ),
        minimum_upgrade_from=_string(
            release, "minimum_upgrade_from", "release.minimum_upgrade_from"
        ),
        product_code_namespace=namespace,
        upgrade_schedule=_string(
            installer, "upgrade_schedule", "windows_installer.upgrade_schedule"
        ),
        allow_same_version_upgrades=_boolean(
            installer,
            "allow_same_version_upgrades",
            "windows_installer.allow_same_version_upgrades",
        ),
        signing_algorithm=_string(signing, "algorithm", "signing.algorithm"),
        timestamp_required=_boolean(signing, "timestamp_required", "signing.timestamp_required"),
        certificate_thumbprint_env=_string(
            signing, "certificate_thumbprint_env", "signing.certificate_thumbprint_env"
        ),
        timestamp_url_env=_string(signing, "timestamp_url_env", "signing.timestamp_url_env"),
    )
    validate_policy(policy)
    return policy


def validate_policy(policy: ReleasePolicy) -> None:
    errors: list[str] = []
    try:
        version_tuple(policy.target_version)
        version_tuple(policy.qualified_source_version)
        version_tuple(policy.minimum_upgrade_from)
    except ReleasePolicyError as exc:
        errors.append(str(exc))
    else:
        if version_tuple(policy.target_version) <= version_tuple(policy.qualified_source_version):
            errors.append("release.target_version must be newer than qualified_source_version")
        if version_tuple(policy.minimum_upgrade_from) > version_tuple(policy.target_version):
            errors.append("release.minimum_upgrade_from must not be newer than target_version")

    if not _UPGRADE_CODE_RE.fullmatch(policy.upgrade_code):
        errors.append("product.upgrade_code must be an uppercase braced GUID")
    if policy.upgrade_schedule not in _ALLOWED_UPGRADE_SCHEDULES:
        errors.append(
            "windows_installer.upgrade_schedule must be "
            + " or ".join(sorted(_ALLOWED_UPGRADE_SCHEDULES))
        )
    if policy.allow_same_version_upgrades:
        errors.append("same-version MSI replacement must remain disabled")
    if policy.signing_algorithm.lower() != "sha256":
        errors.append("signing.algorithm must be sha256")
    if not policy.timestamp_required:
        errors.append("signing.timestamp_required must remain true")

    if errors:
        raise ReleasePolicyError("; ".join(errors))


def version_tuple(version: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        raise ReleasePolicyError(f"version {version!r} is not major.minor.patch")
    major, minor, patch = (int(part) for part in match.groups())
    if major > 255:
        raise ReleasePolicyError(f"version {version!r}: major field must be at most 255")
    if minor > 255:
        raise ReleasePolicyError(f"version {version!r}: minor field must be at most 255")
    if patch > 65535:
        raise ReleasePolicyError(f"version {version!r}: patch field must be at most 65535")
    return major, minor, patch


def read_source_version(path: Path = VERSION_PATH) -> str:
    text = path.read_text(encoding="utf-8")
    match = _VERSION_SOURCE_RE.search(text)
    if match is None:
        raise ReleasePolicyError(f"{path}: no __version__ assignment found")
    return match.group(1)


def read_version_info(path: Path = VERSION_PATH) -> tuple[int, int, int]:
    text = path.read_text(encoding="utf-8")
    match = _VERSION_INFO_RE.search(text)
    if match is None:
        raise ReleasePolicyError(f"{path}: no version_info assignment found")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def read_pyproject_version(path: Path = PYPROJECT_PATH) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project")
    if not isinstance(project, dict):
        raise ReleasePolicyError(f"{path}: no [project] table")
    version = project.get("version")
    if not isinstance(version, str):
        raise ReleasePolicyError(f"{path}: no static project version")
    return version


def product_code_for(version: str, policy: ReleasePolicy | None = None) -> str:
    version_tuple(version)
    selected = policy or load_policy()
    value = uuid.uuid5(selected.product_code_namespace, f"product:{version}")
    return "{" + str(value).upper() + "}"


def validate_release_activation(policy: ReleasePolicy) -> list[str]:
    errors: list[str] = []
    if not policy.source_is_target:
        errors.append(
            f"source is {policy.qualified_source_version}, not the target {policy.target_version}"
        )
    if not policy.distribution_authorized:
        errors.append("release.distribution_authorized is false")
    return errors


def validate_upgrade_pair(
    source_version: str, target_version: str, policy: ReleasePolicy | None = None
) -> None:
    selected = policy or load_policy()
    source = version_tuple(source_version)
    target = version_tuple(target_version)
    errors: list[str] = []
    if source != version_tuple(selected.minimum_upgrade_from):
        errors.append(
            f"source {source_version} is not the supported upgrade source "
            f"{selected.minimum_upgrade_from}"
        )
    if target != version_tuple(selected.target_version):
        errors.append(f"target {target_version} is not the policy target {selected.target_version}")
    if target <= source:
        errors.append("target version must be newer; same-version and downgrade are refused")
    if product_code_for(source_version, selected) == product_code_for(target_version, selected):
        errors.append("source and target ProductCodes must differ")
    if errors:
        raise ReleasePolicyError("; ".join(errors))


def validate_source_tree(
    policy: ReleasePolicy,
    *,
    version_path: Path = VERSION_PATH,
    pyproject_path: Path = PYPROJECT_PATH,
) -> list[str]:
    errors: list[str] = []
    try:
        source = read_source_version(version_path)
        source_tuple = version_tuple(source)
    except ReleasePolicyError as exc:
        return [str(exc)]
    except OSError as exc:
        return [f"cannot read {version_path}: {exc}"]

    try:
        declared_info = read_version_info(version_path)
    except ReleasePolicyError as exc:
        errors.append(str(exc))
    except OSError as exc:
        errors.append(f"cannot read {version_path}: {exc}")
    else:
        if declared_info != source_tuple:
            errors.append(f"version_info {declared_info} does not match __version__ {source}")

    try:
        pyproject_version = read_pyproject_version(pyproject_path)
    except (OSError, ReleasePolicyError) as exc:
        errors.append(str(exc))
    else:
        if pyproject_version != source:
            errors.append(f"pyproject version {pyproject_version} does not match {source}")

    if source == policy.target_version:
        if not policy.source_is_target:
            errors.append("source is the target version but release.source_is_target is false")
    elif source == policy.qualified_source_version:
        if policy.source_is_target:
            errors.append("source is qualified but release.source_is_target is true")
    else:
        errors.append(
            f"source {source} is neither qualified {policy.qualified_source_version} "
            f"nor target {policy.target_version}"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=str(POLICY_PATH), help="release policy TOML")
    parser.add_argument("--product-code", metavar="VERSION", default=None)
    args = parser.parse_args(argv)

    try:
        policy = load_policy(Path(args.policy))
    except (OSError, ReleasePolicyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if args.product_code is not None:
        try:
            print(product_code_for(args.product_code, policy))
        except ReleasePolicyError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        return 0

    errors = validate_source_tree(policy)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "OK: "
        f"{policy.product_name} source={read_source_version()} "
        f"target={policy.target_version} "
        f"target_active={str(policy.source_is_target).lower()} "
        f"distribution_authorized={str(policy.distribution_authorized).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
