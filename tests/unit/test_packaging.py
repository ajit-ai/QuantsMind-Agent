"""Tests for the executable packaging configuration.

The packaged build cannot be exercised inside the unit suite -- it takes minutes
and needs a native runner -- so these tests cover the parts that can go wrong
silently: a spec that stops excluding development tooling, a smoke test that
looks for the wrong executable or the wrong Qt plugin, and build metadata that
cannot be traced back to a platform.
"""

from __future__ import annotations

import ast
import platform
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import smoke_test_executable as smoke  # noqa: E402

SPEC = REPO / "packaging" / "quantsmind-agent.spec"


@pytest.fixture(scope="module")
def spec_source() -> str:
    assert SPEC.is_file(), "packaging/quantsmind-agent.spec is missing"
    return SPEC.read_text(encoding="utf-8")


class TestSpecFile:
    def test_spec_parses(self, spec_source: str) -> None:
        ast.parse(spec_source)

    def test_entry_point_is_the_stable_pyinstaller_shim(self, spec_source: str) -> None:
        # Using app.main directly would drag argparse/CLI state into the bundle
        # bootstrap and make the entry point less stable for PyInstaller.
        assert 'scripts" / "pyinstaller_entry.py' in spec_source

    def test_src_is_on_the_analysis_path(self, spec_source: str) -> None:
        # Without this the bundle cannot import quantsmind_agent at all.
        assert 'pathex=[str(REPO / "src")]' in spec_source

    @pytest.mark.parametrize(
        "excluded",
        ["pytest", "_pytest", "mypy", "ruff", "sphinx", "tkinter"],
    )
    def test_development_tooling_is_excluded(self, spec_source: str, excluded: str) -> None:
        """A dev dependency in a shipped bundle is dead weight at best."""

        tree = ast.parse(spec_source)
        excludes: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXCLUDES" for t in node.targets
            ):
                excludes = [el.value for el in node.value.elts]  # type: ignore[attr-defined]
        assert excluded in excludes

    def test_unused_qt_modules_are_excluded(self, spec_source: str) -> None:
        assert "PySide6.QtWebEngineCore" in spec_source
        assert "PySide6.QtQml" in spec_source

    def test_keyring_backends_are_collected(self, spec_source: str) -> None:
        # keyring picks a backend through entry points, which static analysis
        # cannot see. Missing these means no usable credential store on a
        # machine whose backend is not the default.
        assert 'collect_submodules("keyring.backends")' in spec_source

    def test_gui_builds_a_windowed_binary_without_a_console(self, spec_source: str) -> None:
        assert "console=False" in spec_source

    def test_reads_the_version_without_importing_the_package(self, spec_source: str) -> None:
        # Building must not depend on the package importing cleanly.
        assert "import quantsmind_agent" not in spec_source

    def test_macos_bundle_declares_folder_usage(self, spec_source: str) -> None:
        """macOS needs usage descriptions or folder access is denied."""

        for key in (
            "NSDesktopFolderUsageDescription",
            "NSDocumentsFolderUsageDescription",
            "NSDownloadsFolderUsageDescription",
        ):
            assert key in spec_source


class TestSmokeTestConfiguration:
    @pytest.mark.parametrize("system", ["Windows", "Linux", "Darwin"])
    def test_every_supported_system_has_a_layout(self, system: str) -> None:
        assert system in smoke.LAYOUT
        assert smoke.LAYOUT[system][1]

    @pytest.mark.parametrize("system", ["Windows", "Linux", "Darwin"])
    def test_every_supported_system_has_a_platform_plugin(self, system: str) -> None:
        assert system in smoke.PLATFORM_PLUGIN

    def test_macos_layout_points_inside_the_bundle(self) -> None:
        # macOS executables live in Contents/MacOS, not at the top level.
        assert "MacOS/" in smoke.LAYOUT["Darwin"][1]

    def test_locate_returns_none_when_nothing_is_built(self, tmp_path: Path) -> None:
        assert smoke.locate(tmp_path) is None

    def test_locate_finds_a_built_executable(self, tmp_path: Path) -> None:
        system = __import__("platform").system()
        if system not in smoke.LAYOUT:
            pytest.skip(f"no layout defined for {system}")
        root, exe = smoke.LAYOUT[system]
        target = tmp_path / root / exe
        target.parent.mkdir(parents=True)
        target.write_text("", encoding="utf-8")
        assert smoke.locate(tmp_path) == target


class TestSmokeTestOutputDecoding:
    """A windowed build writes the ANSI code page; decoding must cope."""

    def test_utf8_output_decodes(self) -> None:
        assert smoke.decode_output(b"ready       : yes") == "ready       : yes"

    def test_ansi_encoded_path_is_not_mangled(self) -> None:
        # A cp1252-encoded path decoded as UTF-8 turns every non-ASCII
        # character into U+FFFD, which silently broke the non-ASCII workspace
        # check on the Windows runner.
        raw = "workspace   : C:/tmp/ünïcode".encode("cp1252")
        assert smoke.decode_output(raw) == "workspace   : C:/tmp/ünïcode"

    def test_utf8_path_is_not_mangled(self) -> None:
        raw = "workspace   : /tmp/ünïcode".encode()
        assert smoke.decode_output(raw) == "workspace   : /tmp/ünïcode"

    def test_empty_output_is_empty_text(self) -> None:
        assert smoke.decode_output(b"") == ""


class TestSmokeTestExecutableDiscovery:
    """A good build must not be reported as missing because of its layout."""

    def test_finds_an_executable_at_an_unexpected_depth(self, tmp_path: Path) -> None:
        system = platform.system()
        if system not in smoke.LAYOUT:
            pytest.skip(f"no layout defined for {system}")
        nested = tmp_path / "dist" / "some" / "other" / "place" / smoke.EXE_NAME[system]
        nested.parent.mkdir(parents=True)
        nested.write_text("", encoding="utf-8")
        found = smoke.locate(tmp_path / "dist")
        assert found is not None, "discovery must not depend on the documented layout"
        assert found.name == smoke.EXE_NAME[system]

    def test_prefers_the_shallowest_match(self, tmp_path: Path) -> None:
        system = platform.system()
        if system not in smoke.LAYOUT:
            pytest.skip(f"no layout defined for {system}")
        deep = tmp_path / "a" / "b" / "c" / smoke.EXE_NAME[system]
        deep.parent.mkdir(parents=True)
        deep.write_text("", encoding="utf-8")
        shallow = tmp_path / smoke.EXE_NAME[system]
        shallow.write_text("", encoding="utf-8")
        assert smoke.locate(tmp_path) == shallow


class TestPlatformMetadata:
    def test_classifiers_do_not_claim_unverified_platforms(self) -> None:
        """The wheel metadata must not advertise what we cannot evidence."""

        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert "Operating System :: Microsoft :: Windows :: Windows 11" in text
        assert "Operating System :: POSIX :: Linux" in text
        # BSD has no wheels for the Qt dependency; claiming it would be a lie.
        for claim in ("Operating System :: BSD", "Operating System :: POSIX :: BSD"):
            assert claim not in text

    def test_requires_python_matches_the_tested_floor(self) -> None:
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert 'requires-python = ">=3.11"' in text
