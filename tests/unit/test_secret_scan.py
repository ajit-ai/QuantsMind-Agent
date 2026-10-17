"""Tests for the credential scanner.

The scanner's value is entirely in being believed. Two ways to lose that are
tested here: reporting a credential that is not there, and missing one that is.

The false-positive case became real during packaging: Qt's DLLs contain readable
ASCII such as ``private key block``, so a build directory in the working tree made
the scanner fail on every run. A gate that always fails is a gate that gets
skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import secret_scan  # noqa: E402


class TestRealCredentials:
    # Assembled rather than written out: a literal credential-shaped line in this
    # file would trip the scanner that runs over the repository, which is the
    # same signal a real key would give.
    @pytest.mark.parametrize(
        "line",
        [
            "OPENROUTER_API_KEY=" + "sk-" + "abc123def456ghi789jkl012mno345pqr",
            "export OPENAI_API_KEY=" + "sk-" + "9f8e7d6c5b4a3210zyxwvutsrqponmlkj",
        ],
    )
    def test_a_live_looking_key_is_reported(self, tmp_path: Path, line: str) -> None:
        path = tmp_path / "config.txt"
        path.write_text(line + "\n", encoding="utf-8")
        assert secret_scan.scan([path])


class TestMarkedExamples:
    @pytest.mark.parametrize(
        "line",
        [
            "OPENROUTER_API_KEY=sk-test-not-real",
            "api_key = 'sk-your-key-here'",
            "OPENAI_API_KEY=sk-***",
            "ANTHROPIC_API_KEY=placeholder",
        ],
    )
    def test_documented_examples_are_not_reported(self, tmp_path: Path, line: str) -> None:
        """A documented example is documentation, not a leak."""

        path = tmp_path / "README.md"
        path.write_text(line + "\n", encoding="utf-8")
        assert secret_scan.scan([path]) == []


class TestBinaryFiles:
    def test_a_compiled_library_is_not_scanned(self, tmp_path: Path) -> None:
        """Qt ships the ASCII text "private key block" inside its DLLs.

        Decoding a binary with errors ignored turns that into a line the
        patterns match, so every packaged build would report a credential.
        """

        path = tmp_path / "Qt6Network.dll"
        path.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00private key block\x00\x00\x01\x02")
        assert secret_scan.looks_binary(path)
        assert secret_scan.scan([path]) == []

    def test_text_files_are_still_scanned(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_text("hello\n", encoding="utf-8")
        assert not secret_scan.looks_binary(path)

    def test_an_unreadable_file_does_not_raise(self, tmp_path: Path) -> None:
        # A path that does not exist stands in for a permission error: the scan
        # must skip it rather than crash the gate.
        assert secret_scan.looks_binary(tmp_path / "nope.txt")


class TestTheProjectItselfIsClean:
    def test_no_credential_shaped_strings_in_the_repository(self) -> None:
        findings = secret_scan.scan(secret_scan.candidates(staged=False))
        assert findings == [], "credential-shaped strings found:\n" + "\n".join(findings)
