"""Tests for the fail-closed Windows signing architecture."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import signing  # noqa: E402

THUMBPRINT = "A" * 40
TIMESTAMP = "https://timestamp.example.test/rfc3161"


@pytest.fixture
def signtool(tmp_path: Path) -> Path:
    path = tmp_path / "signtool.exe"
    path.write_bytes(b"not executed")
    return path


def environment(signtool: Path | None = None) -> dict[str, str]:
    env = {
        signing.CERTIFICATE_ENV: THUMBPRINT,
        signing.TIMESTAMP_ENV: TIMESTAMP,
    }
    if signtool is not None:
        env[signing.SIGNTOOL_ENV] = str(signtool)
    return env


class TestSigningConfiguration:
    def test_explicit_thumbprint_and_timestamp_are_accepted(self, signtool: Path) -> None:
        config = signing.load_signing_config(environment(signtool))
        assert config.certificate_thumbprint == THUMBPRINT
        assert config.timestamp_url == TIMESTAMP
        assert config.signtool == str(signtool)

    def test_missing_certificate_is_refused(self, signtool: Path) -> None:
        env = environment(signtool)
        del env[signing.CERTIFICATE_ENV]
        with pytest.raises(signing.SigningError, match=signing.CERTIFICATE_ENV):
            signing.load_signing_config(env)

    def test_automatic_certificate_selection_is_not_attempted(self) -> None:
        source = (REPO / "scripts" / "signing.py").read_text(encoding="utf-8")
        assert "CurrentUser\\My" not in source
        assert '"/a"' not in source

    @pytest.mark.parametrize("value", ["", "abc", "G" * 40, "A" * 39, "A" * 41])
    def test_malformed_thumbprints_are_refused(self, value: str) -> None:
        with pytest.raises(signing.SigningError):
            signing.normalize_thumbprint(value)

    def test_colons_and_whitespace_in_a_thumbprint_are_normalized(self) -> None:
        assert signing.normalize_thumbprint(" ".join(["aa"] * 20)) == THUMBPRINT

    @pytest.mark.parametrize("url", ["http://timestamp.test", "timestamp.test", "https://"])
    def test_untrusted_timestamp_urls_are_refused(self, url: str) -> None:
        with pytest.raises(signing.SigningError, match=signing.TIMESTAMP_ENV):
            signing.validate_timestamp_url(url)

    def test_a_missing_configured_signtool_is_refused(self, tmp_path: Path) -> None:
        env = environment(tmp_path / "missing.exe")
        with pytest.raises(signing.SigningError, match=signing.SIGNTOOL_ENV):
            signing.load_signing_config(env, require_tool=False)


class TestCommands:
    def test_sign_command_uses_the_authorized_thumbprint_and_timestamp(
        self, tmp_path: Path, signtool: Path
    ) -> None:
        artifact = tmp_path / "artifact.exe"
        artifact.write_bytes(b"artifact")
        config = signing.load_signing_config(environment(signtool))
        command = signing.sign_command(artifact, config)
        assert command[:3] == [str(signtool), "sign", "/sha1"]
        assert command[3] == THUMBPRINT
        assert "/tr" in command and TIMESTAMP in command
        assert command[command.index("/td") + 1] == "sha256"
        assert command[command.index("/fd") + 1] == "sha256"
        assert command[-1] == str(artifact)
        assert "/a" not in command

    def test_signing_a_missing_artifact_is_refused(self, signtool: Path, tmp_path: Path) -> None:
        config = signing.load_signing_config(environment(signtool))
        with pytest.raises(signing.SigningError, match="missing file"):
            signing.sign_command(tmp_path / "missing.exe", config)

    def test_dry_run_does_not_execute(self, tmp_path: Path, signtool: Path) -> None:
        artifact = tmp_path / "artifact.exe"
        artifact.write_bytes(b"artifact")
        config = signing.load_signing_config(environment(signtool))
        command = signing.sign_file(artifact, config, dry_run=True)
        assert command[0] == str(signtool)

    def test_verify_command_checks_the_machine_policy(self, tmp_path: Path, signtool: Path) -> None:
        artifact = tmp_path / "artifact.exe"
        artifact.write_bytes(b"artifact")
        command = signing.verify_command(artifact, str(signtool))
        assert command == [str(signtool), "verify", "/pa", "/all", "/v", str(artifact)]

    def test_verifying_a_missing_artifact_is_refused(self, tmp_path: Path, signtool: Path) -> None:
        with pytest.raises(signing.SigningError, match="missing file"):
            signing.verify_command(tmp_path / "missing.exe", str(signtool))


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell is unavailable")
class TestVerifierScript:
    def test_the_verifier_parses(self) -> None:
        script = REPO / "scripts" / "verify_windows_signature.ps1"
        command = (
            "$e = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}', "
            "[ref]$null, [ref]$e); "
            "if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_the_verifier_never_signs(self) -> None:
        source = (REPO / "scripts" / "verify_windows_signature.ps1").read_text(encoding="utf-8")
        assert "signtool" not in source
        assert "Set-AuthenticodeSignature" not in source
        assert "Remove-Item" not in source
        assert "Copy-Item" not in source


class TestCommandLine:
    def test_check_fails_without_an_explicit_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv(signing.CERTIFICATE_ENV, raising=False)
        monkeypatch.delenv(signing.TIMESTAMP_ENV, raising=False)
        assert signing.main(["check"]) == 1
        assert "FAIL" in capsys.readouterr().err

    def test_dry_run_prints_the_command(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        signtool: Path,
    ) -> None:
        artifact = tmp_path / "artifact.exe"
        artifact.write_bytes(b"artifact")
        monkeypatch.setenv(signing.CERTIFICATE_ENV, THUMBPRINT)
        monkeypatch.setenv(signing.TIMESTAMP_ENV, TIMESTAMP)
        monkeypatch.setenv(signing.SIGNTOOL_ENV, str(signtool))
        assert signing.main(["sign", str(artifact), "--dry-run"]) == 0
        assert THUMBPRINT in capsys.readouterr().out
