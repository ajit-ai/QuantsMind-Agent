"""Repository credential-shape guard.

The default scan covers the complete repository source: every tracked file and
every untracked, non-ignored file. ``--staged`` covers only files staged for
commit. Complements ``.gitignore`` and the redaction filter -- those prevent leaks at
runtime; this prevents one at rest.

Usage::

    python scripts/secret_scan.py            # scan all tracked source + untracked files
    python scripts/secret_scan.py --staged   # scan only files staged for commit

The default mode is what CI runs; ``--staged`` remains a pre-commit convenience.
Gitignored build output is excluded by git's own ignore machinery for untracked
files, and no repository source directory is skipped by this scanner.
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


def _git_z(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], capture_output=True, check=False, cwd=Path.cwd())
    return [
        entry.decode("utf-8", errors="surrogateescape")
        for entry in result.stdout.split(b"\0")
        if entry
    ]


def candidates(staged: bool) -> list[Path]:
    """Return the files the scanner must inspect.

    The default scans the complete repository source: every file git tracks,
    plus untracked files that are not ignored. A clean checkout therefore scans
    the same files a dirty working tree scans; it does not silently degrade to
    scanning only what differs from HEAD.

    ``--staged`` keeps the pre-commit behaviour and scans only the files staged
    for commit, which is useful before a commit but is not the CI gate.
    """
    if staged:
        listing = _git_z("diff", "--cached", "--name-only", "--diff-filter=ACM", "-z")
    else:
        listing = _git_z("ls-files", "-z", "--cached") + _git_z(
            "ls-files", "-z", "--others", "--exclude-standard"
        )
    existing: list[Path] = []
    seen: set[str] = set()
    for entry in listing:
        path = Path(entry)
        key = path.as_posix()
        if path.is_file() and key not in seen:
            seen.add(key)
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
