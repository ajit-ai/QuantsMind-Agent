"""Tests for the Windows Installer packaging.

Building an MSI takes minutes and needs Windows, so these tests cover the part
that can be wrong *silently*: the generated authoring. Two real defects came out
of exactly this area, and neither showed up as a build error.

* Directory rows were emitted as siblings rather than nested. WiX reads each
  ``<Directory>`` as a child of whatever element encloses it, so
  ``_internal/PySide6/plugins`` installed as a top-level ``plugins`` folder. The
  MSI still built, still installed 909 of 914 files, and the application failed
  at startup with ``No module named 'pydantic_core._pydantic_core'`` -- a missing
  dependency rather than a broken installer.
* The Add/Remove Programs key used ``{ProductCode}``, which Windows Installer
  treats as literal text, so the entry appeared under a key literally named
  ``{ProductCode}`` and the package was invisible in installed apps.

Both are cheap to prevent and almost impossible to notice without installing, so
the authoring is parsed and asserted here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import build_msi  # noqa: E402
import release_policy  # noqa: E402
import signing  # noqa: E402

WIX_NS = "{http://wixtoolset.org/schemas/v4/wxs}"

#: A fixed version for the generator tests. They check that whatever version is
#: asked for reaches the package, not that the product is on a particular
#: release, so they do not have to change whenever the version does.
SAMPLE_VERSION = "9.7.6"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A bundle shaped like a real PyInstaller onedir output."""

    root = tmp_path / "QuantsMindAgent"
    (root / "_internal" / "PySide6" / "plugins" / "platforms").mkdir(parents=True)
    (root / "_internal" / "pydantic_core").mkdir(parents=True)
    (root / "_internal" / "PySide6" / "plugins" / "platforms" / "qwindows.dll").write_bytes(b"x")
    (root / "_internal" / "pydantic_core" / "_pydantic_core.pyd").write_bytes(b"x")
    (root / "_internal" / "python314.dll").write_bytes(b"x")
    (root / "QuantsMindAgent.exe").write_bytes(b"x")
    return root


def authoring(bundle: Path, tmp_path: Path, scope: str = "perUser") -> ElementTree.Element:
    path = tmp_path / f"Package-{scope}.wxs"
    build_msi.generate_wxs(bundle, "QuantsMindAgent.exe", SAMPLE_VERSION, scope, path)
    return ElementTree.parse(path).getroot()


@pytest.fixture
def wxs(bundle: Path, tmp_path: Path) -> ElementTree.Element:
    return authoring(bundle, tmp_path)


def directory_names(root: ElementTree.Element) -> list[str]:
    return [d.get("Name") or "" for d in root.iter(f"{WIX_NS}Directory")]


def standard_directory_ids(root: ElementTree.Element) -> list[str]:
    """Ids of the well-known roots, e.g. LocalAppDataFolder.

    These are ``StandardDirectory`` rows rather than ``Directory`` rows, so
    looking only for directories finds none of them.
    """

    return [d.get("Id") or "" for d in root.iter(f"{WIX_NS}StandardDirectory")]


def directory_chain(root: ElementTree.Element, directory_id: str) -> list[str]:
    """Names from the install directory down to ``directory_id``.

    The walk descends through non-Directory elements too: the install directory
    sits under ``StandardDirectory``, and stopping at ``Package`` would report an
    empty chain for every real bundle.
    """

    def walk(element: ElementTree.Element, trail: list[str]) -> list[str] | None:
        for child in element:
            if child.tag == f"{WIX_NS}Directory":
                name = child.get("Name") or ""
                if child.get("Id") == directory_id:
                    return [*trail, name]
                found = walk(child, [*trail, name])
            else:
                found = walk(child, trail)
            if found is not None:
                return found
        return None

    return walk(root, []) or []


def component_holding(root: ElementTree.Element, filename: str) -> ElementTree.Element | None:
    for component in root.iter(f"{WIX_NS}Component"):
        if filename in [f.get("Name") for f in component.iter(f"{WIX_NS}File")]:
            return component
    return None


class TestDirectoryNesting:
    def test_a_deep_file_stays_under_its_ancestors(self, wxs: ElementTree.Element) -> None:
        """The regression: flattening turns plugins into a top-level folder."""

        component = component_holding(wxs, "qwindows.dll")
        assert component is not None, "qwindows.dll is not packaged"
        chain = directory_chain(wxs, component.get("Directory") or "")
        assert chain == [
            "Programs",
            "QuantsMindAgent",
            "_internal",
            "PySide6",
            "plugins",
            "platforms",
        ], f"qwindows.dll would install into {chain} instead of under _internal/PySide6/plugins"

    def test_a_native_module_stays_under_internal(self, wxs: ElementTree.Element) -> None:
        component = component_holding(wxs, "_pydantic_core.pyd")
        assert component is not None
        assert directory_chain(wxs, component.get("Directory") or "") == [
            "Programs",
            "QuantsMindAgent",
            "_internal",
            "pydantic_core",
        ]

    def test_the_executable_sits_directly_in_the_install_directory(
        self, wxs: ElementTree.Element
    ) -> None:
        component = component_holding(wxs, "QuantsMindAgent.exe")
        assert component is not None
        assert component.get("Directory") == "INSTALLDIR"

    def test_install_directory_is_named_after_the_product(self, wxs: ElementTree.Element) -> None:
        install = next(d for d in wxs.iter(f"{WIX_NS}Directory") if d.get("Id") == "INSTALLDIR")
        assert install.get("Name") == "QuantsMindAgent"


class TestPackageProperties:
    def test_declares_the_product_version(self, wxs: ElementTree.Element) -> None:
        package = wxs.find(f"{WIX_NS}Package")
        assert package is not None and package.get("Version") == SAMPLE_VERSION

    def test_product_identity_is_constant_across_versions(self) -> None:
        """An upgrade only works if every version ships the same UpgradeCode."""

        assert build_msi.UPGRADE_CODE == "{7C4A2B8D-0F11-4E2A-9B3C-5D6E7F8A9B0C}"

    def test_upgrade_is_authored(self, wxs: ElementTree.Element) -> None:
        """A package with no upgrade path installs beside itself forever."""

        assert next(wxs.iter(f"{WIX_NS}MajorUpgrade"), None) is not None

    def test_product_code_is_explicit_and_version_specific(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        first = authoring(bundle, tmp_path)
        package = first.find(f"{WIX_NS}Package")
        assert package is not None
        assert package.get("ProductCode") == release_policy.product_code_for(SAMPLE_VERSION)

        second_path = tmp_path / "next.wxs"
        build_msi.generate_wxs(bundle, "QuantsMindAgent.exe", "9.8.0", "perUser", second_path)
        second = ElementTree.parse(second_path).getroot()
        next_package = second.find(f"{WIX_NS}Package")
        assert next_package is not None
        assert next_package.get("ProductCode") != package.get("ProductCode")

    def test_product_code_is_stable_for_the_same_version(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        first = authoring(bundle, tmp_path).find(f"{WIX_NS}Package")
        second = authoring(bundle, tmp_path).find(f"{WIX_NS}Package")
        assert first is not None and second is not None
        assert first.get("ProductCode") == second.get("ProductCode")

    def test_product_code_is_not_the_upgrade_code(self, wxs: ElementTree.Element) -> None:
        package = wxs.find(f"{WIX_NS}Package")
        assert package is not None
        assert package.get("ProductCode") != package.get("UpgradeCode")

    def test_upgrade_runs_before_cost_initialization(self, wxs: ElementTree.Element) -> None:
        """The default afterInstallValidate schedule blocks rollback in some failures."""

        upgrade = next(wxs.iter(f"{WIX_NS}MajorUpgrade"))
        assert upgrade.get("Schedule") == "afterInstallInitialize"
        assert upgrade.get("AllowSameVersionUpgrades") is None

    def test_per_user_scope_installs_without_elevation(self, wxs: ElementTree.Element) -> None:
        package = wxs.find(f"{WIX_NS}Package")
        assert package is not None and package.get("Scope") == "perUser"
        assert "LocalAppDataFolder" in standard_directory_ids(wxs)
        assert "Programs" in directory_names(wxs)

    def test_per_machine_scope_targets_program_files(self, bundle: Path, tmp_path: Path) -> None:
        root = authoring(bundle, tmp_path, scope="perMachine")
        package = root.find(f"{WIX_NS}Package")
        assert package is not None and package.get("Scope") == "perMachine"
        standards = standard_directory_ids(root)
        assert "ProgramFiles6432Folder" in standards
        assert "LocalAppDataFolder" not in standards


class TestIdentityStability:
    def test_the_same_bundle_produces_the_same_identities(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        """Deterministic identities are what make a later build an upgrade."""

        first = authoring(bundle, tmp_path)
        second = authoring(bundle, tmp_path)
        assert [c.get("Guid") for c in first.iter(f"{WIX_NS}Component")] == [
            c.get("Guid") for c in second.iter(f"{WIX_NS}Component")
        ]

    def test_a_new_version_keeps_the_component_identities(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        """Bumping the version must not orphan every installed component."""

        first = tmp_path / "a.wxs"
        second = tmp_path / "b.wxs"
        build_msi.generate_wxs(bundle, "QuantsMindAgent.exe", "9.7.6", "perUser", first)
        build_msi.generate_wxs(bundle, "QuantsMindAgent.exe", "9.8.0", "perUser", second)
        assert [
            c.get("Guid") for c in ElementTree.parse(first).getroot().iter(f"{WIX_NS}Component")
        ] == [c.get("Guid") for c in ElementTree.parse(second).getroot().iter(f"{WIX_NS}Component")]


class TestComponents:
    def test_no_component_declares_two_key_paths(self, wxs: ElementTree.Element) -> None:
        """WIX0042: exactly one key path per component, or the build fails."""

        for component in wxs.iter(f"{WIX_NS}Component"):
            keypaths = [
                child
                for child in component
                if child.tag in (f"{WIX_NS}File", f"{WIX_NS}RegistryValue")
                and child.get("KeyPath") == "yes"
            ]
            assert len(keypaths) <= 1, f"{component.get('Id')} has {len(keypaths)} key paths"

    def test_every_directory_holding_a_file_has_a_component(self, wxs: ElementTree.Element) -> None:
        known = {d.get("Id") for d in wxs.iter(f"{WIX_NS}Directory")} | {"INSTALLDIR"}
        for component in wxs.iter(f"{WIX_NS}Component"):
            assert component.get("Directory") in known

    def test_the_install_directory_is_removed_on_uninstall(self, wxs: ElementTree.Element) -> None:
        # The removal is expressed by living in a component of INSTALLDIR and
        # carrying RemoveFolder; neither attribute alone identifies it.
        removal_hosts = {
            component.get("Directory")
            for component in wxs.iter(f"{WIX_NS}Component")
            if next(component.iter(f"{WIX_NS}RemoveFolder"), None) is not None
            and next(component.iter(f"{WIX_NS}RemoveFolder")).get("On") == "uninstall"
        }
        assert "INSTALLDIR" in removal_hosts


class TestShortcuts:
    def test_start_menu_and_desktop_shortcuts_exist(self, wxs: ElementTree.Element) -> None:
        targets = {s.get("Directory") for s in wxs.iter(f"{WIX_NS}Shortcut")}
        assert "ProgramMenuFolder" in targets
        assert "DesktopFolder" in targets

    def test_the_shortcut_belongs_to_the_component_that_can_be_removed(
        self, wxs: ElementTree.Element
    ) -> None:
        """A shortcut outside a component is never removed on uninstall."""

        for shortcut in wxs.iter(f"{WIX_NS}Shortcut"):
            parent = next(
                component
                for component in wxs.iter(f"{WIX_NS}Component")
                if shortcut in list(component)
            )
            assert parent.get("Id")


class TestInstalledAppsEntry:
    """The package must be findable in installed apps, under the real GUID."""

    def _uninstall_key(self, root: ElementTree.Element) -> ElementTree.Element:
        values = [
            v
            for v in root.iter(f"{WIX_NS}RegistryValue")
            if v.get("Key", "").startswith(
                "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
            )
        ]
        assert values, "no installed-apps registry entry is authored"
        return values[0]

    def test_the_key_uses_a_formatted_product_code(self, wxs: ElementTree.Element) -> None:
        """``{ProductCode}`` is literal text; ``[ProductCode]`` is resolved."""

        key = self._uninstall_key(wxs).get("Key", "")
        assert key.endswith("\\[ProductCode]")
        assert "{ProductCode}" not in key

    def test_the_entry_is_written_to_the_installing_users_hive(
        self, wxs: ElementTree.Element
    ) -> None:
        """A per-user package must not try to write HKLM."""

        assert self._uninstall_key(wxs).get("Root") == "HKCU"

    def test_the_entry_names_the_product_and_version(self, wxs: ElementTree.Element) -> None:
        names = {
            v.get("Name"): v.get("Value")
            for v in wxs.iter(f"{WIX_NS}RegistryValue")
            if v.get("Key", "").startswith(
                "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
            )
        }
        assert names.get("DisplayName") == "QuantsMind-Agent"
        assert names.get("DisplayVersion") == SAMPLE_VERSION
        assert names.get("UninstallString")


class TestUserDataIsNeverPackaged:
    def test_configuration_files_in_the_bundle_fail_the_build(self, bundle: Path) -> None:
        (bundle / "config.toml").write_text(
            "api_key = 'sk-example-not-a-real-key'\n", encoding="utf-8"
        )
        assert build_msi.find_user_data(bundle) == ["config.toml"]

    def test_credential_files_are_detected_in_nested_directories(self, bundle: Path) -> None:
        nested = bundle / "_internal" / "secrets.json"
        nested.write_text("{}", encoding="utf-8")
        assert build_msi.find_user_data(bundle) == [str(Path("_internal") / "secrets.json")]

    def test_a_clean_bundle_reports_nothing(self, bundle: Path) -> None:
        assert build_msi.find_user_data(bundle) == []


class TestReleaseMode:
    def test_release_mode_refuses_an_inactive_policy(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert build_msi.main(["--onedir", str(tmp_path), "--release"]) == 2
        errors = capsys.readouterr().err
        assert "FAIL" in errors
        assert "distribution_authorized" in errors

    def test_a_release_requires_a_signed_executable(self, bundle: Path) -> None:
        with pytest.raises(signing.SigningError):
            build_msi.verify_executable_signature(bundle / "QuantsMindAgent.exe", None)


class TestBundleDiscovery:
    def test_accepts_the_folder_containing_the_executable(self, bundle: Path) -> None:
        found, exe = build_msi.locate_bundle(bundle)
        assert found == bundle
        assert exe == "QuantsMindAgent.exe"

    def test_accepts_the_pyinstaller_parent_folder(self, bundle: Path) -> None:
        found, exe = build_msi.locate_bundle(bundle.parent)
        assert found == bundle
        assert exe == "QuantsMindAgent.exe"

    def test_reports_nothing_when_there_is_no_bundle(self, tmp_path: Path) -> None:
        assert build_msi.locate_bundle(tmp_path) == (None, "")
