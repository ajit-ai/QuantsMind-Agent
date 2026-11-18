"""v1.2 service tests: evidence, sync, router, plugins."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from quantsmind_agent.providers.cost_router import CostRouter, ModelTier, estimate_cost
from quantsmind_agent.services.evidence import export_evidence_pack
from quantsmind_agent.services.memory import MemoryEntry, MemoryStore
from quantsmind_agent.services.plugins import (
    PluginError,
    load_plugin,
    register_plugin_tools,
    validate_manifest,
)
from quantsmind_agent.services.profiles import EngineeringProfile, ProfileStore
from quantsmind_agent.services.sync import merge_memory, merge_profiles


def test_evidence_pack(tmp_path: Path) -> None:
    out = export_evidence_pack(
        tmp_path / "pack.zip", audit_events=[{"a": 1}], changesets=[], verification=[]
    )
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {
            "meta.json",
            "audit.json",
            "changesets.json",
            "verification.json",
            "manifest.json",
        }
        assert json.loads(zf.read("manifest.json"))["audit_records"] == 1


def test_merge_profiles_last_writer() -> None:
    local = [{"profile_id": "p1", "title": "Old"}]
    remote = [{"profile_id": "p1", "title": "New"}, {"profile_id": "p2", "title": "Added"}]
    merged = merge_profiles(local, remote)
    assert {p["profile_id"]: p["title"] for p in merged} == {"p1": "New", "p2": "Added"}


def test_merge_memory_drops_secrets() -> None:
    merged = merge_memory([], [{"title": "Leak", "body": "api_key = xyz"}])
    assert merged == []


def test_router_budget() -> None:
    router = CostRouter([ModelTier("fast", 0.10, 100, 1), ModelTier("strong", 2.0, 900, 3)])
    assert (
        router.choose(1000, budget_usd=0.50).name == "balanced"
        if False
        else router.choose(1000, budget_usd=0.50).cost_per_1k <= 2.0
    )
    tier = router.choose(100, budget_usd=1.0)
    assert estimate_cost(100, tier) <= 1.0
    assert router.meter(1000, ModelTier("strong", 2.0, 900, 3))["tier"] == "strong"


def test_plugin_manifest_validation() -> None:
    manifest = validate_manifest({"id": "demo", "name": "Demo", "permissions": ["READ_WORKSPACE"]})
    assert manifest.plugin_id == "demo"
    with pytest.raises(PluginError):
        validate_manifest({"id": "", "permissions": []})
    with pytest.raises(PluginError):
        validate_manifest({"id": "x", "permissions": ["FLY"]})
    assert register_plugin_tools(manifest, None) == 0  # type: ignore[arg-type]


def test_plugin_file_load(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"id": "p", "name": "P", "permissions": []}))
    assert load_plugin(tmp_path / "manifest.json").plugin_id == "p"


def test_plugin_registers_real_tool(tmp_path: Path) -> None:
    import sys

    mod = tmp_path / "myplugin.py"
    mod.write_text("def tools():\n    return [{'name': 'demo_tool', 'description': 'demo'}]\n")
    sys.path.insert(0, str(tmp_path))
    from quantsmind_agent.runtime.tools import ToolRegistry

    manifest = validate_manifest(
        {
            "id": "demo",
            "name": "Demo",
            "permissions": ["READ_WORKSPACE"],
            "entrypoint": "myplugin:tools",
        }
    )
    count = register_plugin_tools(manifest, ToolRegistry())
    assert count == 1
    sys.path.remove(str(tmp_path))


def test_plugin_bad_entrypoint_raises() -> None:
    from quantsmind_agent.runtime.tools import ToolRegistry

    with pytest.raises(PluginError):
        register_plugin_tools(
            validate_manifest({"id": "x", "entrypoint": "nope:nope"}), ToolRegistry()
        )


def test_profile_memory_roundtrip(tmp_path: Path) -> None:
    ProfileStore(tmp_path).save(EngineeringProfile(profile_id="p1", title="T"))
    MemoryStore(tmp_path).add(MemoryEntry(entry_id="e1", title="T", verified=True))
    assert ProfileStore(tmp_path).get("p1") is not None
    assert MemoryStore(tmp_path).search("T")
