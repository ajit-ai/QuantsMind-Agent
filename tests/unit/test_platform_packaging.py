"""Tests for the platform packaging definitions that cannot be built here.

A Debian package, a macOS disk image and a FreeBSD package all have to be built
on their own operating system, so the code that builds them cannot be exercised
in this suite or on this machine. What *can* be checked here is the part that
fails quietly: a control file with an unreplaced placeholder, a desktop entry
whose Exec does not exist, or a builder that would cheerfully produce an
artifact for the wrong platform.

These tests read the definitions and the builders. They do not claim the packages
build.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import build_deb  # noqa: E402
import build_dmg  # noqa: E402
import build_freebsd_pkg  # noqa: E402
import record_artifacts  # noqa: E402

DEB = REPO / "packaging" / "deb"
FREEBSD = REPO / "packaging" / "freebsd"


@pytest.mark.parametrize(
    "path",
    [
        DEB / "control.in",
        DEB / "com.quantsmind.agent.desktop",
        DEB / "postinst",
        DEB / "prerm",
        FREEBSD / "quantsmind-agent.desktop",
    ],
)
def test_packaging_definition_exists(path: Path) -> None:
    assert path.is_file(), f"{path} is missing"


class TestDebianControl:
    @pytest.fixture(scope="class")
    def control(self) -> str:
        return (DEB / "control.in").read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "field", ["Package:", "Version:", "Section:", "Priority:", "Architecture:", "Maintainer:"]
    )
    def test_required_fields_are_present(self, control: str, field: str) -> None:
        """dpkg refuses a control file missing any of these."""

        assert field in control

    @pytest.mark.parametrize(
        "field", ["Package:", "Version:", "Architecture:", "Installed-Size:", "Maintainer:"]
    )
    def test_required_fields_are_not_empty(self, control: str, field: str) -> None:
        value = control.split(field, 1)[1].splitlines()[0].strip()
        assert value, f"{field} is empty"

    def test_declares_runtime_dependencies(self, control: str) -> None:
        """A package that omits Depends installs and then fails to start."""

        depends = next(line for line in control.splitlines() if line.startswith("Depends:"))
        for library in ("libegl1", "libxkbcommon", "libdbus", "libfontconfig"):
            assert library in depends

    def test_placeholders_are_substituted_at_build_time(self, control: str) -> None:
        # These are substituted by the builder; the file itself is a template.
        for placeholder in ("@VERSION@", "@ARCH@", "@SIZE@"):
            assert placeholder in control

    def test_it_does_not_claim_to_be_a_perfect_package_repository(self, control: str) -> None:
        """The description must not promise Debian-wide compatibility."""

        description = control.split("Description:", 1)[1]
        assert "Debian" not in description.split(".", 1)[0]


class TestDesktopEntries:
    @pytest.mark.parametrize(
        "path", [DEB / "com.quantsmind.agent.desktop", FREEBSD / "quantsmind-agent.desktop"]
    )
    def test_entry_has_the_fields_a_launcher_needs(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        assert "[Desktop Entry]" in text
        assert "Type=Application" in text
        assert "Name=QuantsMind-Agent" in text
        assert "Exec=quantsmind-agent" in text
        assert "Terminal=false" in text
        # Without a category the entry is not shown in most menus.
        assert "Categories=" in text and "Development" in text


class TestMaintainerScripts:
    def test_postinst_does_not_touch_user_data(self) -> None:
        text = (DEB / "postinst").read_text(encoding="utf-8")
        assert "update-desktop-database" in text
        for forbidden in ("rm -rf", "rm -f"):
            assert forbidden not in text

    def test_prerm_leaves_user_configuration_alone(self) -> None:
        text = (DEB / "prerm").read_text(encoding="utf-8")
        assert "rm -rf" not in text
        assert "rm -f" not in text
        assert text.strip().startswith("#!/bin/sh")


class TestBuildersRefuseTheWrongPlatform:
    """A builder that runs on the wrong OS must produce nothing, not a bad file."""

    def test_deb_builder_requires_linux(self, tmp_path: Path) -> None:
        bundle = tmp_path / "QuantsMindAgent"
        bundle.mkdir()
        (bundle / "QuantsMindAgent").write_bytes(b"x")
        import platform

        if platform.system() == "Linux":
            pytest.skip("this check is about refusing a non-Linux host")
        assert build_deb.main(["--onedir", str(tmp_path)]) == 2

    def test_dmg_builder_requires_macos(self, tmp_path: Path) -> None:
        app = tmp_path / "QuantsMindAgent.app"
        app.mkdir()
        import platform

        if platform.system() == "Darwin":
            pytest.skip("this check is about refusing a non-macOS host")
        assert build_dmg.main(["--app", str(app)]) == 2

    def test_freebsd_builder_requires_freebsd(self, tmp_path: Path) -> None:
        bundle = tmp_path / "QuantsMindAgent"
        bundle.mkdir()
        (bundle / "QuantsMindAgent").write_bytes(b"x")
        import platform

        if platform.system() == "FreeBSD":
            pytest.skip("this check is about refusing a non-FreeBSD host")
        assert build_freebsd_pkg.main(["--onedir", str(tmp_path)]) == 2


class TestArtifactManifest:
    def test_every_required_format_is_listed(self) -> None:
        """A missing platform must appear as missing, not as silence."""

        assert set(record_artifacts.EXPECTED) == {
            "windows/onedir",
            "windows/msi",
            "ubuntu/onedir",
            "ubuntu/deb",
            "macos/app",
            "macos/dmg",
            "freebsd/onedir",
            "freebsd/pkg",
        }

    def test_a_directory_artifact_is_not_built(self, tmp_path: Path) -> None:
        entries = {entry.key: entry for entry in record_artifacts.collect(tmp_path)}
        assert entries["windows/msi"].status == "BLOCKED"
        assert entries["ubuntu/deb"].limitations

    def test_a_built_package_is_described_by_its_metadata(self, tmp_path: Path) -> None:
        target = tmp_path / "windows" / "msi"
        target.mkdir(parents=True)
        package = target / "QuantsMindAgent-9.7.6-x64.msi"
        package.write_bytes(b"fake msi")
        (target / "BUILD-METADATA.json").write_text(
            '{"artifact": "msi", "version": "9.7.6", "packaging_tool": "WiX Toolset 5.0.2",'
            ' "filename": "QuantsMindAgent-9.7.6-x64.msi",'
            ' "built_on": {"system": "Windows", "release": "11", "machine": "AMD64"}}',
            encoding="utf-8",
        )
        entries = {entry.key: entry for entry in record_artifacts.collect(tmp_path)}
        entry = entries["windows/msi"]
        assert entry.status == "BUILT"
        assert entry.checksum_scope == "file"
        assert entry.sha256 == record_artifacts.sha256_of(package)
        assert "Windows" in entry.target

    def test_a_folder_artifact_gets_a_tree_digest_that_notices_changes(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "windows" / "onedir" / "QuantsMindAgent"
        (target / "_internal").mkdir(parents=True)
        (target / "QuantsMindAgent.exe").write_bytes(b"exe")
        (target / "_internal" / "lib.dll").write_bytes(b"one")

        entries = {entry.key: entry for entry in record_artifacts.collect(tmp_path)}
        entry = entries["windows/onedir"]
        assert entry.checksum_scope == "directory-tree"
        first = entry.sha256

        # A changed file must change the digest, and so must a new one.
        (target / "_internal" / "lib.dll").write_bytes(b"two")
        entries = {e.key: e for e in record_artifacts.collect(tmp_path)}
        assert entries["windows/onedir"].sha256 != first

        (target / "_internal" / "extra.dll").write_bytes(b"three")
        entries = {e.key: e for e in record_artifacts.collect(tmp_path)}
        assert entries["windows/onedir"].sha256 != first
