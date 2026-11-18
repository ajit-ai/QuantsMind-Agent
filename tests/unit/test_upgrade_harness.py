"""Safety checks for the destructive MSI upgrade harness."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify_msi_upgrade.ps1"


class TestHarnessSource:
    def test_harness_exists(self) -> None:
        assert SCRIPT.is_file()

    @pytest.mark.parametrize(
        "guard",
        [
            "-AuthorizeDestructiveInstall",
            "-EvidenceDir is required with -Execute",
            "no existing QuantsMind-Agent installation",
            "refusing to modify this host",
            "ProductCodes differ",
            "UpgradeCode is stable",
            "upgrade target is newer",
            "ExpectedThumbprint",
            "QM_SIGNING_CERT_SHA1",
        ],
    )
    def test_required_fail_closed_guards_are_present(self, guard: str) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        assert guard in source

    def test_plan_is_the_default(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        assert "if (-not $Execute)" in source
        assert "no installs were performed" in source


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell is unavailable")
class TestHarnessInvocation:
    def test_the_script_parses(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"[void][ScriptBlock]::Create((Get-Content -Raw -LiteralPath '{SCRIPT}'))",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_missing_artifacts_fail_before_any_install(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(SCRIPT),
                "-FromMsi",
                str(tmp_path / "missing-old.msi"),
                "-ToMsi",
                str(tmp_path / "missing-new.msi"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "nothing was installed" in result.stdout
        assert "preflight failed" in result.stdout
