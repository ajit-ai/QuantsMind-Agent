"""Create a disposable project for the manual acceptance test QM-V01-CODE-001.

Run it with::

    python scripts\\make_acceptance_fixture.py

It writes a small, self-contained Python project to ``.acceptance-fixture\\``
(a git-ignored directory) that the agent can be pointed at during Windows 11
acceptance testing. The project is intentionally trivial and disposable: the
acceptance procedure only reads files.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

FILES: dict[str, str] = {
    "README.md": (
        "# Widget Factory\n"
        "\n"
        "A tiny example project used to validate QuantsMind-Agent.\n"
        "\n"
        "The entry point is `widget_factory.main:main`; run it with\n"
        "`python -m widget_factory`.\n"
    ),
    "pyproject.toml": (
        "[project]\n"
        'name = "widget-factory"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        "\n"
        "[project.scripts]\n"
        'widget-factory = "widget_factory.main:main"\n'
    ),
    "widget_factory/__init__.py": (
        '"""Widget factory package."""\n'
        "\n"
        "from widget_factory.config import Settings, load_settings\n"
        "\n"
        '__all__ = ["Settings", "load_settings"]\n'
    ),
    "widget_factory/__main__.py": (
        '"""Module entry point."""\n'
        "\n"
        "from widget_factory.main import main\n"
        "\n"
        "raise SystemExit(main())\n"
    ),
    "widget_factory/config.py": (
        '"""Configuration loading."""\n'
        "\n"
        "from dataclasses import dataclass\n"
        "\n"
        'DEFAULT_HOST = "localhost"\n'
        "DEFAULT_PORT = 8080\n"
        "\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Settings:\n"
        '    """Runtime settings."""\n'
        "\n"
        "    host: str\n"
        "    port: int\n"
        "\n"
        "\n"
        "def load_settings() -> Settings:\n"
        '    """Return the default settings."""\n'
        "    return Settings(host=DEFAULT_HOST, port=DEFAULT_PORT)\n"
    ),
    "widget_factory/factory.py": (
        '"""Widget creation."""\n'
        "\n"
        "from widget_factory.config import Settings\n"
        "\n"
        "\n"
        "class Widget:\n"
        '    """A manufactured widget."""\n'
        "\n"
        "    def __init__(self, name: str, size: int) -> None:\n"
        "        self.name = name\n"
        "        self.size = size\n"
        "\n"
        "    def __repr__(self) -> str:\n"
        '        return f"Widget(name={self.name!r}, size={self.size})"\n'
        "\n"
        "\n"
        "def build_widget(name: str, size: int = 1) -> Widget:\n"
        '    """Create a widget."""\n'
        "    return Widget(name=name, size=size)\n"
        "\n"
        "\n"
        "def describe(widget: Widget) -> str:\n"
        '    """Return a human description of a widget."""\n'
        '    return f"{widget.name} of size {widget.size}"\n'
    ),
    "widget_factory/main.py": (
        '"""Application entry point."""\n'
        "\n"
        "from widget_factory.config import load_settings\n"
        "from widget_factory.factory import build_widget, describe\n"
        "\n"
        "\n"
        "def main() -> int:\n"
        '    """Start the widget factory."""\n'
        "    settings = load_settings()\n"
        '    widget = build_widget("demo")\n'
        "    print(describe(widget))\n"
        '    print(f"listening on {settings.host}:{settings.port}")\n'
        "    return 0\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n"
    ),
    "docs/architecture.md": (
        "# Architecture\n"
        "\n"
        "`main.py` wires `config.load_settings()` and `factory.build_widget()`\n"
        "together. `config` holds settings, `factory` holds domain objects.\n"
    ),
    ".gitignore": "__pycache__/\n*.pyc\n",
}


def create_fixture(target: Path, *, force: bool = False) -> Path:
    """Write the acceptance fixture into ``target``."""

    if target.exists():
        if not force:
            raise SystemExit(f"{target} already exists. Re-run with --force to recreate it.")
        shutil.rmtree(target)

    for relative, content in FILES.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        default=".acceptance-fixture/widget-factory",
        help="Where to write the disposable project.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete and recreate an existing fixture.",
    )
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    create_fixture(target, force=args.force)
    print(f"Created the acceptance fixture at {target}")
    print(f"Files: {len(FILES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
