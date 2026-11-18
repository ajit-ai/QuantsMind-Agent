"""v1.7 tests: plugin scaffold + template sharing."""

from __future__ import annotations

from pathlib import Path

from quantsmind_agent.services.plugin_scaffold import scaffold_plugin
from quantsmind_agent.services.template_sharing import (
    export_template_bundle,
    import_template_bundle,
)


def test_scaffold(tmp_path: Path) -> None:
    dest = scaffold_plugin(tmp_path / "plug", "demo")
    assert (dest / "manifest.json").exists()
    assert (dest / "demo.py").exists()
    import json

    assert json.loads((dest / "manifest.json").read_text())["id"] == "demo"


def test_template_roundtrip(tmp_path: Path) -> None:
    out = export_template_bundle(
        tmp_path / "bundle.json",
        profiles=[{"profile_id": "p1"}],
        memories=[{"title": "t"}, {"title": "Leak", "body": "api_key = x"}],
    )
    data = import_template_bundle(out)
    assert data["profiles"] == [{"profile_id": "p1"}]
    assert len(data["memories"]) == 1  # secret entry dropped
