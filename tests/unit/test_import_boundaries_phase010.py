"""Phase 0.10 import-boundary tests.

The core must stay importable and testable without Qt, and navigation surfaces
must reach the filesystem through the workspace services rather than reading,
writing or walking it themselves. The editor document model owns its own file
I/O; those paths are covered by the editor tests.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "quantsmind_agent"
NAVIGATION_ROOTS = ("gui/shell", "gui/conversation")
FILESYSTEM_MODULES = {"glob", "os", "shutil", "subprocess", "tempfile"}


def _modules(root: Path) -> Iterator[Path]:
    yield from sorted(root.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def _module_level_imports(tree: ast.Module) -> Iterator[str]:
    """Imports executed merely by importing the module.

    The desktop entry point imports Qt lazily inside its entry functions on
    purpose, so a headless process can import every core module; function-local
    imports are therefore not module-level coupling.
    """

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_core_modules_never_import_qt_at_module_level() -> None:
    offenders: list[str] = []
    for path in _modules(PACKAGE_ROOT):
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[0] == "gui":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module in _module_level_imports(tree):
            if module == "PySide6" or module.startswith("PySide6."):
                offenders.append(f"{relative}: {module}")
    assert offenders == []


def test_navigation_panels_do_not_import_filesystem_modules() -> None:
    offenders: list[str] = []
    for name in NAVIGATION_ROOTS:
        for path in _modules(PACKAGE_ROOT / name):
            relative = path.relative_to(PACKAGE_ROOT)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for module in _imported_modules(tree):
                if module.split(".")[0] in FILESYSTEM_MODULES:
                    offenders.append(f"{relative}: {module}")
    assert offenders == []
