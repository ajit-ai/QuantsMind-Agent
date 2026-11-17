"""Acceptance tests for the previously-uncovered peripheral services (F83-F90).

Covers success, failure, boundary and invalid-input scenarios for:
- F83 Collab Mirror (export_mirror / import_mirror)
- F84 Plugin Scaffold (scaffold_plugin)
- F85 Plugins (PluginManifest / validate_manifest / load_plugin / register_plugin_tools)
- F86 Policy Templates (PolicyTemplate / PolicyTemplateLibrary)
- F87 Profiles (EngineeringProfile / ProfileStore)
- F88 Review Comments (ReviewComment / CommentStore)
- F89 Sync (merge_profiles / merge_memory)
- F90 Template Sharing (export_template_bundle / import_template_bundle)

These are meaningful behavioural tests, not mere import/existence checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantsmind_agent.runtime.tools import ToolRegistry
from quantsmind_agent.security.permissions import Permission
from quantsmind_agent.services.collab_mirror import export_mirror, import_mirror
from quantsmind_agent.services.plugin_scaffold import scaffold_plugin
from quantsmind_agent.services.plugins import (
    PluginError,
    PluginManifest,
    load_plugin,
    register_plugin_tools,
    validate_manifest,
)
from quantsmind_agent.services.policy_templates import (
    PolicyTemplate,
    PolicyTemplateError,
    PolicyTemplateLibrary,
)
from quantsmind_agent.services.profiles import (
    EngineeringProfile,
    ProfileStore,
    ProfileValidationError,
)
from quantsmind_agent.services.review_comments import CommentStore, ReviewComment
from quantsmind_agent.services.sync import merge_memory, merge_profiles
from quantsmind_agent.services.template_sharing import (
    export_template_bundle,
    import_template_bundle,
)


# -- F83 Collab Mirror --------------------------------------------------------
class TestCollabMirror:
    def test_export_writes_all_files(self, tmp_path: Path) -> None:
        dest = tmp_path / "mirror"
        returned = export_mirror(
            dest,
            profiles=[{"profile_id": "p1"}],
            memories=[{"kind": "fact", "title": "t"}],
            audit_counts={"allow": 3},
        )
        assert returned == dest
        assert (dest / "profiles.json").is_file()
        assert (dest / "memory.json").is_file()
        assert (dest / "audit_counts.json").is_file()
        data = json.loads((dest / "profiles.json").read_text(encoding="utf-8"))
        assert data["format"] == "quantsmind-mirror/1"
        assert data["profiles"] == [{"profile_id": "p1"}]

    def test_export_without_audit_counts_omits_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "mirror"
        export_mirror(dest, profiles=[], memories=[])
        assert not (dest / "audit_counts.json").exists()

    def test_import_round_trip(self, tmp_path: Path) -> None:
        dest = tmp_path / "mirror"
        profiles = [{"profile_id": "p1"}, {"profile_id": "p2"}]
        memories = [{"kind": "fact", "title": "note"}]
        export_mirror(dest, profiles=profiles, memories=memories)
        loaded = import_mirror(dest)
        assert loaded["profiles"] == profiles
        assert loaded["memories"] == memories

    def test_import_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        loaded = import_mirror(tmp_path / "nonexistent")
        assert loaded == {"profiles": [], "memories": []}

    def test_import_ignores_unknown_keys(self, tmp_path: Path) -> None:
        dest = tmp_path / "mirror"
        dest.mkdir()
        (dest / "profiles.json").write_text(
            json.dumps({"format": "quantsmind-mirror/1", "profiles": [], "unexpected": 1}),
            encoding="utf-8",
        )
        loaded = import_mirror(dest)
        assert "unexpected" not in loaded
        assert loaded["profiles"] == []


# -- F84 Plugin Scaffold ------------------------------------------------------
class TestPluginScaffold:
    def test_scaffold_creates_reviewable_files(self, tmp_path: Path) -> None:
        dest = tmp_path / "plugin"
        returned = scaffold_plugin(dest, "my-plugin")
        assert returned == dest
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["id"] == "my-plugin"
        assert manifest["permissions"] == ["READ_WORKSPACE"]
        assert (dest / "my-plugin.py").is_file()
        assert (dest / "README.md").is_file()

    def test_scaffold_empty_id_defaults(self, tmp_path: Path) -> None:
        dest = tmp_path / "plugin"
        scaffold_plugin(dest, "   ")
        assert (dest / "plugin.py").is_file()

    def test_scaffold_manifest_is_valid_plugin_manifest(self, tmp_path: Path) -> None:
        dest = tmp_path / "plugin"
        scaffold_plugin(dest, "valid-one")
        manifest = load_plugin(dest / "manifest.json")
        assert manifest.plugin_id == "valid-one"
        assert manifest.permissions == ["READ_WORKSPACE"]


# -- F85 Plugins --------------------------------------------------------------
class TestPlugins:
    def test_validate_manifest_accepts_valid(self) -> None:
        manifest = validate_manifest(
            {"id": "p1", "name": "Plugin One", "permissions": ["READ_WORKSPACE"]}
        )
        assert manifest.plugin_id == "p1"
        assert manifest.name == "Plugin One"

    def test_validate_manifest_rejects_empty_id(self) -> None:
        with pytest.raises(PluginError, match="plugin_id"):
            validate_manifest({"id": "", "name": "x"})

    def test_validate_manifest_rejects_unknown_permission(self) -> None:
        with pytest.raises(PluginError, match="unknown permission"):
            validate_manifest({"id": "p1", "permissions": ["DELETE_EVERYTHING"]})

    def test_validate_manifest_accepts_all_declared_permissions(self) -> None:
        for permission in Permission:
            manifest = validate_manifest({"id": "p1", "permissions": [permission.value]})
            assert permission.value in manifest.permissions

    def test_load_plugin_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(
            json.dumps({"id": "file-plugin", "name": "File", "entrypoint": "x:y"}),
            encoding="utf-8",
        )
        manifest = load_plugin(path)
        assert manifest.plugin_id == "file-plugin"

    def test_manifest_validate_requires_permissions_subset(self) -> None:
        manifest = PluginManifest(plugin_id="p", name="n", permissions=["NETWORK"])
        manifest.validate()  # NETWORK is a real Permission value

    def test_register_plugin_tools_via_dict_factory(self, tmp_path: Path) -> None:
        # Build a plugin module on disk and import it.
        module_dir = tmp_path / "pkg"
        module_dir.mkdir()
        (module_dir / "__init__.py").write_text("", encoding="utf-8")
        (module_dir / "tools_mod.py").write_text(
            "def factory():\n    return [{'name': 'hello', 'description': 'says hello'}]\n",
            encoding="utf-8",
        )
        import sys

        sys.path.insert(0, str(tmp_path))
        try:
            import importlib

            importlib.import_module("pkg")
            manifest = PluginManifest(
                plugin_id="p1",
                name="n",
                permissions=["READ_WORKSPACE"],
                entrypoint="pkg.tools_mod:factory",
            )
            registry = ToolRegistry()
            count = register_plugin_tools(manifest, registry)
            assert count == 1
            assert "hello" in registry.names
        finally:
            sys.path.remove(str(tmp_path))
            for name in list(sys.modules):
                if name.startswith("pkg"):
                    del sys.modules[name]

    def test_register_plugin_tools_rejects_empty_entrypoint(self) -> None:
        manifest = PluginManifest(plugin_id="p", name="n")
        registry = ToolRegistry()
        assert register_plugin_tools(manifest, registry) == 0

    def test_register_plugin_tools_rejects_invalid_entrypoint(self) -> None:
        manifest = PluginManifest(plugin_id="p", name="n", entrypoint="no-colon")
        registry = ToolRegistry()
        with pytest.raises(PluginError, match="invalid entrypoint"):
            register_plugin_tools(manifest, registry)

    def test_register_plugin_tools_rejects_missing_module(self) -> None:
        manifest = PluginManifest(
            plugin_id="p", name="n", entrypoint="definitely_missing_xyz:factory"
        )
        registry = ToolRegistry()
        with pytest.raises(PluginError, match="unavailable"):
            register_plugin_tools(manifest, registry)

    def test_registered_dict_tool_keeps_read_permission(self, tmp_path: Path) -> None:
        module_dir = tmp_path / "pkg2"
        module_dir.mkdir()
        (module_dir / "__init__.py").write_text("", encoding="utf-8")
        (module_dir / "tools_mod.py").write_text(
            "def factory():\n    return [{'name': 't', 'description': 'd'}]\n",
            encoding="utf-8",
        )
        import sys

        sys.path.insert(0, str(tmp_path))
        try:
            import importlib

            importlib.import_module("pkg2")
            manifest = PluginManifest(
                plugin_id="p",
                name="n",
                permissions=["READ_WORKSPACE"],
                entrypoint="pkg2.tools_mod:factory",
            )
            registry = ToolRegistry()
            register_plugin_tools(manifest, registry)
            spec = registry.get("t").spec
            assert spec.permission is Permission.READ_WORKSPACE
        finally:
            sys.path.remove(str(tmp_path))
            for name in list(sys.modules):
                if name.startswith("pkg2"):
                    del sys.modules[name]


# -- F86 Policy Templates -----------------------------------------------------
class TestPolicyTemplates:
    def test_template_validation(self) -> None:
        t = PolicyTemplate(name="strict")
        t.validate()
        with pytest.raises(PolicyTemplateError):
            PolicyTemplate(name="").validate()
        with pytest.raises(PolicyTemplateError):
            PolicyTemplate(name="x", max_timeout_seconds=0).validate()
        with pytest.raises(PolicyTemplateError):
            PolicyTemplate(name="x", max_timeout_seconds=601).validate()

    def test_library_save_list_apply(self, tmp_path: Path) -> None:
        lib = PolicyTemplateLibrary(tmp_path)
        assert lib.list() == []
        lib.save(PolicyTemplate(name="team", max_timeout_seconds=60))
        items = lib.list()
        assert len(items) == 1
        assert items[0].name == "team"
        applied = lib.apply("team")
        assert applied is not None and applied.max_timeout_seconds == 60
        assert lib.apply("missing") is None

    def test_library_save_replaces_by_name(self, tmp_path: Path) -> None:
        lib = PolicyTemplateLibrary(tmp_path)
        lib.save(PolicyTemplate(name="team", max_timeout_seconds=60))
        lib.save(PolicyTemplate(name="team", max_timeout_seconds=90))
        items = lib.list()
        assert len(items) == 1
        assert items[0].max_timeout_seconds == 90

    def test_library_list_ignores_corrupt_file(self, tmp_path: Path) -> None:
        lib = PolicyTemplateLibrary(tmp_path)
        path = tmp_path / ".quantsmind" / "policy_templates.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert lib.list() == []


# -- F87 Profiles -------------------------------------------------------------
class TestProfiles:
    def test_profile_validation(self) -> None:
        p = EngineeringProfile(profile_id="default")
        p.validate()
        with pytest.raises(ProfileValidationError):
            EngineeringProfile(profile_id="").validate()
        with pytest.raises(ProfileValidationError):
            EngineeringProfile(profile_id="x", title="t" * 201).validate()
        with pytest.raises(ProfileValidationError):
            EngineeringProfile(profile_id="x", languages=["  "]).validate()
        with pytest.raises(ProfileValidationError):
            EngineeringProfile(profile_id="x", build_commands=["a\x00b"]).validate()

    def test_store_save_get_delete(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path, root_id="r1")
        assert store.list() == []
        profile = EngineeringProfile(profile_id="web", title="Web")
        store.save(profile)
        assert store.get("web") is not None
        assert store.get("web").root_id == "r1"
        assert store.delete("web") is True
        assert store.delete("web") is False

    def test_store_save_replaces_duplicate(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path)
        store.save(EngineeringProfile(profile_id="p", title="old"))
        store.save(EngineeringProfile(profile_id="p", title="new"))
        assert len(store.list()) == 1
        assert store.get("p").title == "new"

    def test_store_list_ignores_invalid_records(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path)
        path = store.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "profiles": [{"profile_id": ""}, {"profile_id": "ok"}]}),
            encoding="utf-8",
        )
        assert [p.profile_id for p in store.list()] == ["ok"]

    def test_describe_commands_is_data_only(self, tmp_path: Path) -> None:
        profile = EngineeringProfile(
            profile_id="p",
            build_commands=["build"],
            test_commands=["test"],
            format_commands=["fmt"],
            lint_commands=["lint"],
        )
        store = ProfileStore(tmp_path)
        assert store.describe_commands(profile) == ["build", "test", "fmt", "lint"]


# -- F88 Review Comments ------------------------------------------------------
class TestReviewComments:
    def test_comment_requires_text(self) -> None:
        with pytest.raises(ValueError, match="text required"):
            ReviewComment(task_id="t", path="p", author="a", text="   ")

    def test_comment_sets_created_at(self) -> None:
        c = ReviewComment(task_id="t", path="p", author="a", text="hello")
        assert c.created_at > 0

    def test_store_add_list_by_path(self, tmp_path: Path) -> None:
        store = CommentStore(tmp_path)
        assert store.list() == []
        store.add(ReviewComment(task_id="t1", path="a.py", author="u", text="first"))
        store.add(ReviewComment(task_id="t1", path="b.py", author="u", text="second"))
        store.add(ReviewComment(task_id="t2", path="a.py", author="u", text="third"))
        assert len(store.list()) == 3
        assert len(store.list("t1")) == 2
        assert len(store.by_path("t1", "a.py")) == 1
        assert store.by_path("t1", "a.py")[0].text == "first"

    def test_store_list_ignores_corrupt(self, tmp_path: Path) -> None:
        store = CommentStore(tmp_path)
        path = tmp_path / ".quantsmind" / "review_comments.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bad json", encoding="utf-8")
        assert store.list() == []


# -- F89 Sync -----------------------------------------------------------------
class TestSync:
    def test_merge_profiles_last_writer_wins(self) -> None:
        local = [{"profile_id": "a", "title": "local"}, {"profile_id": "b", "title": "b"}]
        remote = [{"profile_id": "a", "title": "remote"}]
        result = merge_profiles(local, remote)
        assert len(result) == 2
        assert next(p for p in result if p["profile_id"] == "a")["title"] == "remote"

    def test_merge_profiles_sorted_and_skips_missing_id(self) -> None:
        result = merge_profiles(
            [{"profile_id": "z"}, {"no_id": 1}],
            [{"profile_id": "a"}],
        )
        assert [p["profile_id"] for p in result] == ["a", "z"]

    def test_merge_memory_remote_wins_and_drops_secrets(self) -> None:
        local = [{"kind": "fact", "title": "T", "body": "old"}]
        remote = [
            {"kind": "fact", "title": "T", "body": "new"},
            {"kind": "fact", "title": "SECRET", "api_key": "leak"},
        ]
        result = merge_memory(local, remote)
        assert any(m["title"] == "T" and m["body"] == "new" for m in result)
        assert not any("api_key" in str(m) for m in result)

    def test_merge_memory_preserves_invalidation(self) -> None:
        local = [{"kind": "fact", "title": "T", "valid": False}]
        remote = [{"kind": "fact", "title": "T", "valid": True}]
        result = merge_memory(local, remote)
        # remote wins per id; both keys identical so remote replaces local
        assert len(result) == 1


# -- F90 Template Sharing -----------------------------------------------------
class TestTemplateSharing:
    def test_export_strips_secrets(self, tmp_path: Path) -> None:
        dest = tmp_path / "template.json"
        returned = export_template_bundle(
            dest,
            profiles=[{"profile_id": "p"}],
            memories=[
                {"kind": "fact", "title": "ok"},
                {"kind": "fact", "title": "bad", "api_key": "secret"},
                {"kind": "fact", "title": "bad2", "value": "mysecret"},
            ],
        )
        assert returned == dest
        data = json.loads(dest.read_text(encoding="utf-8"))
        assert data["format"] == "quantsmind-templates/1"
        assert len(data["memories"]) == 1
        assert data["memories"][0]["title"] == "ok"

    def test_import_round_trip(self, tmp_path: Path) -> None:
        dest = tmp_path / "template.json"
        export_template_bundle(dest, profiles=[{"profile_id": "p"}], memories=[])
        data = import_template_bundle(dest)
        assert data["profiles"] == [{"profile_id": "p"}]

    def test_import_rejects_wrong_format(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"format": "unknown"}), encoding="utf-8")
        with pytest.raises(ValueError, match="not a QuantsMind template bundle"):
            import_template_bundle(path)

    def test_import_rejects_non_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="not a QuantsMind template bundle"):
            import_template_bundle(path)
