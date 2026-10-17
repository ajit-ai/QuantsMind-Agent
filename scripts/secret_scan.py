"""Pre-commit guard: fail if a credential-shaped string is about to be committed.

Scans the files git would add and reports anything that looks like a live key.
Complements ``.gitignore`` and the redaction filter -- those prevent leaks at
runtime; this prevents one at commit time.

Usage::

    python scripts/secret_scan.py            # scan files git would add
    python scripts/secret_scan.py --staged   # scan the staged index
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Patterns that indicate a credential rather than an example.
PATTERNS: dict[str, re.Pattern[str]] = {
    "openai-style key": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._\-]{24,}"),
    "anthropic-style key": re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}"),
    "assigned api key": re.compile(
        r"""(?:api[_-]?key|secret|password|token)\s*[:=]\s*["'][A-Za-z0-9._\-]{20,}["']""",
        re.IGNORECASE,
    ),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}

#: Substrings that mark a deliberate, non-secret example.
SAFE_MARKERS = ("sk-test-", "sk-***", "sk-your", "example", "placeholder", "not-set")


def _git(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, cwd=Path.cwd()
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def candidates(staged: bool) -> list[Path]:
    listing = (
        _git("diff", "--cached", "--name-only", "--diff-filter=ACM")
        if staged
        else _git("ls-files", "--others", "--exclude-standard")
    )
    paths = [Path(entry) for entry in listing]
    existing = [path for path in paths if path.is_file()]
    # Always include tracked modifications, not just new files.
    for entry in _git("diff", "--name-only", "--diff-filter=ACM"):
        path = Path(entry)
        if path.is_file() and path not in existing:
            existing.append(path)
    return existing


def looks_binary(path: Path, probe: int = 8192) -> bool:
    """Whether a file is a compiled binary rather than text.

    Packaging output puts Qt's DLLs and other compiled libraries in the working
    tree, and those embed readable ASCII such as ``private key block``. Decoding
    them with ``errors="ignore"`` produces text a credential pattern matches, so
    a build directory turns the scanner into a machine that always complains.
    A scanner that cries wolf on every build is a scanner people stop running.

    A NUL byte in the first few kilobytes is the standard heuristic, and no text
    file this project cares about contains one.
    """

    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(probe)
    except OSError:
        return True


def scan(paths: list[Path]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        if looks_binary(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            if any(marker in lowered for marker in SAFE_MARKERS):
                continue
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path}:{number}: {label}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Scan the staged index instead.")
    args = parser.parse_args(argv)

    paths = candidates(args.staged)
    findings = scan(paths)

    if findings:
        print("Credential-shaped strings found:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\nRemove the value, or mark the line as an example. "
            "If a real key was committed, rotate it.",
            file=sys.stderr,
        )
        return 1

    print(f"No credential-shaped strings in {len(paths)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
