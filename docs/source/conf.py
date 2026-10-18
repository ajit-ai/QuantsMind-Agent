"""Sphinx configuration for the QuantsMind-Agent documentation.

The documentation is written in reStructuredText and builds to
``docs/build/html/index.html``. The build is offline and deterministic: no
theme downloads, no intersphinx network access, no API-key requirement.

Build it with::

    scripts\\build-docs.ps1          # Windows
    python scripts/build_docs.py     # any platform
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from quantsmind_agent.version import __version__  # noqa: E402

# -- Project information ---------------------------------------------------
project = "QuantsMind-Agent"
author = "QuantsMind"
copyright = f"2026, {author}"
version = __version__
release = __version__

#: The private repository is never published; these stay empty so no external
#: documentation is fetched during the build.
github_url = "https://github.com/ajit-ai/QuantsMind-Agent"

# -- General configuration -------------------------------------------------
extensions = [
    "sphinxcontrib.mermaid",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.todo",
]

templates_path = ["_templates"]
exclude_patterns: list[str] = []
source_suffix = ".rst"
master_doc = "index"
language = "en"
pygments_style = "friendly"

#: ``TODO`` entries render as visible notes instead of silent omissions.
todo_include_todos = True

intersphinx_mapping: dict[str, str] = {}

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}

# -- HTML output -----------------------------------------------------------
html_theme = "sphinx_rtd_theme"
html_title = f"{project} {version}"
html_short_title = project
html_static_path = ["_static"]
html_last_updated_fmt = "%Y-%m-%d"
html_show_sourcelink = True
html_copy_source = False

html_theme_options = {
    "navigation_depth": 3,
    "collapse_navigation": False,
    "style_external_links": True,
    "titles_only": False,
}

#: Keep the "built on" footer honest.
html_context = {
    "display_github": False,
    "build_date": datetime.now(tz=UTC).strftime("%Y-%m-%d"),
}

# -- Warnings are errors ---------------------------------------------------
#: CI fails the documentation build on any warning.
suppress_warnings: list[str] = []
nitpicky = False
