"""Tests for the release version and MSI identity policy."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import release_policy  # noqa: E402

POLICY = release_policy.load_policy()


class TestPolicyFile:
    def test_target_is_newer_than_the_qualified_source(self) -> None:
        assert release_policy.version_tuple(POLICY.target_version) > release_policy.version_tuple(
            POLICY.qualified_source_version
        )

    def test_source_is_not_the_target(self) -> None:
        assert POLICY.target_version == "1.0.0"
        assert POLICY.qualified_source_version == "0.9.0"
        assert POLICY.source_is_target is False
        assert POLICY.distribution_authorized is False

    def test_product_identity_is_stable(self) -> None:
        assert POLICY.upgrade_code == "{7C4A2B8D-0F11-4E2A-9B3C-5D6E7F8A9B0C}"

    def test_same_version_replacement_is_disabled(self) -> None:
        assert POLICY.allow_same_version_upgrades is False
        assert POLICY.upgrade_schedule == "afterInstallInitialize"

    def test_signing_requires_an_explicit_thumbprint_and_timestamp(self) -> None:
        assert POLICY.timestamp_required is True
        assert POLICY.certificate_thumbprint_env == "QM_SIGNING_CERT_SHA1"
        assert POLICY.timestamp_url_env == "QM_SIGNING_TIMESTAMP_URL"

    def test_policy_rejects_same_version_upgrades(self) -> None:
        broken = dataclasses.replace(POLICY, allow_same_version_upgrades=True)
        with pytest.raises(release_policy.ReleasePolicyError, match="same-version"):
            release_policy.validate_policy(broken)

    def test_policy_rejects_a_validate_time_upgrade_schedule(self) -> None:
        broken = dataclasses.replace(POLICY, upgrade_schedule="beforeInstallValidate")
        with pytest.raises(release_policy.ReleasePolicyError, match="upgrade_schedule"):
            release_policy.validate_policy(broken)

    def test_policy_rejects_a_downgrade_target(self) -> None:
        broken = dataclasses.replace(POLICY, target_version="0.8.0")
        with pytest.raises(release_policy.ReleasePolicyError, match="newer"):
            release_policy.validate_policy(broken)


class TestSourceTree:
    def test_the_checked_out_source_matches_the_qualified_baseline(self) -> None:
        assert release_policy.validate_source_tree(POLICY) == []

    def test_pyproject_mismatch_is_reported(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
        errors = release_policy.validate_source_tree(POLICY, pyproject_path=pyproject)
        assert any("pyproject version" in error for error in errors)

    def test_version_info_mismatch_is_reported(self, tmp_path: Path) -> None:
        version_file = tmp_path / "version.py"
        version_file.write_text(
            '__version__: Final[str] = "0.9.0"\n'
            "version_info: Final[tuple[int, int, int]] = (0, 9, 1)\n",
            encoding="utf-8",
        )
        errors = release_policy.validate_source_tree(POLICY, version_path=version_file)
        assert any("version_info" in error for error in errors)

    def test_a_missing_target_activation_is_reported(self, tmp_path: Path) -> None:
        version_file = tmp_path / "version.py"
        version_file.write_text(
            '__version__: Final[str] = "1.0.0"\n'
            "version_info: Final[tuple[int, int, int]] = (1, 0, 0)\n",
            encoding="utf-8",
        )
        errors = release_policy.validate_source_tree(POLICY, version_path=version_file)
        assert any("source_is_target" in error for error in errors)

    def test_an_unmapped_source_version_is_reported(self, tmp_path: Path) -> None:
        version_file = tmp_path / "version.py"
        version_file.write_text(
            '__version__: Final[str] = "0.9.1"\n'
            "version_info: Final[tuple[int, int, int]] = (0, 9, 1)\n",
            encoding="utf-8",
        )
        errors = release_policy.validate_source_tree(POLICY, version_path=version_file)
        assert any("neither qualified" in error for error in errors)


class TestVersions:
    @pytest.mark.parametrize("version", ["1", "1.2", "1.2.3.4", "01.2.3", "v1.2.3", ""])
    def test_malformed_versions_are_rejected(self, version: str) -> None:
        with pytest.raises(release_policy.ReleasePolicyError):
            release_policy.version_tuple(version)

    def test_msi_field_limits_are_enforced(self) -> None:
        assert release_policy.version_tuple("255.255.65535") == (255, 255, 65535)
        with pytest.raises(release_policy.ReleasePolicyError, match="major"):
            release_policy.version_tuple("256.0.0")
        with pytest.raises(release_policy.ReleasePolicyError, match="patch"):
            release_policy.version_tuple("1.0.65536")

    def test_release_activation_is_refused_on_the_qualified_source(self) -> None:
        errors = release_policy.validate_release_activation(POLICY)
        assert any("not the target" in error for error in errors)
        assert any("distribution_authorized" in error for error in errors)

    def test_an_activated_authorized_release_passes(self) -> None:
        activated = dataclasses.replace(POLICY, source_is_target=True, distribution_authorized=True)
        assert release_policy.validate_release_activation(activated) == []

    def test_the_supported_upgrade_pair_is_accepted(self) -> None:
        release_policy.validate_upgrade_pair("0.9.0", "1.0.0", POLICY)

    @pytest.mark.parametrize(
        ("source", "target"),
        [("0.9.0", "0.9.0"), ("1.0.0", "0.9.0"), ("0.8.0", "1.0.0")],
    )
    def test_unsafe_upgrade_pairs_are_refused(self, source: str, target: str) -> None:
        with pytest.raises(release_policy.ReleasePolicyError):
            release_policy.validate_upgrade_pair(source, target, POLICY)


class TestProductCode:
    def test_product_code_is_stable(self) -> None:
        first = release_policy.product_code_for("1.0.0", POLICY)
        second = release_policy.product_code_for("1.0.0", POLICY)
        assert first == second == "{517D7069-CAC9-5722-ACFC-A242687A8D02}"

    def test_product_code_changes_with_version(self) -> None:
        codes = {
            release_policy.product_code_for(version, POLICY)
            for version in ("0.9.0", "1.0.0", "1.0.1")
        }
        assert len(codes) == 3

    def test_product_code_is_not_the_upgrade_code(self) -> None:
        assert release_policy.product_code_for("1.0.0", POLICY) != POLICY.upgrade_code


class TestCommandLine:
    def test_check_succeeds(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert release_policy.main([]) == 0
        output = capsys.readouterr().out
        assert "target=1.0.0" in output
        assert "distribution_authorized=false" in output

    def test_product_code_is_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert release_policy.main(["--product-code", "1.0.0"]) == 0
        assert capsys.readouterr().out.strip() == "{517D7069-CAC9-5722-ACFC-A242687A8D02}"

    def test_invalid_version_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert release_policy.main(["--product-code", "1.0"]) == 1
        assert "FAIL" in capsys.readouterr().err
