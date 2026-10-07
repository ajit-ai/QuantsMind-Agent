"""Normalise reStructuredText in docs/source.

Two mechanical fixes that are tedious and error-prone by hand:

1. **Title adornments.** A section underline or overline must be at least as
   long as its title. Sphinx treats a short adornment as a warning, and the
   documentation build is configured to fail on warnings.

2. **Simple and grid tables.** Every cell must fit inside the column border.
   Rather than eyeballing the borders, the widths are recomputed from the widest
   cell in each column.

Run with ``--check`` to verify without writing (exit code 1 if changes are
needed), which is how CI keeps this from drifting.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "docs" / "source"

#: Characters a border or adornment line may be built from.
BORDER_CHARS = set("=-~^\"'`#*+")

#: Two or more spaces separate columns in a simple RST table.
_SPACES = re.compile(r"[ \t]{2,}")

#: Leading indentation that is not part of a cell's text.
_INDENT = re.compile(r"^[ \t]*")


@dataclass
class Change:
    """One normalisation applied to (or needed by) a file."""

    path: Path
    line: int
    kind: str


def _is_border(line: str) -> bool:
    """True for an adornment or table border, ignoring inter-column gaps."""

    stripped = _SPACES.sub("", line).strip()
    if not stripped:
        return False
    return all(char in BORDER_CHARS for char in stripped)


def _at_top_level(line: str) -> bool:
    """True when ``line`` starts in column 1.

    Indented lines belong to literal blocks -- code samples and ASCII diagrams --
    which this fixer must never rewrite.
    """

    return bool(line) and line[0] not in " \t"


def _is_multi_column_border(line: str) -> bool:
    """A table border separates at least two columns.

    A single-column border (a section adornment) has no gap, which is what
    keeps this fixer from rewriting headings as tables.
    """

    return _is_border(line) and _at_top_level(line) and bool(_SPACES.search(line))


def _title_width(line: str) -> int:
    return len(line.rstrip())


def repair_adornments(lines: list[str], changes: list[Change], path: Path) -> list[str]:
    """Restore adornment lines that were blanked out."""

    out: list[str] = []
    for offset, line in enumerate(lines):
        if len(line) >= 3 and not line.strip():
            char = "=" if offset + 1 < len(lines) else "-"
            out.append(char * len(line.rstrip()))
            changes.append(Change(path, offset + 1, "repair"))
            continue
        out.append(line)
    return out


def fix_titles(lines: list[str], changes: list[Change], path: Path) -> list[str]:
    """Pad every overline or underline that is shorter than its title."""

    out = list(lines)
    for index in range(1, len(lines)):
        if not (_is_border(lines[index]) and _at_top_level(lines[index])):
            continue
        title = lines[index - 1]
        if not title.strip() or _is_border(title):
            continue
        needed = _title_width(title)
        char = lines[index].strip()[0]
        if len(lines[index].rstrip()) < needed:
            out[index] = char * needed
            kind = (
                "underline"
                if index + 1 >= len(lines) or not lines[index + 1].strip()
                else "overline"
            )
            changes.append(Change(path, index + 1, kind))
    return out


def _split_row(line: str, count: int) -> list[str] | None:
    """Split a table row into ``count`` cells, or return ``None``."""

    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = [cell.strip() for cell in _SPACES.split(_INDENT.sub("", stripped)) if cell.strip()]
    return cells if len(cells) == count else None


def _table_block(lines: list[str], start: int) -> list[str] | None:
    """Return the simple-table block starting at ``start``, else ``None``."""

    if not _is_multi_column_border(lines[start]):
        return None
    if start + 2 >= len(lines):
        return None
    if _is_border(lines[start + 1]) or not _is_multi_column_border(lines[start + 2]):
        return None

    end = start + 3
    while end < len(lines) and lines[end].strip():
        end += 1
    block = lines[start:end]
    return block if _is_border(block[-1]) else None


def _column_widths(block: list[str]) -> list[int] | None:
    count = len(_SPACES.split(_INDENT.sub("", block[0].strip())))
    if count < 2:
        return None

    widths = [0] * count
    for line in block:
        if _is_border(line):
            continue
        cells = _split_row(line, count)
        if cells is None:
            # A continuation row (an empty first column) wraps the previous
            # cell; its length counts towards the last column.
            stripped = _INDENT.sub("", line.rstrip())
            if not stripped:
                continue
            if len(stripped.split()) == 0:
                continue
            widths[-1] = max(widths[-1], len(stripped))
            continue
        for position, cell in enumerate(cells):
            widths[position] = max(widths[position], len(cell))
    return widths


def _render_table(block: list[str], widths: list[int]) -> list[str]:
    border = "  ".join("=" * width for width in widths)
    rendered = [border]
    for line in block[1:]:
        if _is_border(line):
            rendered.append(border)
            continue
        cells = _split_row(line, len(widths))
        if cells is None:
            rendered.append(line)
            continue
        rendered.append(
            "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()
        )
    return rendered


def fix_tables(lines: list[str], changes: list[Change], path: Path) -> list[str]:
    """Recompute simple-table column widths from their contents."""

    out = list(lines)
    index = 0
    while index < len(lines) - 2:
        block = _table_block(lines, index)
        if block is None:
            index += 1
            continue

        widths = _column_widths(block)
        if widths is not None:
            needed = sum(widths) + 2 * (len(widths) - 1)
            borders = [line for line in block if _is_border(line)]
            # Every border row must span the full table width, otherwise
            # docutils reports the table as malformed.
            stale = any(len(line.rstrip()) != needed for line in borders)
            if stale or needed != len(block[0].rstrip()):
                rebuilt = _render_table(block, widths)
                for offset, text in enumerate(rebuilt):
                    out[index + offset] = text
                changes.append(Change(path, index + 1, "table"))
        index += len(block)
    return out


def process(path: Path, check: bool) -> list[Change]:
    """Normalise one file and return the changes made (or needed)."""

    original = path.read_text(encoding="utf-8").splitlines()
    changes: list[Change] = []

    lines = repair_adornments(original, changes, path)
    lines = fix_titles(lines, changes, path)
    lines = fix_tables(lines, changes, path)

    if changes and not check:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report needed changes without writing them.",
    )
    args = parser.parse_args(argv)

    total = 0
    for path in sorted(SOURCE.rglob("*.rst")):
        for change in process(path, args.check):
            verb = "needs" if args.check else "fixed"
            print(f"{path.relative_to(REPO_ROOT)}:{change.line}: {verb} {change.kind}")
            total += 1

    if args.check and total:
        print(f"{total} documentation normalisation(s) pending.", file=sys.stderr)
        return 1
    print(f"{'Would fix' if args.check else 'Fixed'} {total} documentation issue(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
